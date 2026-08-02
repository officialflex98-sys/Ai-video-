"""
Polls Telegram for a new script.txt file sent by the authorized user, and
if found, overwrites the local script.txt with it. Meant to be run by a
scheduled GitHub Actions workflow, which then commits and pushes the
updated file - triggering the existing Build Video workflow automatically,
since it already watches script.txt for changes.

Only accepts DOCUMENT uploads (not typed messages), since Telegram caps
plain text messages at 4096 characters - far too small for a full
documentary or commentary script.

Requires: pip install requests
Env vars: TELEGRAM_BOT_TOKEN (required)
          TELEGRAM_CHAT_ID   (required - only messages from this exact
                              chat ID are accepted, so no one else can
                              hijack the pipeline by messaging the bot)
"""

import os
import sys
import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET_FILE = ".telegram_offset"
SCRIPT_PATH = "script.txt"


def _load_offset() -> int:
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE, "r") as f:
            return int(f.read().strip() or 0)
    return 0


def _save_offset(offset: int):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def _send_reply(chat_id: str, text: str):
    try:
        requests.post(f"{API_BASE}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=15)
    except requests.RequestException as e:
        print(f"  [warn] failed to send Telegram reply: {e}")


def sync_script_from_telegram() -> bool:
    if not BOT_TOKEN or not AUTHORIZED_CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")

    offset = _load_offset()
    resp = requests.get(f"{API_BASE}/getUpdates",
                         params={"offset": offset + 1, "timeout": 0}, timeout=30)
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    if not updates:
        print("No new Telegram messages.")
        return False

    updated_script = False
    latest_offset = offset

    for update in updates:
        update_id = update.get("update_id", offset)
        latest_offset = max(latest_offset, update_id)

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id != str(AUTHORIZED_CHAT_ID):
            print(f"  [skip] message from unauthorized chat_id {chat_id}")
            continue

        document = message.get("document")
        if not document:
            _send_reply(chat_id,
                        "Please send your script as a .txt file attachment, "
                        "not a typed message (Telegram limits typed messages "
                        "to 4096 characters, too small for a full script).")
            continue

        file_id = document.get("file_id")
        file_name = document.get("file_name", "")
        if not file_name.lower().endswith(".txt"):
            _send_reply(chat_id, f"Received '{file_name}' - only .txt files are accepted, ignoring.")
            continue

        file_info_resp = requests.get(f"{API_BASE}/getFile", params={"file_id": file_id}, timeout=15)
        file_info_resp.raise_for_status()
        file_path = file_info_resp.json()["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        file_resp = requests.get(file_url, timeout=30)
        file_resp.raise_for_status()

        with open(SCRIPT_PATH, "wb") as f:
            f.write(file_resp.content)

        print(f"  Wrote new {SCRIPT_PATH} from Telegram document '{file_name}' "
              f"({len(file_resp.content)} bytes)")
        _send_reply(chat_id, f"Got it — script.txt updated ({len(file_resp.content)} bytes). "
                             f"Video generation will start shortly.")
        updated_script = True

    _save_offset(latest_offset)
    return updated_script


if __name__ == "__main__":
    changed = sync_script_from_telegram()
    # Exit code signals to the workflow whether a commit is needed.
    sys.exit(0 if changed else 1)
