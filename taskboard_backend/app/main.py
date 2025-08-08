from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from typing import Dict, Optional
from queue import Queue
import threading
from datetime import datetime
import os
import hmac
import hashlib
import json
import time
from urllib.parse import parse_qsl, unquote
import urllib.request
import urllib.parse

try:
    # optional: load .env if present
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

# In-memory stores
USERS: Dict[int, dict] = {}
TASKS: Dict[int, dict] = {}
BALANCES: Dict[int, dict] = {}
TASK_COUNTER = 1

# --- Simple in-process pub/sub for SSE ---
SUBSCRIBERS: Dict[int, Queue] = {}
SUBSCRIBERS_LOCK = threading.Lock()
SUB_ID_SEQ = 1

def _sse_format(event: str, data: dict) -> str:
    import json as _json
    return f"event: {event}\n" + f"data: {_json.dumps(data, ensure_ascii=False)}\n\n"

def _publish(event: str, data: dict):
    with SUBSCRIBERS_LOCK:
        qs = list(SUBSCRIBERS.values())
    payload = _sse_format(event, data)
    for q in qs:
        try:
            q.put_nowait(payload)
        except Exception:
            pass

@app.get("/events")
def sse_events():
    global SUB_ID_SEQ
    q: Queue = Queue(maxsize=100)
    with SUBSCRIBERS_LOCK:
        sid = SUB_ID_SEQ
        SUB_ID_SEQ += 1
        SUBSCRIBERS[sid] = q

    def gen():
        try:
            # initial hello
            yield _sse_format("hello", {"ok": True})
            # keepalive loop
            import time as _time
            last_ping = _time.time()
            while True:
                try:
                    item = q.get(timeout=10)
                    yield item
                except Exception:
                    # keepalive ping every ~20s
                    now = _time.time()
                    if now - last_ping > 20:
                        last_ping = now
                        yield _sse_format("ping", {"ts": int(now)})
        finally:
            with SUBSCRIBERS_LOCK:
                SUBSCRIBERS.pop(sid, None)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(gen()), headers=headers)


def get_user_id_from_token(token: str) -> int:
    try:
        return int(token.split(":")[0])
    except Exception:
        raise ValueError("Invalid token")


def verify_telegram_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """
    Verify Telegram WebApp initData using HMAC-SHA256 according to Telegram docs.
    Returns parsed data dict (including 'user') if valid; otherwise None.
    """
    try:
        # init_data is a URL-encoded query string
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in params:
            return None
        received_hash = params.pop("hash")

        # Build data_check_string: sort by key, join as key=value with newline
        data_check_pairs = []
        for k in sorted(params.keys()):
            v = params[k]
            # values may be URL-encoded JSON strings (e.g., user)
            data_check_pairs.append(f"{k}={v}")
        data_check_string = "\n".join(data_check_pairs)

        secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
        h = hmac.new(secret_key, msg=data_check_string.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()
        if h != received_hash:
            return None

        # Optional timestamp check (24h)
        auth_date = int(params.get("auth_date", "0"))
        if auth_date and time.time() - auth_date > 24 * 3600:
            return None

        # Parse user JSON if available
        user_raw = params.get("user")
        user_obj = None
        if user_raw:
            try:
                user_obj = json.loads(unquote(user_raw))
            except Exception:
                user_obj = None

        params["user"] = user_obj
        params["hash"] = received_hash
        return params
    except Exception:
        return None


def _send_telegram_message(chat_id: int, text: str) -> None:
    """Send message via Bot API if SEND_NOTIFICATIONS env is truthy."""
    try:
        if not os.getenv("SEND_NOTIFICATIONS"):
            return
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            return
        api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(api_url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=5) as resp:
            _ = resp.read()
    except Exception:
        # Silent fail to avoid breaking main flow
        pass


def _augment_task_names(t: dict) -> dict:
    """Return a copy of task with customer_name and performer_name added if available."""
    out = dict(t)
    cid = out.get("customer_id")
    pid = out.get("performer_id")
    if cid in USERS:
        out["customer_name"] = USERS[cid]["profile"].get("name")
        out["customer_username"] = USERS[cid]["profile"].get("username")
    if pid and (pid in USERS):
        out["performer_name"] = USERS[pid]["profile"].get("name")
        out["performer_username"] = USERS[pid]["profile"].get("username")
    return out


@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.get("/")
def root():
    return jsonify({"service": "TaskBoard API", "ok": True, "endpoints": ["/health", "/auth/telegram", "/tasks"]})


@app.post("/auth/telegram")
def auth_telegram():
    data = request.get_json(silent=True) or {}
    # Accept initData from JSON, form, query, or raw body fallback
    init_data = (
        data.get("initData")
        or request.form.get("initData")
        or request.args.get("initData")
    )
    if not init_data and request.data:
        try:
            import json as _json
            raw = _json.loads(request.data.decode("utf-8"))
            init_data = raw.get("initData")
        except Exception:
            init_data = None
    if not init_data:
        return jsonify({"detail": "initData required"}), 400

    # Prefer real Telegram verification when hash is present
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    user_id = None
    name = None
    username = None
    verified = None
    if bot_token and ("hash=" in init_data):
        verified = verify_telegram_init_data(init_data, bot_token)
    if verified:
        u = verified.get("user") or {}
        user_id = int(u.get("id")) if u and u.get("id") is not None else None
        name = (u.get("first_name") or "") + (" " + u.get("last_name") if u.get("last_name") else "")
        name = name.strip() or (u.get("username") or (f"User{user_id}" if user_id else "User"))
        username = u.get("username")
    else:
        # If HMAC verification failed but init_data is a Telegram querystring, try to parse user JSON as a soft fallback
        try:
            params = dict(parse_qsl(init_data, keep_blank_values=True))
            user_raw = params.get("user")
            if user_raw:
                u = json.loads(unquote(user_raw))
                user_id = int(u.get("id")) if u and u.get("id") is not None else None
                name = (u.get("first_name") or "") + (" " + u.get("last_name") if u.get("last_name") else "")
                name = name.strip() or (u.get("username") or (f"User{user_id}" if user_id else "User"))
                username = u.get("username")
            else:
                raise ValueError("no user in initData")
        except Exception:
            # Fallback demo format: "<uid>:<name>:<username>"
            try:
                parts = init_data.split(":")
                user_id = int(parts[0])
                name = parts[1] if len(parts) > 1 else (f"User{user_id}")
                username = parts[2] if len(parts) > 2 else None
            except Exception:
                return jsonify({"detail": "Invalid initData format"}), 400
    if user_id not in USERS:
        USERS[user_id] = {"profile": {
            "name": name,
            "username": username,
            "avatar": None,
            "rating": 5.0,
            "created_tasks": 0,
            "finished_tasks": 0,
        }}
    if user_id not in BALANCES:
        BALANCES[user_id] = {"balance": 1000, "history": []}
    token = f"{user_id}:{int(datetime.utcnow().timestamp())}"
    return jsonify({"token": token})


@app.get("/tasks")
def list_tasks():
    category = request.args.get("category")
    price_min = request.args.get("price_min", type=int)
    price_max = request.args.get("price_max", type=int)
    sort = request.args.get("sort")
    items = list(TASKS.values())
    if category:
        items = [t for t in items if t.get("category") == category]
    if price_min is not None:
        items = [t for t in items if int(t.get("price", 0)) >= price_min]
    if price_max is not None:
        items = [t for t in items if int(t.get("price", 0)) <= price_max]
    if sort == "new":
        items.sort(key=lambda x: x.get("id", 0), reverse=True)
    elif sort == "price":
        items.sort(key=lambda x: x.get("price", 0), reverse=True)
    elif sort == "rating":
        items.sort(key=lambda x: x.get("customer_rating", 0), reverse=True)
    items = [_augment_task_names(t) for t in items]
    return jsonify({"items": items})


@app.get("/tasks/<int:task_id>")
def get_task(task_id: int):
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"detail": "Task not found", "id": task_id}), 404
    return jsonify(_augment_task_names(t))


@app.post("/tasks")
def create_task():
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        user_id = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    body = request.get_json(force=True, silent=True) or {}
    title = body.get("title")
    description = body.get("description")
    price = int(body.get("price", 0))
    category = body.get("category")
    if not title or not description or not category or price < 1:
        return jsonify({"detail": "Invalid payload"}), 400
    global TASK_COUNTER
    b = BALANCES.setdefault(user_id, {"balance": 0, "history": []})
    if b["balance"] < price:
        return jsonify({"detail": "Insufficient balance"}), 400
    b["balance"] -= price
    b["history"].append({"type": "freeze", "amount": price, "ts": datetime.utcnow().isoformat(), "task_id": TASK_COUNTER})
    task = {
        "id": TASK_COUNTER,
        "title": title,
        "description": description,
        "price": price,
        "category": category,
        "customer_id": user_id,
        "customer_rating": 5.0,
        "status": "free",
        "performer_id": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    TASKS[TASK_COUNTER] = task
    # increment created counter safely
    try:
        USERS.setdefault(user_id, {"profile": {}})
        prof = USERS[user_id].setdefault("profile", {})
        prof["created_tasks"] = int(prof.get("created_tasks", 0)) + 1
    except Exception:
        pass
    TASK_COUNTER += 1
    USERS[user_id]["profile"]["created_tasks"] = USERS[user_id]["profile"].get("created_tasks", 0) + 1
    try:
        _send_telegram_message(user_id, f"✅ Задание создано: <b>{title}</b> на {price} монет")
    except Exception:
        pass
    at = _augment_task_names(task)
    _publish("task", {"action": "created", "task": at})
    _publish("balance", {"user_id": user_id, "balance": b["balance"]})
    return jsonify(at)


@app.post("/tasks/<int:task_id>/take")
def take_task(task_id: int):
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"detail": "Task not found"}), 404
    # Prevent customer from taking their own task
    if t.get("customer_id") == uid:
        return jsonify({"detail": "Cannot take own task"}), 403
    if t["status"] != "free":
        return jsonify({"detail": "Task not available"}), 400
    t["status"] = "taken"
    t["performer_id"] = uid
    try:
        _send_telegram_message(t["customer_id"], f"🛠 Исполнитель взял задание #{task_id}")
    except Exception:
        pass
    at = _augment_task_names(t)
    _publish("task", {"action": "updated", "task": at})
    return jsonify(at)


@app.post("/tasks/<int:task_id>/complete")
def complete_task(task_id: int):
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"detail": "Task not found"}), 404
    if t.get("performer_id") != uid:
        return jsonify({"detail": "Not your task"}), 403
    if t["status"] != "taken":
        return jsonify({"detail": "Invalid status"}), 400
    body = request.get_json(force=True, silent=True) or {}
    t["status"] = "completed"
    t["result_text"] = body.get("result_text", "")
    try:
        _send_telegram_message(t["customer_id"], f"📦 Работа по заданию #{task_id} отмечена как выполненная")
    except Exception:
        pass
    at = _augment_task_names(t)
    _publish("task", {"action": "updated", "task": at})
    return jsonify(at)


@app.post("/tasks/<int:task_id>/confirm")
def confirm_task(task_id: int):
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"detail": "Task not found"}), 404
    if t["customer_id"] != uid:
        return jsonify({"detail": "Only customer can confirm"}), 403
    if t["status"] != "completed":
        return jsonify({"detail": "Invalid status"}), 400
    t["status"] = "confirmed"
    performer_id = t.get("performer_id")
    if performer_id is None:
        return jsonify({"detail": "No performer"}), 400
    # transfer funds to performer
    pb = BALANCES.setdefault(performer_id, {"balance": 0, "history": []})
    pb["balance"] += t["price"]
    # increment finished counter for performer
    try:
        USERS.setdefault(performer_id, {"profile": {}})
        pprof = USERS[performer_id].setdefault("profile", {})
        pprof["finished_tasks"] = int(pprof.get("finished_tasks", 0)) + 1
    except Exception:
        pass
    pb["history"].append({"type": "transfer", "amount": t["price"], "ts": datetime.utcnow().isoformat(), "task_id": task_id})
    if performer_id in USERS:
        USERS[performer_id]["profile"]["finished_tasks"] = USERS[performer_id]["profile"].get("finished_tasks", 0) + 1
    try:
        _send_telegram_message(performer_id, f"🎉 Оплата за задание #{task_id} зачислена: {t['price']} монет")
    except Exception:
        pass
    at = _augment_task_names(t)
    _publish("task", {"action": "updated", "task": at})
    _publish("balance", {"user_id": performer_id, "balance": pb["balance"]})
    return jsonify(at)


@app.post("/tasks/<int:task_id>/reject")
def reject_task(task_id: int):
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"detail": "Task not found"}), 404
    if t["customer_id"] != uid:
        return jsonify({"detail": "Only customer can reject"}), 403
    if t["status"] not in ("taken", "completed"):
        return jsonify({"detail": "Invalid status"}), 400
    t["status"] = "rejected"
    cb = BALANCES.setdefault(uid, {"balance": 0, "history": []})
    cb["balance"] += t["price"]
    cb["history"].append({"type": "return", "amount": t["price"], "ts": datetime.utcnow().isoformat(), "task_id": task_id})
    try:
        performer = t.get("performer_id")
        if performer:
            _send_telegram_message(performer, f"⚠️ Задание #{task_id} отклонено заказчиком, средства возвращены заказчику")
    except Exception:
        pass
    at = _augment_task_names(t)
    _publish("task", {"action": "updated", "task": at})
    _publish("balance", {"user_id": uid, "balance": cb["balance"]})
    return jsonify(at)


@app.get("/balance")
def get_balance():
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    b = BALANCES.setdefault(uid, {"balance": 0, "history": []})
    return jsonify({"balance": b["balance"], "history": b["history"]})


@app.post("/balance/add")
def balance_add():
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    body = request.get_json(force=True, silent=True) or {}
    amount = int(body.get("amount", 0))
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    if amount < 1:
        return jsonify({"detail": "Invalid amount"}), 400
    b = BALANCES.setdefault(uid, {"balance": 0, "history": []})
    b["balance"] += amount
    b["history"].append({"type": "add", "amount": amount, "ts": datetime.utcnow().isoformat()})
    return jsonify({"balance": b["balance"], "history": b["history"]})


@app.post("/balance/withdraw")
def balance_withdraw():
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    body = request.get_json(force=True, silent=True) or {}
    amount = int(body.get("amount", 0))
    b = BALANCES.setdefault(uid, {"balance": 0, "history": []})
    b["history"].append({"type": "withdraw_stub", "amount": amount, "ts": datetime.utcnow().isoformat()})
    return jsonify({"balance": b["balance"], "history": b["history"]})


@app.get("/profile")
def get_profile():
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    profile = USERS.setdefault(uid, {"profile": {"name": f"User{uid}", "rating": 5.0, "created_tasks": 0, "finished_tasks": 0}})["profile"]
    return jsonify(profile)


@app.post("/profile")
def update_profile():
    token = request.args.get("token") or request.headers.get("X-Token") or request.form.get("token")
    if not token:
        return jsonify({"detail": "token required"}), 401
    try:
        uid = get_user_id_from_token(token)
    except ValueError:
        return jsonify({"detail": "Invalid token"}), 401
    body = request.get_json(force=True, silent=True) or {}
    USERS.setdefault(uid, {"profile": {}})["profile"].update(body)
    return jsonify(USERS[uid]["profile"])


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    app.run(host=host, port=port, debug=True)
