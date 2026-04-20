"""
Run this script once after deploying to register your webhook URL with Telegram.

Usage:
    python set_webhook.py https://your-app.vercel.app/api/webhook
    python set_webhook.py https://your-app.railway.app/webhook
"""

import sys
import os
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN") or input("Enter BOT_TOKEN: ").strip()

if len(sys.argv) < 2:
    url = input("Enter your full webhook URL (e.g. https://my-bot.vercel.app/api/webhook): ").strip()
else:
    url = sys.argv[1]

resp = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
    json={"url": url, "drop_pending_updates": True},
)
print(resp.json())
