# 🍪 Stream Cookie Bot

A Telegram bot that delivers streaming service cookies/accounts on demand.

---

## 🚀 Free Hosting Options

### ✅ Recommended — Railway (Easiest)

Railway gives **$5/month free credit** — more than enough for this bot.
Data persists on a volume between restarts.

1. Push this folder to a GitHub repo.
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**.
3. Add a **Volume** (Storage → Add Volume → mount at `/data`).
4. Set environment variables under **Variables**:
   ```
   BOT_TOKEN   = your_token
   ADMIN_ID    = your_user_id
   DB_PATH     = /data/bot_data.db
   ```
5. Railway detects `railway.json` and runs `python bot.py` automatically.

---

### ✅ Fly.io (Also Free)

Fly.io has a permanent free tier with persistent volumes.

```bash
# Install flyctl: https://fly.io/docs/getting-started/installing-flyctl/
fly auth login
fly launch              # follow prompts, use fly.toml as config
fly volumes create bot_data_vol --size 1 --region ams
fly secrets set BOT_TOKEN=xxx ADMIN_ID=yyy
fly deploy
```

---

### ⚠️ Vercel (Serverless — Limited)

Vercel works for **webhook mode only**, but its filesystem is **ephemeral**
(SQLite resets between cold starts). Best used for testing or if you mount
an external DB.

**Steps:**
1. Deploy to Vercel (GitHub import or `vercel deploy`).
2. Add `BOT_TOKEN` and `ADMIN_ID` in **Project → Settings → Environment Variables**.
3. Register the webhook with Telegram (**run once locally**):
   ```bash
   pip install requests
   python set_webhook.py https://your-app.vercel.app/api/webhook
   ```
4. That's it — Telegram pushes updates to `/api/webhook`.

> For persistent data on Vercel, use [Neon](https://neon.tech) (free Postgres)
> and replace the `db_connect()` function in `bot.py` with a psycopg2 connection.

---

### ⚠️ Render (Paid for workers)

Render's free tier **no longer supports background workers** (only web services
sleep after 15 min). The `render.yaml` targets the `starter` plan ($7/mo).
Use Railway or Fly.io for a truly free option.

---

## 🔧 Environment Variables

| Variable      | Required | Description |
|---------------|----------|-------------|
| `BOT_TOKEN`   | ✅ | Your bot token from [@BotFather](https://t.me/botfather) |
| `ADMIN_ID`    | ✅ | Your Telegram numeric user ID |
| `DB_PATH`     | ➖ | SQLite path (default: `bot_data.db`) |
| `USE_WEBHOOK` | ➖ | Set `true` to enable webhook mode |
| `WEBHOOK_URL` | ➖ | Your HTTPS base URL (e.g. `https://my-bot.railway.app`) |
| `PORT`        | ➖ | Port for webhook server (default: `8080`) |

---

## 🍪 Adding Cookies

### Via Telegram (recommended for all platforms)

1. Start a chat with your bot.
2. Send a `.txt` file (one account per file).
3. The bot will ask which service it belongs to, or you can set the **caption** to the service key directly (e.g. `netflix`).

### Via filesystem (Railway / Fly.io / VPS)

Drop `.txt` files into:
```
cookies/netflix/
cookies/spotify/
cookies/crunchyroll/
```

Each file = one account. Supported formats:
- `email:password`
- `email=x\npassword=y`
- Raw cookie string

---

## 👤 User Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu — pick a service |
| `/redeem <code>` | Redeem a giveaway code |
| `/help` | Help & info |

## 🛠️ Admin Commands

| Command | Description |
|---------|-------------|
| `/admin` | Stats panel |
| `/gencode <service> [n]` | Generate redeem codes |
| `/addservice <key> <name> <emoji>` | Add a service |
| `/removeservice <key>` | Remove a service |
| `/listservices` | List services + stock |
| `/addadmin <user_id>` | Add an admin |
| `/removeadmin <user_id>` | Remove an admin |
| `/listadmins` | List all admins |
| `/refunds` | View pending refund requests |
| `/checkcookie [service]` | Peek at a cookie |

Send a `.txt` file to the bot to add cookies directly via Telegram.

---

## 🏗️ Architecture Changes (vs original)

| Feature | Before | After |
|---------|--------|-------|
| Cookie storage | Files only | **DB + files** (DB takes priority) |
| User states | In-memory dict | **SQLite table** (survives restarts) |
| Run mode | Polling only | **Polling + Webhook** (auto-detected) |
| Cookie upload | FTP/SCP files | **Send `.txt` files in Telegram chat** |
| Platform configs | render.yaml only | **Railway, Fly.io, Vercel, Render** |
