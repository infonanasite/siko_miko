"""
Stream Cookie Bot — Multi-Platform Edition
Telegram bot (polling or webhook) + Flask admin panel served on PORT.
"""

import os
import re
import sqlite3
import random
import string
import logging
import time
import asyncio
import threading
import secrets
from pathlib import Path
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

# Import the Netflix checker
from checker import check_netflix_account, country_to_flag

# ─── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MASTER_ADMIN  = int(os.getenv("ADMIN_ID", "0"))
DB_PATH       = os.getenv("DB_PATH", "bot_data.db")
PORT          = int(os.getenv("PORT", "5000"))
USE_WEBHOOK   = os.getenv("USE_WEBHOOK", "").lower() in ("1", "true", "yes")
WEBHOOK_URL   = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH  = "/webhook"
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

if not ADMIN_API_KEY:
    ADMIN_API_KEY = secrets.token_hex(20)
    logger.warning("=" * 60)
    logger.warning("ADMIN_API_KEY not set — generated one for this session:")
    logger.warning(f"  {ADMIN_API_KEY}")
    logger.warning("Set ADMIN_API_KEY env var to make it permanent.")
    logger.warning("=" * 60)

# ─── Default services ────────────────────────────────────────────────────────────
DEFAULT_SERVICES = [
    {"key": "netflix",     "name": "Netflix",     "emoji": "🍪"},
    {"key": "crunchyroll", "name": "Crunchyroll", "emoji": "🥨"},
    {"key": "spotify",     "name": "Spotify",     "emoji": "🎧"},
]

checker_rate: dict = {}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── Directory setup ─────────────────────────────────────────────────────────────
def ensure_dirs():
    for svc in get_services():
        Path(f"cookies/{svc['key']}").mkdir(parents=True, exist_ok=True)
        Path(f"sent/{svc['key']}").mkdir(parents=True, exist_ok=True)

# ─── Database ────────────────────────────────────────────────────────────────────
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_connect()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS claimed_users (
            user_id    INTEGER,
            service    TEXT,
            claimed_at INTEGER,
            PRIMARY KEY (user_id, service)
        );
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code       TEXT    PRIMARY KEY,
            service    TEXT    DEFAULT 'netflix',
            used_by    INTEGER,
            used_at    INTEGER,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS services (
            key   TEXT PRIMARY KEY,
            name  TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT '🍪'
        );
        CREATE TABLE IF NOT EXISTS refund_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            service      TEXT,
            photo_id     TEXT,
            status       TEXT    DEFAULT 'pending',
            requested_at INTEGER,
            resolved_at  INTEGER,
            resolved_by  INTEGER
        );
        CREATE TABLE IF NOT EXISTS refund_log (
            user_id     INTEGER,
            service     TEXT,
            refunded_at INTEGER,
            UNIQUE(user_id, service)
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id  INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS cookie_stock (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            service    TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            claimed_at INTEGER,
            added_at   INTEGER,
            added_by   INTEGER
        );
        CREATE TABLE IF NOT EXISTS user_states (
            user_id     INTEGER PRIMARY KEY,
            state       TEXT,
            service_key TEXT,
            updated_at  INTEGER
        );
    """)
    conn.commit()
    c.execute("SELECT COUNT(*) FROM services")
    if c.fetchone()[0] == 0:
        for s in DEFAULT_SERVICES:
            c.execute(
                "INSERT OR IGNORE INTO services (key,name,emoji) VALUES (?,?,?)",
                (s["key"], s["name"], s["emoji"]),
            )
        conn.commit()
    conn.close()

# ─── User-state helpers ──────────────────────────────────────────────────────────
def get_user_state(user_id: int):
    conn = db_connect()
    row = conn.execute(
        "SELECT state, service_key FROM user_states WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return (row["state"], row["service_key"]) if row else None

def set_user_state(user_id: int, state: str, service_key: str):
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO user_states (user_id,state,service_key,updated_at) VALUES (?,?,?,?)",
        (user_id, state, service_key, int(time.time())),
    )
    conn.commit()
    conn.close()

def clear_user_state(user_id: int):
    conn = db_connect()
    conn.execute("DELETE FROM user_states WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ─── General helpers ─────────────────────────────────────────────────────────────
def get_services() -> list[dict]:
    conn = db_connect()
    rows = conn.execute("SELECT * FROM services").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def is_admin(user_id: int) -> bool:
    if user_id == MASTER_ADMIN:
        return True
    conn = db_connect()
    row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None

def get_all_admins() -> list[int]:
    admins = [MASTER_ADMIN] if MASTER_ADMIN else []
    conn = db_connect()
    rows = conn.execute("SELECT user_id FROM admins").fetchall()
    conn.close()
    admins += [r["user_id"] for r in rows]
    return list(set(admins))

def stock_count(service_key: str | None = None):
    conn = db_connect()
    if service_key:
        db_count = conn.execute(
            "SELECT COUNT(*) FROM cookie_stock WHERE service=? AND claimed_at IS NULL",
            (service_key,),
        ).fetchone()[0]
        conn.close()
        folder = Path(f"cookies/{service_key}")
        return db_count + (len(list(folder.glob("*.txt"))) if folder.exists() else 0)
    result = {}
    for svc in get_services():
        db_count = conn.execute(
            "SELECT COUNT(*) FROM cookie_stock WHERE service=? AND claimed_at IS NULL",
            (svc["key"],),
        ).fetchone()[0]
        folder = Path(f"cookies/{svc['key']}")
        result[svc["key"]] = db_count + (len(list(folder.glob("*.txt"))) if folder.exists() else 0)
    conn.close()
    return result

def parse_account(content: str) -> dict:
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if not lines:
        return {"type": "cookie", "raw": content, "email": "", "password": ""}
    kv = {}
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip().lower()] = v.strip()
    if "email" in kv or "password" in kv or "user" in kv:
        return {
            "type": "userpass",
            "email":    kv.get("email") or kv.get("user") or kv.get("username") or "",
            "password": kv.get("password") or kv.get("pass") or "",
            "raw": content,
        }
    first = lines[0]
    if first.count(":") >= 1:
        left, right = first.split(":", 1)
        if "@" in left or (len(left) > 3 and len(right) > 3 and " " not in left):
            return {"type": "userpass", "email": left.strip(), "password": right.strip(), "raw": content}
    return {"type": "cookie", "raw": content, "email": "", "password": ""}

def pick_cookie(service_key: str):
    conn = db_connect()
    row = conn.execute(
        "SELECT id, content FROM cookie_stock WHERE service=? AND claimed_at IS NULL ORDER BY RANDOM() LIMIT 1",
        (service_key,),
    ).fetchone()
    if row:
        conn.execute("UPDATE cookie_stock SET claimed_at=? WHERE id=?", (int(time.time()), row["id"]))
        conn.commit()
        conn.close()
        return parse_account(row["content"]), f"db:{row['id']}"
    conn.close()
    folder = Path(f"cookies/{service_key}")
    files  = list(folder.glob("*.txt")) if folder.exists() else []
    if not files:
        return None
    chosen = random.choice(files)
    return parse_account(chosen.read_text(encoding="utf-8").strip()), str(chosen)

def move_cookie_to_sent(filepath: str, service_key: str):
    if filepath.startswith("db:"):
        return
    src  = Path(filepath)
    dest = Path(f"sent/{service_key}/{src.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)

def generate_redeem_code(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

def format_account_message(account: dict, service_key: str, prefix: str) -> str:
    msg = f"{prefix}\n\n📺 *Service:* {service_key.title()}\n\n"
    if account["type"] == "userpass":
        msg += (
            f"📧 *Email:* `{account['email']}`\n"
            f"🔑 *Password:* `{account['password']}`\n\n"
            f"_Tap any field to copy it. Enjoy! 🍿_"
        )
    else:
        msg += f"🍪 *Cookie:*\n`{account['raw']}`\n\n_Tap the cookie to copy it. Enjoy! 🍿_"
    msg += "\n\n⚠️ Having issues? Use /start and tap 🔄 Refund."
    return msg

# ─── Telegram handlers ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    counts  = stock_count()
    buttons = [
        [InlineKeyboardButton(
            f"{s['emoji']} {s['name']} ({counts.get(s['key'], 0)} in stock)",
            callback_data=f"service:{s['key']}",
        )]
        for s in get_services()
    ]
    buttons.append([InlineKeyboardButton("🔄 Request Refund", callback_data="refund")])
    await update.message.reply_text(
        f"👋 *Hey {user.first_name or 'there'}! Welcome to the Stream Cookie Bot!*\n\n"
        f"Pick a service and get your cookie instantly 👇\n\n"
        f"`/redeem <code>` — Redeem a giveaway code\n`/help` — Help & info",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = (
        "🤖 *Stream Cookie Bot*\n\n"
        "👤 *User Commands:*\n"
        "`/start` — Main menu\n`/redeem <code>` — Redeem code\n`/help` — Help\n\n"
    )
    if is_admin(uid):
        text += (
            "🛠️ *Admin Commands:*\n"
            "`/admin` — Stats panel\n`/gencode <service> [n]` — Generate codes\n"
            "`/addservice <key> <name> <emoji>` — Add service\n"
            "`/removeservice <key>` — Remove service\n"
            "`/listservices` — List services\n`/addadmin <id>` — Add admin\n"
            "`/removeadmin <id>` — Remove admin\n`/listadmins` — List admins\n"
            "`/refunds` — Pending refunds\n`/checkcookie [service]` — Peek cookie\n\n"
            "📤 Send a `.txt` file to add a cookie directly."
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    uid   = query.from_user.id

    if data.startswith("service:"):
        service_key = data.split(":", 1)[1]
        services    = {s["key"]: s for s in get_services()}
        if service_key not in services:
            return await query.edit_message_text("❌ Service not found.")
        svc = services[service_key]
        conn = db_connect()
        if conn.execute(
            "SELECT 1 FROM claimed_users WHERE user_id=? AND service=?", (uid, service_key)
        ).fetchone():
            conn.close()
            return await query.edit_message_text(
                f"⚠️ You already claimed a *{svc['name']}* cookie!\n\nIf broken, use /start → 🔄 Refund.",
                parse_mode=ParseMode.MARKDOWN,
            )
        conn.close()
        result = pick_cookie(service_key)
        if not result:
            return await query.edit_message_text(
                f"❌ *{svc['name']} is out of stock.*\n\nCheck back later!",
                parse_mode=ParseMode.MARKDOWN,
            )
        account, fpath = result
        move_cookie_to_sent(fpath, service_key)
        conn = db_connect()
        conn.execute(
            "INSERT OR IGNORE INTO claimed_users (user_id,service,claimed_at) VALUES (?,?,?)",
            (uid, service_key, int(time.time())),
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(
            format_account_message(account, service_key, "🎉 *Here's your account!*"),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "refund":
        conn = db_connect()
        claimed = conn.execute(
            "SELECT DISTINCT service FROM claimed_users WHERE user_id=?", (uid,)
        ).fetchall()
        conn.close()
        if not claimed:
            return await query.edit_message_text("❌ You haven't claimed any cookies yet.")
        await query.edit_message_text(
            "🔄 *Refund Request*\n\nWhich service needs a replacement?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔄 {r['service'].title()}", callback_data=f"refund_svc:{r['service']}")]
                for r in claimed
            ]),
        )

    elif data.startswith("refund_svc:"):
        service_key = data.split(":", 1)[1]
        conn = db_connect()
        log = conn.execute(
            "SELECT refunded_at FROM refund_log WHERE user_id=? AND service=?", (uid, service_key)
        ).fetchone()
        conn.close()
        if log and (time.time() - log["refunded_at"]) < 86400:
            return await query.edit_message_text("⏳ One refund per 24 hours per service.")
        set_user_state(uid, "waiting_refund_screenshot", service_key)
        await query.edit_message_text(
            f"🔄 *Refund — {service_key.title()}*\n\n📸 Send a screenshot showing the error.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data.startswith(("approve_refund:", "reject_refund:")):
        if not is_admin(uid):
            return await query.answer("Unauthorized", show_alert=True)
        action, rid = data.split(":", 1)
        rid = int(rid)
        conn = db_connect()
        req = conn.execute("SELECT * FROM refund_requests WHERE id=?", (rid,)).fetchone()
        if not req or req["status"] != "pending":
            conn.close()
            return await query.edit_message_text("⚠️ Already resolved or not found.")
        if action == "approve_refund":
            cookie = pick_cookie(req["service"])
            if not cookie:
                conn.close()
                return await query.edit_message_text("❌ No stock for this service.")
            account, fpath = cookie
            move_cookie_to_sent(fpath, req["service"])
            conn.execute(
                "UPDATE refund_requests SET status='approved',resolved_at=?,resolved_by=? WHERE id=?",
                (int(time.time()), uid, rid),
            )
            conn.execute(
                "INSERT OR REPLACE INTO refund_log (user_id,service,refunded_at) VALUES (?,?,?)",
                (req["user_id"], req["service"], int(time.time())),
            )
            conn.commit()
            conn.close()
            await ctx.bot.send_message(
                req["user_id"],
                format_account_message(account, req["service"], "🎫 *Refund Approved! Replacement:*"),
                parse_mode=ParseMode.MARKDOWN,
            )
            await query.edit_message_text(f"✅ Refund #{rid} approved.")
        else:
            conn.execute(
                "UPDATE refund_requests SET status='rejected',resolved_at=?,resolved_by=? WHERE id=?",
                (int(time.time()), uid, rid),
            )
            conn.commit()
            conn.close()
            await ctx.bot.send_message(
                req["user_id"],
                f"❌ *Refund Rejected*\n\nYour refund for *{req['service'].title()}* was rejected.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await query.edit_message_text(f"❌ Refund #{rid} rejected.")

    elif data.startswith("upload_svc:"):
        if not is_admin(uid):
            return await query.answer("Unauthorized", show_alert=True)
        _, file_id, service_key = data.split(":", 2)
        try:
            file    = await ctx.bot.get_file(file_id)
            content = (await file.download_as_bytearray()).decode("utf-8").strip()
            # For Netflix, validate first
            if service_key == "netflix":
                result = check_netflix_account(content, save_to_file=True)
                if result["valid"]:
                    # Insert into DB
                    conn = db_connect()
                    conn.execute(
                        "INSERT INTO cookie_stock (service,content,added_at,added_by) VALUES (?,?,?,?)",
                        (service_key, content, int(time.time()), uid),
                    )
                    conn.commit()
                    conn.close()
                    await query.edit_message_text(
                        f"✅ *Valid Netflix account added!*\n"
                        f"📧 `{result['email']}`\n"
                        f"{result['flag']} {result['country']}\n"
                        f"🎬 {result['plan']} ({result['max_streams']} screens, {result['video_quality']})\n"
                        f"💳 {result['payment_method']}\n\n"
                        f"📁 Saved as `{result['flag']}[{result['email']}][{result['plan']}].txt`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                else:
                    buttons = InlineKeyboardMarkup([
                        [InlineKeyboardButton("➕ Add anyway", callback_data=f"force_add:{file_id}:{service_key}")],
                        [InlineKeyboardButton("❌ Discard", callback_data="discard_cookie")],
                    ])
                    await query.edit_message_text(
                        f"⚠️ *Cookie appears invalid:* {result.get('error', 'unknown')}\n\nAdd anyway?",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=buttons,
                    )
            else:
                # Non-Netflix: direct DB insertion
                conn = db_connect()
                conn.execute(
                    "INSERT INTO cookie_stock (service,content,added_at,added_by) VALUES (?,?,?,?)",
                    (service_key, content, int(time.time()), uid),
                )
                conn.commit()
                conn.close()
                await query.edit_message_text(
                    f"✅ Cookie added to *{service_key.title()}*!",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")

    elif data.startswith("force_add:"):
        if not is_admin(uid):
            return await query.answer("Unauthorized", show_alert=True)
        _, file_id, service_key = data.split(":", 2)
        try:
            file = await ctx.bot.get_file(file_id)
            content = (await file.download_as_bytearray()).decode("utf-8").strip()
            conn = db_connect()
            conn.execute(
                "INSERT INTO cookie_stock (service,content,added_at,added_by) VALUES (?,?,?,?)",
                (service_key, content, int(time.time()), uid),
            )
            conn.commit()
            conn.close()
            # Also try to save file with basic info if Netflix
            if service_key == "netflix":
                # Run checker just to get email/plan if possible, but don't fail
                result = check_netflix_account(content, save_to_file=True)
            await query.edit_message_text(
                f"✅ Cookie added to *{service_key.title()}* (forced).",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")

    elif data == "discard_cookie":
        await query.edit_message_text("🗑️ Cookie discarded.")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = get_user_state(uid)
    if not state or state[0] != "waiting_refund_screenshot":
        return
    _, service_key = state
    photo_id = update.message.photo[-1].file_id
    conn = db_connect()
    conn.execute(
        "INSERT INTO refund_requests (user_id,service,photo_id,requested_at) VALUES (?,?,?,?)",
        (uid, service_key, photo_id, int(time.time())),
    )
    req_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    clear_user_state(uid)
    await update.message.reply_text(
        "✅ *Refund submitted!* An admin will review it shortly.", parse_mode=ParseMode.MARKDOWN
    )
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_refund:{req_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_refund:{req_id}"),
    ]])
    for adm in get_all_admins():
        try:
            await ctx.bot.send_photo(
                adm, photo=photo_id,
                caption=(
                    f"🔄 *Refund #{req_id}*\n"
                    f"User: `{uid}` | Service: *{service_key.title()}*\n"
                    f"Time: {time.strftime('%Y-%m-%d %H:%M')}"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=buttons,
            )
        except Exception:
            pass

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".txt"):
        return

    caption = (update.message.caption or "").strip().lower()
    svc_keys = [s["key"] for s in get_services()]

    file = await ctx.bot.get_file(doc.file_id)
    content = (await file.download_as_bytearray()).decode("utf-8").strip()

    if caption in svc_keys:
        if caption == "netflix":
            result = check_netflix_account(content, save_to_file=True)
            if result["valid"]:
                conn = db_connect()
                conn.execute(
                    "INSERT INTO cookie_stock (service,content,added_at,added_by) VALUES (?,?,?,?)",
                    (caption, content, int(time.time()), uid),
                )
                conn.commit()
                conn.close()
                await update.message.reply_text(
                    f"✅ *Valid Netflix account added!*\n"
                    f"📧 `{result['email']}`\n"
                    f"{result['flag']} {result['country']}\n"
                    f"🎬 {result['plan']} ({result['max_streams']} screens, {result['video_quality']})\n"
                    f"💳 {result['payment_method']}\n\n"
                    f"📁 Saved as `{result['flag']}[{result['email']}][{result['plan']}].txt`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                buttons = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add anyway", callback_data=f"force_add:{doc.file_id}:{caption}")],
                    [InlineKeyboardButton("❌ Discard", callback_data="discard_cookie")],
                ])
                await update.message.reply_text(
                    f"⚠️ *Cookie appears invalid:* {result.get('error', 'unknown')}\n\nAdd anyway?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=buttons,
                )
        else:
            conn = db_connect()
            conn.execute(
                "INSERT INTO cookie_stock (service,content,added_at,added_by) VALUES (?,?,?,?)",
                (caption, content, int(time.time()), uid),
            )
            conn.commit()
            conn.close()
            await update.message.reply_text(
                f"✅ Cookie stored in *{caption.title()}*!", parse_mode=ParseMode.MARKDOWN
            )
    else:
        await update.message.reply_text(
            "📁 Which service is this cookie for?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{s['emoji']} {s['name']}", callback_data=f"upload_svc:{doc.file_id}:{s['key']}")]
                for s in get_services()
            ]),
        )

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Unauthorized.")
    counts = stock_count()
    conn   = db_connect()
    total  = conn.execute("SELECT COUNT(*) FROM claimed_users").fetchone()[0]
    pend   = conn.execute("SELECT COUNT(*) FROM refund_requests WHERE status='pending'").fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"📊 *Admin Panel*\n\n"
        + " | ".join(f"{s['emoji']} {s['name']}: {counts.get(s['key'],0)}" for s in get_services())
        + f"\n\n👥 Claimed: `{total}` | 🔄 Pending refunds: `{pend}`\n\nUse `/help` for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_gencode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Unauthorized.")
    args = ctx.args
    if not args:
        return await update.message.reply_text("Usage: /gencode <service> [amount]")
    service_key = args[0].lower()
    amount      = min(int(args[1]) if len(args) > 1 else 1, 50)
    if service_key not in [s["key"] for s in get_services()]:
        return await update.message.reply_text(f"❌ Unknown service.")
    conn  = db_connect()
    codes = []
    for _ in range(amount):
        code = generate_redeem_code()
        conn.execute(
            "INSERT OR IGNORE INTO redeem_codes (code,service,created_at) VALUES (?,?,?)",
            (code, service_key, int(time.time())),
        )
        codes.append(code)
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ *{amount} code(s) for {service_key.title()}:*\n\n" + "\n".join(f"• `{c}`" for c in codes),
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_redeem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args
    if not args:
        return await update.message.reply_text("Usage: /redeem <code>")
    code = args[0].upper().strip()
    conn = db_connect()
    row  = conn.execute("SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
    if not row:
        conn.close()
        return await update.message.reply_text(f"❌ Code `{code}` not found.", parse_mode=ParseMode.MARKDOWN)
    if row["used_by"]:
        conn.close()
        return await update.message.reply_text("❌ Code already redeemed.")
    service_key = row["service"] or "netflix"
    if stock_count(service_key) == 0:
        for s in get_services():
            if stock_count(s["key"]) > 0:
                service_key = s["key"]
                break
        else:
            conn.close()
            return await update.message.reply_text("⚠️ No stock available.")
    conn.execute(
        "UPDATE redeem_codes SET used_by=?,used_at=? WHERE code=?",
        (uid, int(time.time()), code),
    )
    conn.commit()
    conn.close()
    result = pick_cookie(service_key)
    if not result:
        return await update.message.reply_text("⚠️ Out of stock!")
    account, fpath = result
    move_cookie_to_sent(fpath, service_key)
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO claimed_users (user_id,service,claimed_at) VALUES (?,?,?)",
        (uid, service_key, int(time.time())),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        format_account_message(account, service_key, "🎉 *Code redeemed!*"),
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_listservices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    counts = stock_count()
    lines  = [f"{s['emoji']} *{s['name']}* (`{s['key']}`) — {counts.get(s['key'],0)} cookies" for s in get_services()]
    await update.message.reply_text("📋 *Services:*\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_addservice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Unauthorized.")
    args = ctx.args
    if len(args) < 3:
        return await update.message.reply_text("Usage: /addservice <key> <name> <emoji>")
    key, name, emoji = args[0].lower(), args[1], args[2]
    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO services (key,name,emoji) VALUES (?,?,?)", (key, name, emoji))
    conn.commit()
    conn.close()
    Path(f"cookies/{key}").mkdir(parents=True, exist_ok=True)
    Path(f"sent/{key}").mkdir(parents=True, exist_ok=True)
    await update.message.reply_text(f"✅ Service *{name}* added!", parse_mode=ParseMode.MARKDOWN)

async def cmd_removeservice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Unauthorized.")
    args = ctx.args
    if not args:
        return await update.message.reply_text("Usage: /removeservice <key>")
    key = args[0].lower()
    if stock_count(key) > 0:
        return await update.message.reply_text("❌ Service still has cookies in stock.")
    conn = db_connect()
    conn.execute("DELETE FROM services WHERE key=?", (key,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Service `{key}` removed.", parse_mode=ParseMode.MARKDOWN)

async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("❌ Unauthorized.")
    args = ctx.args
    if not args:
        return await update.message.reply_text("Usage: /addadmin <user_id>")
    new_id = int(args[0])
    conn   = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO admins (user_id,added_by,added_at) VALUES (?,?,?)",
        (new_id, uid, int(time.time())),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ `{new_id}` added as admin.", parse_mode=ParseMode.MARKDOWN)

async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != MASTER_ADMIN:
        return await update.message.reply_text("❌ Only master admin can remove admins.")
    args = ctx.args
    if not args:
        return await update.message.reply_text("Usage: /removeadmin <user_id>")
    target = int(args[0])
    if target == MASTER_ADMIN:
        return await update.message.reply_text("❌ Cannot remove master admin.")
    conn = db_connect()
    conn.execute("DELETE FROM admins WHERE user_id=?", (target,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Admin `{target}` removed.", parse_mode=ParseMode.MARKDOWN)

async def cmd_listadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    conn  = db_connect()
    rows  = conn.execute("SELECT * FROM admins").fetchall()
    conn.close()
    lines = [f"👑 Master: `{MASTER_ADMIN}`"] + [f"• `{r['user_id']}`" for r in rows]
    await update.message.reply_text("👮 *Admins:*\n\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_refunds(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Unauthorized.")
    conn    = db_connect()
    pending = conn.execute(
        "SELECT * FROM refund_requests WHERE status='pending' ORDER BY requested_at DESC LIMIT 10"
    ).fetchall()
    conn.close()
    if not pending:
        return await update.message.reply_text("✅ No pending refunds.")
    for req in pending:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_refund:{req['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_refund:{req['id']}"),
        ]])
        caption = (
            f"🔄 *Refund #{req['id']}*\nUser: `{req['user_id']}`\n"
            f"Service: *{req['service']}*\nTime: {time.strftime('%Y-%m-%d %H:%M', time.localtime(req['requested_at']))}"
        )
        if req["photo_id"]:
            await update.message.reply_photo(
                req["photo_id"], caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons
            )
        else:
            await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons)

async def cmd_checkcookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("❌ Unauthorized.")
    now = time.time()
    checker_rate.setdefault(uid, [])
    checker_rate[uid] = [t for t in checker_rate[uid] if now - t < 60]
    if len(checker_rate[uid]) >= 10:
        return await update.message.reply_text("⏳ Rate limit: 10 checks/minute.")
    checker_rate[uid].append(now)

    # If replying to a document, check it
    if update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        if not doc.file_name.endswith(".txt"):
            return await update.message.reply_text("❌ Reply to a .txt cookie file.")
        file = await ctx.bot.get_file(doc.file_id)
        content = (await file.download_as_bytearray()).decode("utf-8").strip()
        result = check_netflix_account(content, save_to_file=False)  # don't save during check
        if result["valid"]:
            msg = (
                f"✅ *Valid Netflix Account*\n"
                f"📧 `{result['email']}`\n"
                f"{result['flag']} {result['country']}\n"
                f"🎬 {result['plan']} | {result['max_streams']} screens | {result['video_quality']}\n"
                f"💳 {result['payment_method']}"
            )
        else:
            msg = f"❌ *Invalid* — {result.get('error', 'unknown')}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("📎 Reply to a .txt cookie file with /checkcookie to validate it.")

# ─── PTB Application builder ─────────────────────────────────────────────────────
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("admin",         cmd_admin))
    app.add_handler(CommandHandler("gencode",       cmd_gencode))
    app.add_handler(CommandHandler("redeem",        cmd_redeem))
    app.add_handler(CommandHandler("listservices",  cmd_listservices))
    app.add_handler(CommandHandler("addservice",    cmd_addservice))
    app.add_handler(CommandHandler("removeservice", cmd_removeservice))
    app.add_handler(CommandHandler("addadmin",      cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin",   cmd_removeadmin))
    app.add_handler(CommandHandler("listadmins",    cmd_listadmins))
    app.add_handler(CommandHandler("refunds",       cmd_refunds))
    app.add_handler(CommandHandler("checkcookie",   cmd_checkcookie))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    return app

# ─── Polling runner (background thread) ─────────────────────────────────────────
def _run_polling():
    logger.info("Polling thread starting…")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ptb = build_application()

    async def _run():
        async with ptb:
            await ptb.initialize()
            await ptb.start()
            await ptb.updater.start_polling(drop_pending_updates=True)
            logger.info("✅ Polling bot is running.")
            await asyncio.Event().wait()  # block forever

    try:
        loop.run_until_complete(_run())
    except Exception as e:
        logger.error(f"Polling thread crashed: {e}")

# ─── Flask admin panel ───────────────────────────────────────────────────────────
flask_app = Flask(__name__)

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if key != ADMIN_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@flask_app.route("/")
def serve_panel():
    return send_from_directory(BASE_DIR, "admin_panel.html")

@flask_app.route("/api/auth", methods=["POST"])
def api_auth():
    body = request.get_json(silent=True) or {}
    if body.get("key") == ADMIN_API_KEY:
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401

@flask_app.route("/api/stats")
@require_auth
def api_stats():
    counts = stock_count()
    conn   = db_connect()
    total_claimed   = conn.execute("SELECT COUNT(*) FROM claimed_users").fetchone()[0]
    pending_refunds = conn.execute("SELECT COUNT(*) FROM refund_requests WHERE status='pending'").fetchone()[0]
    total_codes     = conn.execute("SELECT COUNT(*) FROM redeem_codes").fetchone()[0]
    used_codes      = conn.execute("SELECT COUNT(*) FROM redeem_codes WHERE used_by IS NOT NULL").fetchone()[0]
    db_cookies_total = conn.execute("SELECT COUNT(*) FROM cookie_stock").fetchone()[0]
    db_cookies_avail = conn.execute("SELECT COUNT(*) FROM cookie_stock WHERE claimed_at IS NULL").fetchone()[0]
    conn.close()
    return jsonify({
        "stock":           counts,
        "total_claimed":   total_claimed,
        "pending_refunds": pending_refunds,
        "total_codes":     total_codes,
        "used_codes":      used_codes,
        "db_cookies":      {"total": db_cookies_total, "available": db_cookies_avail},
        "services":        get_services(),
    })

@flask_app.route("/api/services", methods=["GET"])
@require_auth
def api_list_services():
    counts = stock_count()
    svcs   = get_services()
    for s in svcs:
        s["stock"] = counts.get(s["key"], 0)
    return jsonify(svcs)

@flask_app.route("/api/services", methods=["POST"])
@require_auth
def api_add_service():
    body  = request.get_json(silent=True) or {}
    key   = body.get("key", "").strip().lower()
    name  = body.get("name", "").strip()
    emoji = body.get("emoji", "🍪").strip()
    if not key or not name:
        return jsonify({"error": "key and name required"}), 400
    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO services (key,name,emoji) VALUES (?,?,?)", (key, name, emoji))
    conn.commit()
    conn.close()
    Path(f"cookies/{key}").mkdir(parents=True, exist_ok=True)
    Path(f"sent/{key}").mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True})

@flask_app.route("/api/services/<key>", methods=["DELETE"])
@require_auth
def api_remove_service(key):
    if stock_count(key) > 0:
        return jsonify({"error": "Service still has cookies in stock"}), 400
    conn = db_connect()
    conn.execute("DELETE FROM services WHERE key=?", (key,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@flask_app.route("/api/cookies", methods=["POST"])
@require_auth
def api_add_cookie():
    body    = request.get_json(silent=True) or {}
    service = body.get("service", "").strip().lower()
    content = body.get("content", "").strip()
    if not service or not content:
        return jsonify({"error": "service and content required"}), 400
    # For Netflix, validate first (optional via API, but we'll just insert)
    conn = db_connect()
    conn.execute(
        "INSERT INTO cookie_stock (service,content,added_at,added_by) VALUES (?,?,?,?)",
        (service, content, int(time.time()), 0),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@flask_app.route("/api/codes", methods=["GET"])
@require_auth
def api_list_codes():
    service = request.args.get("service", "")
    status  = request.args.get("status", "")   # "used" | "unused" | ""
    page    = max(1, int(request.args.get("page", 1)))
    limit   = 50
    offset  = (page - 1) * limit
    where, params = [], []
    if service:
        where.append("service=?");  params.append(service)
    if status == "used":
        where.append("used_by IS NOT NULL")
    elif status == "unused":
        where.append("used_by IS NULL")
    sql    = "SELECT * FROM redeem_codes"
    count_sql = "SELECT COUNT(*) FROM redeem_codes"
    if where:
        cond = " WHERE " + " AND ".join(where)
        sql      += cond
        count_sql += cond
    sql += f" ORDER BY created_at DESC LIMIT {limit} OFFSET {offset}"
    conn  = db_connect()
    total = conn.execute(count_sql, params).fetchone()[0]
    rows  = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify({
        "total": total,
        "page":  page,
        "items": [dict(r) for r in rows],
    })

@flask_app.route("/api/codes", methods=["POST"])
@require_auth
def api_generate_codes():
    body    = request.get_json(silent=True) or {}
    service = body.get("service", "").strip().lower()
    amount  = min(int(body.get("amount", 1)), 100)
    if service not in [s["key"] for s in get_services()]:
        return jsonify({"error": "Unknown service"}), 400
    conn  = db_connect()
    codes = []
    for _ in range(amount):
        code = generate_redeem_code()
        conn.execute(
            "INSERT OR IGNORE INTO redeem_codes (code,service,created_at) VALUES (?,?,?)",
            (code, service, int(time.time())),
        )
        codes.append(code)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "codes": codes})

@flask_app.route("/api/refunds", methods=["GET"])
@require_auth
def api_list_refunds():
    status = request.args.get("status", "pending")
    page   = max(1, int(request.args.get("page", 1)))
    limit  = 20
    offset = (page - 1) * limit
    conn   = db_connect()
    if status == "all":
        rows  = conn.execute(
            f"SELECT * FROM refund_requests ORDER BY requested_at DESC LIMIT {limit} OFFSET {offset}"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM refund_requests").fetchone()[0]
    else:
        rows  = conn.execute(
            f"SELECT * FROM refund_requests WHERE status=? ORDER BY requested_at DESC LIMIT {limit} OFFSET {offset}",
            (status,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM refund_requests WHERE status=?", (status,)
        ).fetchone()[0]
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d["requested_at_fmt"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["requested_at"])) if r["requested_at"] else ""
        items.append(d)
    return jsonify({"total": total, "items": items})

@flask_app.route("/api/refunds/<int:rid>/<action>", methods=["POST"])
@require_auth
def api_resolve_refund(rid, action):
    if action not in ("approve", "reject"):
        return jsonify({"error": "Invalid action"}), 400
    conn = db_connect()
    req  = conn.execute("SELECT * FROM refund_requests WHERE id=?", (rid,)).fetchone()
    if not req:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if req["status"] != "pending":
        conn.close()
        return jsonify({"error": "Already resolved"}), 400
    conn.execute(
        "UPDATE refund_requests SET status=?,resolved_at=?,resolved_by=0 WHERE id=?",
        (action + "d", int(time.time()), rid),
    )
    if action == "approve":
        conn.execute(
            "INSERT OR REPLACE INTO refund_log (user_id,service,refunded_at) VALUES (?,?,?)",
            (req["user_id"], req["service"], int(time.time())),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@flask_app.route("/api/admins", methods=["GET"])
@require_auth
def api_list_admins():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM admins ORDER BY added_at DESC").fetchall()
    conn.close()
    return jsonify({
        "master": MASTER_ADMIN,
        "admins": [dict(r) for r in rows],
    })

@flask_app.route("/api/admins", methods=["POST"])
@require_auth
def api_add_admin():
    body = request.get_json(silent=True) or {}
    uid  = body.get("user_id")
    if not uid:
        return jsonify({"error": "user_id required"}), 400
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO admins (user_id,added_by,added_at) VALUES (?,?,?)",
        (int(uid), 0, int(time.time())),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@flask_app.route("/api/admins/<int:uid>", methods=["DELETE"])
@require_auth
def api_remove_admin(uid):
    if uid == MASTER_ADMIN:
        return jsonify({"error": "Cannot remove master admin"}), 400
    conn = db_connect()
    conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@flask_app.route("/api/claimed", methods=["GET"])
@require_auth
def api_claimed():
    page   = max(1, int(request.args.get("page", 1)))
    limit  = 50
    offset = (page - 1) * limit
    conn   = db_connect()
    total  = conn.execute("SELECT COUNT(*) FROM claimed_users").fetchone()[0]
    rows   = conn.execute(
        f"SELECT * FROM claimed_users ORDER BY claimed_at DESC LIMIT {limit} OFFSET {offset}"
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d["claimed_at_fmt"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["claimed_at"])) if r["claimed_at"] else ""
        items.append(d)
    return jsonify({"total": total, "items": items})

# Telegram webhook route (when USE_WEBHOOK=true)
@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    if not USE_WEBHOOK:
        return "webhook not enabled", 404
    data = request.get_json(force=True)
    app  = build_application()
    asyncio.run(_process_webhook(app, data))
    return "ok", 200

async def _process_webhook(app, data):
    update = Update.de_json(data, app.bot)
    async with app:
        await app.process_update(update)

# ─── Entry point ─────────────────────────────────────────────────────────────────
def main():
    init_db()
    ensure_dirs()

    if USE_WEBHOOK and WEBHOOK_URL:
        full_url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        logger.info(f"Webhook mode → registering {full_url}")
        import requests as _req
        try:
            _req.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": full_url, "drop_pending_updates": True},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Could not register webhook: {e}")
        logger.info(f"Flask starting on port {PORT} (webhook + admin panel)")
    else:
        # Polling bot in background thread
        poll_thread = threading.Thread(target=_run_polling, daemon=True)
        poll_thread.start()
        logger.info(f"Admin panel starting on port {PORT}")

    logger.info(f"Admin panel → http://0.0.0.0:{PORT}/")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()