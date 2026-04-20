"""
Vercel serverless entry point.
Telegram calls POST /api/webhook with each update.

Requirements:
  - Set BOT_TOKEN and ADMIN_ID in Vercel environment variables.
  - Run once locally:  python set_webhook.py
    to register this URL with Telegram.

⚠️  Vercel's filesystem is ephemeral — SQLite data resets on cold starts.
    For persistent data use Railway or Fly.io (see README.md).
    To keep data on Vercel, set DB_PATH to a mounted volume path or
    migrate to a hosted Postgres (Neon / Supabase) with a custom db layer.
"""

import sys
import os
import json
import asyncio
import logging

# Make the project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update
from bot import build_application, init_db, ensure_dirs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── One-time init per warm instance ──────────────────────────────────────────────
init_db()
ensure_dirs()
_ptb_app = build_application()


async def _process(update_data: dict):
    update = Update.de_json(update_data, _ptb_app.bot)
    async with _ptb_app:
        await _ptb_app.process_update(update)


# ── Vercel handler (BaseHTTPRequestHandler format) ───────────────────────────────
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default access log

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Stream Cookie Bot is running!")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            data   = json.loads(body)
            asyncio.run(_process(data))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as e:
            logger.error("Webhook error: %s", e)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())
