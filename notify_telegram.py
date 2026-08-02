"""
Sends a Telegram message with a download link to the finished video.
Run at the end of the Build Video workflow, after the video has been
uploaded as a GitHub Release asset.

Requires: pip install requests
Env vars: TELEGRAM_BOT_TOKEN (required)
          TELEGRAM_CHAT_ID   (required)
          DOWNLOAD_URL       (required - passed in from the workflow after
                              the GitHub Release asset URL is known)
"""

import os
import sys
import requests
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "")


def send_telegram_notification(download_url: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
    if not download_url:
        raise SystemExit("DOWNLOAD_URL was not provided.")

    message = (
        f"🎬 Video generated and ready!\n\n"
        f"📥 Download: {download_url}\n\n"
        f"⏰ Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=15,
    )
    resp.raise_for_status()
    print("Telegram notification sent.")


if __name__ == "__main__":
    send_telegram_notification(DOWNLOAD_URL)
