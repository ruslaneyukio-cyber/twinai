import json
import os
import time
import urllib.parse
import urllib.request

API_URL = "https://api.telegram.org/bot{token}/{method}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://taskboard-tg-miniapp-ruslan.windsurf.build")
API_BASE = os.getenv("API_BASE", "https://taken-pennsylvania-enables-cancel.trycloudflare.com")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")


def tg_request(method: str, params: dict):
    url = API_URL.format(token=TELEGRAM_BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body)


def send_start(chat_id: int):
    webapp_url = f"{FRONTEND_URL}?api={urllib.parse.quote(API_BASE, safe='')}"
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
    webapp_url = f"{FRONTEND_URL}?api={urllib.parse.quote(API_BASE, safe='')}"
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
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            url = API_URL.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
            if params:
                url += "?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read())
            if not data.get("ok"):
                time.sleep(1)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
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
        except Exception:
            # brief backoff then retry
            time.sleep(1)


if __name__ == "__main__":
    main()
