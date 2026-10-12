"""Earn bot — users send text, an admin approves it, and the user's balance grows.

Flow:
- User taps "📝 Yuborish", sends a text or a file (photo, document, video, audio, voice…)
  → every admin gets it with ✅ / ❌ buttons.
- ✅ credits REWARD_AMOUNT (or a custom amount: reply to the message with a number,
  or /approve <id> <amount>). ❌ rejects it.
- User taps "💸 Pul yechish", picks 💳 Karta or 🪙 USDT (BEP20), enters the amount and the
  card number / BEP20 address. The amount is reserved from the balance immediately.
- Admin pays manually and taps "✅ To'landi", or "❌ Rad etish" which returns the money.
"""

import html
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import DB

load_dotenv()


def _env(name: str, default: str = "") -> str:
    # Tolerate stray spaces or quotes pasted into hosting dashboards.
    return os.environ.get(name, default).strip().strip('"').strip("'").strip()


MAX_AMOUNT = Decimal("1000000000000")  # keeps cents well inside SQLite's 64-bit integers


def _to_cents(value: str) -> int | None:
    try:
        d = Decimal(value.replace(",", ".").replace(" ", ""))
        if not d.is_finite() or d <= 0 or d > MAX_AMOUNT or d != d.quantize(Decimal("0.01")):
            return None
    except InvalidOperation:
        return None
    return int(d * 100)


BOT_TOKEN = _env("BOT_TOKEN")
ADMIN_IDS = {int(x) for x in _env("ADMIN_IDS").replace(" ", "").split(",") if x.isdigit()}
DB_PATH = _env("DB_PATH", "earn.db")
CURRENCY = _env("CURRENCY", "kiro")
REWARD = _to_cents(_env("REWARD_AMOUNT", "1")) or 0
MIN_WITHDRAW = _to_cents(_env("MIN_WITHDRAW", "10")) or 0

MAX_TEXT = 3500

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
# httpx logs full request URLs, which contain the bot token — keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("earn_bot")

db = DB(DB_PATH)

BTN_SUBMIT = "📝 Yuborish"
BTN_BALANCE = "💰 Balans"
BTN_WITHDRAW = "💸 Pul yechish"
BTN_HELP = "❓ Yordam"
BTN_CANCEL = "❌ Bekor qilish"

METHODS = {"card": "💳 Karta", "bep20": "🪙 USDT (BEP20)"}
WD_STATUS = {"pending": "⏳ Kutilmoqda", "paid": "✅ To'landi", "rejected": "❌ Rad etildi"}
SUB_STATUS = {"pending": "⏳ Kutilmoqda", "approved": "✅ Tasdiqlandi", "rejected": "❌ Rad etildi"}

# Telegram message attribute -> label shown to admins. Each name also matches Bot.send_<name>.
FILE_TYPES = {
    "photo": "🖼 Rasm",
    "document": "📄 Fayl",
    "video": "🎬 Video",
    "audio": "🎵 Audio",
    "voice": "🎤 Ovozli xabar",
    "animation": "🎞 GIF",
    "video_note": "⏺ Video xabar",
}

BEP20_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ---------------------------------------------------------------- helpers
def money(cents: int) -> str:
    whole, frac = divmod(abs(cents), 100)
    s = f"{whole:,}".replace(",", " ")
    if frac:
        s += f".{frac:02d}"
    return ("-" if cents < 0 else "") + f"{s} {CURRENCY}"


def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ADMIN_IDS


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_SUBMIT), KeyboardButton(BTN_BALANCE)],
         [KeyboardButton(BTN_WITHDRAW), KeyboardButton(BTN_HELP)]],
        resize_keyboard=True,
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True)


def user_label(user_id: int) -> str:
    u = db.get_user(user_id)
    name = html.escape(u["full_name"] or "") if u else ""
    uname = f" @{html.escape(u['username'])}" if u and u["username"] else ""
    return f'<a href="tg://user?id={user_id}">{name or user_id}</a>{uname} (<code>{user_id}</code>)'


def sub_keyboard(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Tasdiqlash (+{money(REWARD)})", callback_data=f"sa:{sub_id}"),
        InlineKeyboardButton("❌ Rad etish", callback_data=f"sr:{sub_id}"),
    ]])


def wd_keyboard(wd_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ To'landi", callback_data=f"wp:{wd_id}"),
        InlineKeyboardButton("❌ Rad etish", callback_data=f"wr:{wd_id}"),
    ]])


def sub_admin_text(sub) -> str:
    text = (
        f"📝 <b>Yangi yuborma #{sub['id']}</b>\n"
        f"👤 {user_label(sub['user_id'])}\n"
        f"🕒 {fmt_dt(sub['created_at'])}"
    )
    if sub["file_type"]:
        text += f"\n📎 {FILE_TYPES.get(sub['file_type'], sub['file_type'])} (yuqoridagi xabarda)"
    if sub["text"]:
        text += f"\n\n{html.escape(sub['text'])}"
    if sub["status"] == "pending":
        text += ("\n\n<i>Boshqa summa bilan tasdiqlash uchun shu xabarga javob qilib summani yozing "
                 f"yoki /approve {sub['id']} SUMMA</i>")
    else:
        extra = f" (+{money(sub['amount'])})" if sub["status"] == "approved" else ""
        text += f"\n\n<b>{SUB_STATUS[sub['status']]}{extra}</b>"
    return text


def wd_admin_text(wd) -> str:
    text = (
        f"💸 <b>Pul yechish so'rovi #{wd['id']}</b>\n"
        f"👤 {user_label(wd['user_id'])}\n"
        f"💰 Summa: <b>{money(wd['amount'])}</b>\n"
        f"🏦 Usul: {METHODS[wd['method']]}\n"
        f"📋 Rekvizit: <code>{html.escape(wd['details'])}</code>\n"
        f"🕒 {fmt_dt(wd['created_at'])}"
    )
    if wd["status"] != "pending":
        text += f"\n\n<b>{WD_STATUS[wd['status']]}</b>"
    return text


async def broadcast_to_admins(context: ContextTypes.DEFAULT_TYPE, kind: str, item_id: int,
                              text: str, markup: InlineKeyboardMarkup) -> None:
    for admin_id in ADMIN_IDS:
        try:
            msg = await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML,
                                                 reply_markup=markup, disable_web_page_preview=True)
            db.add_admin_msg(kind, item_id, admin_id, msg.message_id)
        except TelegramError as e:
            log.warning("Could not notify admin %s: %s", admin_id, e)


async def send_sub_to_admin(bot, chat_id: int, sub):
    """Sends the attached file (if any), then the card with buttons as a reply to it."""
    reply_to = None
    if sub["file_id"]:
        file_msg = await getattr(bot, f"send_{sub['file_type']}")(chat_id, sub["file_id"])
        reply_to = file_msg.message_id
    return await bot.send_message(chat_id, sub_admin_text(sub), parse_mode=ParseMode.HTML,
                                  reply_markup=sub_keyboard(sub["id"]), disable_web_page_preview=True,
                                  reply_to_message_id=reply_to)


async def notify_admins_of_sub(context: ContextTypes.DEFAULT_TYPE, sub) -> None:
    for admin_id in ADMIN_IDS:
        try:
            msg = await send_sub_to_admin(context.bot, admin_id, sub)
            db.add_admin_msg("sub", sub["id"], admin_id, msg.message_id)
        except TelegramError as e:
            log.warning("Could not notify admin %s: %s", admin_id, e)


async def refresh_admin_msgs(context: ContextTypes.DEFAULT_TYPE, kind: str, item_id: int, text: str) -> None:
    """After a decision, update every admin's copy so nobody acts on it twice."""
    for row in db.admin_msgs_for(kind, item_id):
        try:
            await context.bot.edit_message_text(text, chat_id=row["chat_id"], message_id=row["message_id"],
                                                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramError:
            pass


async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    try:
        await context.bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        log.info("Could not notify user %s: %s", user_id, e)


def touch(update: Update) -> None:
    u = update.effective_user
    db.touch_user(u.id, u.username, u.full_name)


def reset(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("state", "wd_method", "wd_amount"):
        context.user_data.pop(key, None)


# ---------------------------------------------------------------- user side
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch(update)
    reset(context)
    await update.message.reply_text(
        "👋 <b>Xush kelibsiz!</b>\n\n"
        f"<b>{BTN_SUBMIT}</b> — matn yuboring. Admin tasdiqlasa balansingizga "
        f"<b>{money(REWARD)}</b> qo'shiladi.\n"
        f"<b>{BTN_BALANCE}</b> — hisobingizni ko'rish.\n"
        f"<b>{BTN_WITHDRAW}</b> — kartaga yoki USDT (BEP20) orqali chiqarish.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "❓ <b>Qanday ishlaydi?</b>\n\n"
        f"1. <b>{BTN_SUBMIT}</b> tugmasini bosing va matningizni yozing.\n"
        "2. Admin uni tekshiradi. Tasdiqlansa — balansingizga pul tushadi, sizga xabar keladi.\n"
        f"3. <b>{BTN_WITHDRAW}</b> → usulni tanlang (💳 Karta yoki 🪙 USDT BEP20) → summani "
        "→ karta raqami yoki BEP20 manzilini yozing.\n"
        f"4. Admin to'lovni amalga oshirgach sizga xabar keladi.\n\n"
        f"Minimal yechish summasi: <b>{money(MIN_WITHDRAW)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    await update.message.reply_text("Bekor qilindi.", reply_markup=main_menu())


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch(update)
    u = db.get_user(update.effective_user.id)
    pending = " (ko'rib chiqilayotgan pul yechish so'rovingiz bor)" if db.user_has_pending_withdrawal(u["user_id"]) else ""
    await update.message.reply_text(
        f"💰 <b>Balans:</b> {money(u['balance'])}{pending}\n"
        f"📈 Jami ishlangan: {money(u['earned'])}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def start_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset(context)
    context.user_data["state"] = "submit"
    await update.message.reply_text(
        "📝 Matn yoki fayl (rasm, hujjat, video, audio, ovozli xabar) yuboring. "
        "Faylga izoh ham yozishingiz mumkin.",
        reply_markup=cancel_menu(),
    )


async def start_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch(update)
    reset(context)
    u = db.get_user(update.effective_user.id)
    if db.user_has_pending_withdrawal(u["user_id"]):
        await update.message.reply_text("⏳ Sizda hali ko'rib chiqilmagan so'rov bor. Iltimos, kuting.",
                                        reply_markup=main_menu())
        return
    if u["balance"] < max(MIN_WITHDRAW, 1):
        await update.message.reply_text(
            f"❗️ Balansingiz yetarli emas.\nBalans: {money(u['balance'])}\n"
            f"Minimal summa: {money(MIN_WITHDRAW)}",
            reply_markup=main_menu(),
        )
        return
    await update.message.reply_text(
        f"💰 Balans: <b>{money(u['balance'])}</b>\n\nPulni qanday olmoqchisiz?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f"wm:{key}")] for key, label in METHODS.items()
        ]),
    )


async def on_method(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str) -> None:
    query = update.callback_query
    await query.answer()
    reset(context)
    context.user_data["state"] = "wd_amount"
    context.user_data["wd_method"] = method
    u = db.get_user(query.from_user.id)
    await query.edit_message_text(f"Tanlandi: {METHODS[method]}")
    await context.bot.send_message(
        query.from_user.id,
        f"💰 Qancha yechmoqchisiz? Summani yozing.\n"
        f"Balans: {money(u['balance'])}, minimal: {money(MIN_WITHDRAW)}\n"
        f"Hammasini yechish uchun <b>hammasi</b> deb yozing.",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_menu(),
    )


async def handle_wd_amount(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    u = db.get_user(update.effective_user.id)
    amount = u["balance"] if text.lower() in ("hammasi", "all", "barchasi") else _to_cents(text)
    if amount is None:
        await update.message.reply_text("❗️ Faqat raqam yozing, masalan: 20")
        return
    if amount < MIN_WITHDRAW:
        await update.message.reply_text(f"❗️ Minimal summa: {money(MIN_WITHDRAW)}")
        return
    if amount > u["balance"]:
        await update.message.reply_text(f"❗️ Balansingizda faqat {money(u['balance'])} bor.")
        return
    context.user_data["wd_amount"] = amount
    context.user_data["state"] = "wd_details"
    if context.user_data["wd_method"] == "card":
        prompt = "💳 Karta raqamingizni yozing (16 ta raqam):"
    else:
        prompt = ("🪙 USDT <b>BEP20</b> (BNB Smart Chain) manzilingizni yozing.\n"
                  "Manzil <code>0x</code> bilan boshlanadi. ⚠️ Boshqa tarmoq manzilini yozmang!")
    await update.message.reply_text(prompt, parse_mode=ParseMode.HTML)


async def handle_wd_details(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    method = context.user_data["wd_method"]
    if method == "card":
        digits = re.sub(r"[\s-]", "", text)
        if not digits.isdigit() or len(digits) != 16:
            await update.message.reply_text("❗️ Karta raqami 16 ta raqamdan iborat bo'lishi kerak. Qaytadan yozing:")
            return
        details = " ".join(digits[i:i + 4] for i in range(0, 16, 4))
    else:
        details = text.strip()
        if not BEP20_RE.match(details):
            await update.message.reply_text(
                "❗️ Noto'g'ri BEP20 manzil. U 0x bilan boshlanib, jami 42 ta belgidan iborat bo'ladi. Qaytadan yozing:")
            return

    amount = context.user_data["wd_amount"]
    user_id = update.effective_user.id
    reset(context)
    if db.user_has_pending_withdrawal(user_id):
        await update.message.reply_text("⏳ Sizda hali ko'rib chiqilmagan so'rov bor.", reply_markup=main_menu())
        return
    wd_id = db.create_withdrawal(user_id, amount, method, details)
    if wd_id is None:
        await update.message.reply_text("❗️ Balans yetarli emas.", reply_markup=main_menu())
        return
    await update.message.reply_text(
        f"✅ So'rov #{wd_id} qabul qilindi!\n"
        f"💰 {money(amount)} → {METHODS[method]}\n"
        f"📋 <code>{html.escape(details)}</code>\n\n"
        "Admin to'lovni amalga oshirgach sizga xabar keladi.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )
    wd = db.get_withdrawal(wd_id)
    await broadcast_to_admins(context, "wd", wd_id, wd_admin_text(wd), wd_keyboard(wd_id))


async def handle_submission(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                            file_id: str | None = None, file_type: str | None = None) -> None:
    if len(text) > MAX_TEXT:
        await update.message.reply_text(f"❗️ Matn juda uzun (maksimum {MAX_TEXT} belgi).")
        return
    reset(context)
    sub_id = db.create_submission(update.effective_user.id, text, file_id, file_type)
    await update.message.reply_text(
        f"✅ Yuborma #{sub_id} adminga yuborildi. Tasdiqlansa balansingizga pul tushadi.",
        reply_markup=main_menu(),
    )
    await notify_admins_of_sub(context, db.get_submission(sub_id))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    touch(update)
    msg = update.message
    text = msg.text.strip()

    # Admin replied to a submission with a number → approve with that amount.
    if is_admin(update) and msg.reply_to_message:
        link = db.find_admin_msg(msg.chat_id, msg.reply_to_message.message_id)
        if link and link["kind"] == "sub":
            amount = _to_cents(text)
            if amount is None:
                await msg.reply_text("❗️ Summani raqam bilan yozing, masalan: 2")
                return
            await approve(update, context, link["item_id"], amount)
            return

    u = db.get_user(update.effective_user.id)
    if u["blocked"]:
        await msg.reply_text("🚫 Siz bloklangansiz.")
        return

    if text == BTN_CANCEL:
        return await cancel_cmd(update, context)
    if text == BTN_SUBMIT:
        return await start_submit(update, context)
    if text == BTN_BALANCE:
        reset(context)
        return await balance_cmd(update, context)
    if text == BTN_WITHDRAW:
        return await start_withdraw(update, context)
    if text == BTN_HELP:
        reset(context)
        return await help_cmd(update, context)

    state = context.user_data.get("state")
    if state == "submit":
        return await handle_submission(update, context, text)
    if state == "wd_amount":
        return await handle_wd_amount(update, context, text)
    if state == "wd_details":
        return await handle_wd_details(update, context, text)

    await msg.reply_text(f"Matn yuborish uchun avval «{BTN_SUBMIT}» tugmasini bosing.", reply_markup=main_menu())


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Non-text messages: accepted as a submission when the user is in submit mode."""
    touch(update)
    msg = update.message
    if db.get_user(update.effective_user.id)["blocked"]:
        await msg.reply_text("🚫 Siz bloklangansiz.")
        return
    if context.user_data.get("state") != "submit":
        await msg.reply_text(f"Fayl yuborish uchun avval «{BTN_SUBMIT}» tugmasini bosing.", reply_markup=main_menu())
        return
    for file_type in FILE_TYPES:
        media = getattr(msg, file_type, None)
        if media:
            file_id = media[-1].file_id if file_type == "photo" else media.file_id
            return await handle_submission(update, context, (msg.caption or "").strip(), file_id, file_type)
    await msg.reply_text("❗️ Bu turdagi xabar qabul qilinmaydi. Matn yoki fayl yuboring.")


# ---------------------------------------------------------------- admin side
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, amount: int) -> str:
    sub = db.approve_submission(sub_id, amount, update.effective_user.id)
    if sub is None:
        existing = db.get_submission(sub_id)
        note = f"#{sub_id} topilmadi." if not existing else f"#{sub_id} allaqachon: {SUB_STATUS[existing['status']]}"
        if update.message:
            await update.message.reply_text(note)
        return note
    await refresh_admin_msgs(context, "sub", sub_id, sub_admin_text(sub))
    balance = db.get_user(sub["user_id"])["balance"]
    await notify_user(context, sub["user_id"],
                      f"✅ Yuborma #{sub_id} tasdiqlandi!\n💰 +{money(amount)}\nBalans: <b>{money(balance)}</b>")
    note = f"✅ #{sub_id} tasdiqlandi, +{money(amount)}"
    if update.message:
        await update.message.reply_text(note)
    return note


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, arg = (query.data or "").partition(":")

    if action == "wm" and arg in METHODS:
        return await on_method(update, context, arg)

    if not is_admin(update):
        await query.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        item_id = int(arg)
    except ValueError:
        await query.answer()
        return
    admin_id = query.from_user.id

    if action == "sa":
        note = await approve(update, context, item_id, REWARD)
        await query.answer(note)
    elif action == "sr":
        sub = db.reject_submission(item_id, admin_id)
        if sub is None:
            await query.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
            return
        await refresh_admin_msgs(context, "sub", item_id, sub_admin_text(sub))
        await notify_user(context, sub["user_id"], f"❌ Yuborma #{item_id} rad etildi.")
        await query.answer("Rad etildi")
    elif action == "wp":
        wd = db.mark_withdrawal_paid(item_id, admin_id)
        if wd is None:
            await query.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
            return
        await refresh_admin_msgs(context, "wd", item_id, wd_admin_text(wd))
        await notify_user(context, wd["user_id"],
                          f"✅ So'rov #{item_id} bo'yicha <b>{money(wd['amount'])}</b> "
                          f"{METHODS[wd['method']]} orqali to'landi!")
        await query.answer("To'landi deb belgilandi")
    elif action == "wr":
        wd = db.reject_withdrawal(item_id, admin_id)
        if wd is None:
            await query.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
            return
        await refresh_admin_msgs(context, "wd", item_id, wd_admin_text(wd))
        await notify_user(context, wd["user_id"],
                          f"❌ So'rov #{item_id} rad etildi. {money(wd['amount'])} balansingizga qaytarildi.")
        await query.answer("Rad etildi, pul qaytarildi")
    else:
        await query.answer()


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            return
        return await func(update, context)
    return wrapper


@admin_only
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🛠 <b>Admin buyruqlari</b>\n\n"
        "/stats — statistika\n"
        "/pending — kutilayotgan yuborma va so'rovlar\n"
        "/approve ID SUMMA — yuborimani boshqa summa bilan tasdiqlash\n"
        "/user USER_ID — foydalanuvchi balansi\n"
        "/addbalance USER_ID SUMMA — balansga qo'shish (ayirish uchun minus: -5)\n"
        "/block USER_ID, /unblock USER_ID\n"
        "/broadcast MATN — hammaga xabar\n\n"
        "Yuborma xabariga javob qilib summani yozsangiz — shu summa bilan tasdiqlanadi.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = db.stats()
    await update.message.reply_text(
        "📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: {s['users']}\n"
        f"📝 Yuborma: ⏳ {s['sub_pending']} · ✅ {s['sub_approved']} · ❌ {s['sub_rejected']}\n"
        f"💰 Jami qo'shilgan: {money(s['credited'])}\n"
        f"🏦 Balanslarda: {money(s['balances'])}\n"
        f"💸 Kutilayotgan yechish: {s['wd_pending']} ta ({money(s['wd_pending_sum'])})\n"
        f"✅ Jami to'langan: {money(s['paid'])}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs, wds = db.pending_submissions(), db.pending_withdrawals()
    if not subs and not wds:
        await update.message.reply_text("Hech narsa kutilmayapti ✅")
        return
    for sub in subs:
        msg = await send_sub_to_admin(context.bot, update.effective_chat.id, sub)
        db.add_admin_msg("sub", sub["id"], msg.chat_id, msg.message_id)
    for wd in wds:
        msg = await update.message.reply_text(wd_admin_text(wd), parse_mode=ParseMode.HTML,
                                              reply_markup=wd_keyboard(wd["id"]), disable_web_page_preview=True)
        db.add_admin_msg("wd", wd["id"], msg.chat_id, msg.message_id)


@admin_only
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    amount = _to_cents(args[1]) if len(args) == 2 else None
    if len(args) != 2 or not args[0].isdigit() or amount is None:
        await update.message.reply_text("Foydalanish: /approve ID SUMMA")
        return
    await approve(update, context, int(args[0]), amount)


@admin_only
async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Foydalanish: /user USER_ID")
        return
    u = db.get_user(int(context.args[0]))
    if not u:
        await update.message.reply_text("Topilmadi.")
        return
    await update.message.reply_text(
        f"👤 {user_label(u['user_id'])}\n💰 Balans: {money(u['balance'])}\n📈 Ishlagan: {money(u['earned'])}\n"
        f"{'🚫 Bloklangan' if u['blocked'] else ''}",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def addbalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 2 or not args[0].isdigit():
        await update.message.reply_text("Foydalanish: /addbalance USER_ID SUMMA")
        return
    negative = args[1].startswith("-")
    cents = _to_cents(args[1].lstrip("-"))
    if cents is None:
        await update.message.reply_text("Summa noto'g'ri.")
        return
    delta = -cents if negative else cents
    new = db.adjust_balance(int(args[0]), delta)
    if new is None:
        await update.message.reply_text("Foydalanuvchi topilmadi yoki balans manfiy bo'lib qoladi.")
        return
    await update.message.reply_text(f"✅ Yangi balans: {money(new)}")
    await notify_user(context, int(args[0]),
                      f"💰 Balansingiz o'zgardi: {'+' if delta > 0 else ''}{money(delta)}\nBalans: <b>{money(new)}</b>")


async def _set_block(update: Update, context: ContextTypes.DEFAULT_TYPE, blocked: bool) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text(f"Foydalanish: /{'block' if blocked else 'unblock'} USER_ID")
        return
    ok = db.set_blocked(int(context.args[0]), blocked)
    await update.message.reply_text("✅ Bajarildi" if ok else "Topilmadi.")


@admin_only
async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_block(update, context, True)


@admin_only
async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_block(update, context, False)


@admin_only
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Foydalanish: /broadcast MATN")
        return
    sent = failed = 0
    for uid in db.all_user_ids():
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except TelegramError:
            failed += 1
    await update.message.reply_text(f"📣 Yuborildi: {sent}, xato: {failed}")


# ---------------------------------------------------------------- main
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


def main() -> None:
    missing = [name for name, value in (("BOT_TOKEN", BOT_TOKEN), ("ADMIN_IDS", ADMIN_IDS)) if not value]
    if missing:
        raise SystemExit(
            f"Missing setting(s): {', '.join(missing)}. On Railway add them under the service's "
            "Variables tab and click Deploy; locally put them in a .env file (see .env.example)."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE

    for name, handler in [
        ("start", start), ("help", help_cmd), ("cancel", cancel_cmd), ("balance", balance_cmd),
        ("admin", admin_cmd), ("stats", stats_cmd), ("pending", pending_cmd), ("approve", approve_cmd),
        ("user", user_cmd), ("addbalance", addbalance_cmd), ("block", block_cmd), ("unblock", unblock_cmd),
        ("broadcast", broadcast_cmd),
    ]:
        app.add_handler(CommandHandler(name, handler, filters=private))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & ~filters.COMMAND & ~filters.TEXT, on_other))
    app.add_error_handler(on_error)

    log.info("Earn bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
