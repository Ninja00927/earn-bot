# Earn bot 💰

Alohida Telegram bot: foydalanuvchi matn yuboradi → admin tasdiqlaydi → balansiga pul tushadi →
foydalanuvchi pulni **kartaga** yoki **USDT (BEP20)** orqali yechish so'rovini yuboradi.

## Foydalanuvchi uchun

| Tugma | Nima qiladi |
|---|---|
| 📝 Yuborish | Matn yozadi, u adminga boradi |
| 💰 Balans | Balans va jami ishlagan pul |
| 💸 Pul yechish | 💳 Karta yoki 🪙 USDT (BEP20) → summa → karta raqami / `0x...` manzil |
| ❓ Yordam | Qisqa yo'riqnoma |

Matn tasdiqlansa yoki rad etilsa, pul yechish so'rovi to'lansa yoki rad etilsa — foydalanuvchiga xabar keladi.
Karta raqami 16 ta raqam, BEP20 manzil `0x` + 40 ta belgi bo'lishi tekshiriladi.

## Admin uchun

Har bir yuborma adminga shunday keladi: kim yubordi (ism, @username, ID), matn va tugmalar:

- **✅ Tasdiqlash** — balansga `REWARD_AMOUNT` qo'shiladi.
- **Boshqa summa** — yuborma xabariga *javob (reply)* qilib summani yozing (masalan `5000`)
  yoki `/approve 12 5000`.
- **❌ Rad etish**.

Pul yechish so'rovi kelganda summa foydalanuvchi balansidan darhol band qilinadi. Pulni o'zingiz
o'tkazasiz va **✅ To'landi** ni bosasiz. **❌ Rad etish** bosilsa pul balansga qaytadi.
Tugmani ikki marta bosish yoki ikki admin bir vaqtda bosishi ikki marta pul qo'shmaydi.

Buyruqlar: `/admin`, `/stats`, `/pending`, `/approve ID SUMMA`, `/user USER_ID`,
`/addbalance USER_ID SUMMA` (ayirish: `-5000`), `/block USER_ID`, `/unblock USER_ID`, `/broadcast MATN`.

## Sozlamalar

| O'zgaruvchi | Ma'nosi | Default |
|---|---|---|
| `BOT_TOKEN` | @BotFather dan **yangi** bot tokeni | — |
| `ADMIN_IDS` | Adminlar ID si, vergul bilan | — |
| `REWARD_AMOUNT` | Har bir tasdiqlangan matn uchun summa | `1000` |
| `MIN_WITHDRAW` | Minimal yechish summasi | `10000` |
| `CURRENCY` | Valyuta nomi (`so'm`, `USDT`, `$`) | `so'm` |
| `DB_PATH` | Baza fayli | `earn.db` (Docker'da `/data/earn.db`) |

## Ishga tushirish

```bash
cd earn_bot
cp .env.example .env      # BOT_TOKEN va ADMIN_IDS ni yozing
pip install -r requirements.txt
python bot.py
```

## Railway'da 24/7

1. Railway loyihasida **+ New → GitHub Repo** → shu repo (student union botidan alohida yangi servis).
2. Servis **Settings → Root Directory** = `earn_bot` (shunda `earn_bot/railway.json` va `Dockerfile` ishlatiladi).
3. **Variables**: `BOT_TOKEN`, `ADMIN_IDS`, `REWARD_AMOUNT`, `MIN_WITHDRAW`, `CURRENCY`.
4. **Volume** qo'shing, mount path `/data` — aks holda redeploy'da balanslar o'chib ketadi.
5. Logda `Earn bot is running` chiqsa — botga `/start` yuboring.

@BotFather `/setcommands`:
```
start - Bosh menyu
balance - Balans
help - Yordam
cancel - Bekor qilish
```
