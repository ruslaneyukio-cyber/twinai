import json
import os
import sys
import time
import urllib.parse
import urllib.request
import ssl
import certifi

API_URL = "https://api.telegram.org/bot{token}/{method}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://taskboard-tg-miniapp-ruslan.windsurf.build")
# Default API base to Render prod backend; env can override
API_BASE = os.getenv("API_BASE", "https://twinai-0yz2.onrender.com")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

print(f"[bot] starting with FRONTEND_URL={FRONTEND_URL} API_BASE={API_BASE}")
sys.stdout.flush()


def tg_request(method: str, params: dict):
    url = API_URL.format(token=TELEGRAM_BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    # Use certifi CA bundle to avoid SSL chain issues
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        body = resp.read()
        return json.loads(body)


def _build_webapp_url() -> str:
    v = int(time.time())
    base = FRONTEND_URL.rstrip("/")
    # Force /index.html to avoid root redirects/caching
    # Use force_api to override API inside Telegram Mini App even when it forces default
    webapp_url = f"{base}/index.html?force_api={urllib.parse.quote(API_BASE, safe='')}&v={v}"
    return webapp_url


def send_start(chat_id: int):
    webapp_url = _build_webapp_url()
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "Open TaskBoard",
                    "web_app": {"url": webapp_url},
                }
            ]
        ]
    }
    print(f"[bot] send_start to chat {chat_id}")
    sys.stdout.flush()
    tg_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                "👋 Добро пожаловать в TaskBoard!\n\n"
                "Это мини‑приложение внутри Telegram для обмена заданиями и монетами.\n"
                "• Размещай задания и замораживай монеты\n"
                "• Бери задачи, выполняй и получай монеты\n"
                "• Подтверждай/отклоняй работу, веди профиль и баланс\n\n"
                "Нажми кнопку ниже, чтобы открыть TaskBoard."
            ),
            "reply_markup": json.dumps(reply_markup),
        },
    )


def send_help(chat_id: int):
    webapp_url = _build_webapp_url()
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Open TaskBoard", "web_app": {"url": webapp_url}}
            ]
        ]
    }
    tg_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                "ℹ️ Подсказка:\n"
                "— /start: отправить кнопку для запуска мини‑аппы\n"
                "— /help: показать это сообщение\n\n"
                "Если TaskBoard не открывается, попробуй закрыть окно мини‑аппы и нажать кнопку ещё раз."
            ),
            "reply_markup": json.dumps(reply_markup),
        },
    )


def main():
    offset = None
    print("[bot] polling getUpdates...")
    sys.stdout.flush()
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            url = API_URL.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
            if params:
                url += "?" + urllib.parse.urlencode(params)
            # getUpdates with certifi-based SSL context
            ctx = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(url, timeout=30, context=ctx) as resp:
                data = json.loads(resp.read())
            if not data.get("ok"):
                print("[bot] getUpdates not ok", data)
                sys.stdout.flush()
                time.sleep(1)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                print("[bot] update:", json.dumps(upd, ensure_ascii=False))
                sys.stdout.flush()
                msg = upd.get("message") or upd.get("channel_post")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                text = (msg.get("text") or "").strip()
                if text.startswith("/start") or text in {"/menu", "/app", "/open"}:
                    send_start(chat_id)
                elif text.startswith("/help"):
                    send_help(chat_id)
                else:
                    # Friendly fallback with the button
                    send_help(chat_id)
        except Exception as e:
            print("[bot] error:", repr(e))
            sys.stdout.flush()
            # brief backoff then retry
            time.sleep(1)


if __name__ == "__main__":
    main()
