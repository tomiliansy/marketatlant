"""Мост Яндекс Маркет (DBS) <-> Telegram.

Маркет шлёт уведомления на этот сервер -> вы получаете их в Telegram.
Ответ на сообщение покупателя (Reply в Telegram) уходит в чат Маркета.

Локальный запуск:  python app.py          (адрес http://127.0.0.1:8000)
Запуск на сервере: gunicorn -w 1 --threads 4 -b 127.0.0.1:8000 app:app
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, request

# ---------- файл .env (локально его читаем сами, на сервере его читает systemd) ----------
def _load_env(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_env()

# ---------- настройки ----------
TG_TOKEN = os.environ["TG_TOKEN"]
MARKET_TOKEN = os.environ["MARKET_TOKEN"]
BUSINESS_ID = os.environ["BUSINESS_ID"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))  # ваш chat_id в Telegram
SECRET = os.environ.get("SECRET", "")  # секретная часть URL
NOTIFY_STATUS = os.environ.get("NOTIFY_STATUS", "0") == "1"  # шуметь о каждой смене статуса
PROXIES = {"https": os.environ["TG_PROXY"]} if os.environ.get("TG_PROXY") else None
DB_PATH = os.environ.get("DB_PATH",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.db"))

MARKET_API = "https://api.partner.market.yandex.ru"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# ---------- маленькая база: какой tg-сообщение к какому чату Маркета ----------
_lock = threading.Lock()


def db(sql, args=()):
    with _lock:
        c = sqlite3.connect(DB_PATH)
        try:
            rows = c.execute(sql, args).fetchall()
            c.commit()
            return rows
        finally:
            c.close()


db("CREATE TABLE IF NOT EXISTS tgmap (tg_id INTEGER PRIMARY KEY, chat_id INTEGER)")
db("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY)")


def first_time(key):
    """Маркет может прислать одно событие несколько раз - отсекаем дубли."""
    if db("SELECT 1 FROM seen WHERE k=?", (key,)):
        return False
    db("INSERT INTO seen VALUES (?)", (key,))
    return True


# ---------- Telegram ----------
def tg(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        json=params, proxies=PROXIES, timeout=70,
    )
    return r.json()


def tg_send(text, market_chat_id=None):
    if not OWNER_ID:
        log.warning("OWNER_ID не задан, сообщение не отправлено: %s", text[:80])
        return
    res = tg("sendMessage", chat_id=OWNER_ID, text=text[:4000])
    if market_chat_id and res.get("ok"):
        db("INSERT OR REPLACE INTO tgmap VALUES (?, ?)",
           (res["result"]["message_id"], market_chat_id))


# ---------- Маркет ----------
def market(method, path, **kw):
    r = requests.request(method, MARKET_API + path,
                         headers={"Api-Key": MARKET_TOKEN}, timeout=20, **kw)
    if not r.ok:
        raise RuntimeError(f"Маркет вернул {r.status_code}: {r.text[:300]}")
    return r.json()


_order_cache = {}


def _cache_order(chat_id, order):
    """Не даём кэшу расти бесконечно: при переполнении выкидываем самые старые записи."""
    if len(_order_cache) > 5000:
        for k in list(_order_cache)[:1000]:
            _order_cache.pop(k, None)
    _order_cache[chat_id] = order


def chat_order(chat_id):
    """Номер заказа, к которому относится чат (кэшируем)."""
    if chat_id not in _order_cache:
        try:
            res = market("GET", f"/v2/businesses/{BUSINESS_ID}/chat",
                         params={"chatId": chat_id}).get("result", {})
            ctx = res.get("context") or {}
            _cache_order(chat_id, res.get("orderId") or ctx.get("orderId"))
        except Exception:
            log.exception("не удалось получить чат %s", chat_id)
            return None
    return _order_cache[chat_id]


_last_cleanup = 0.0


def cleanup():
    """Раз в сутки подрезаем таблицу дедупликации, чтобы она не росла вечно."""
    global _last_cleanup
    if time.time() - _last_cleanup < 86400:
        return
    _last_cleanup = time.time()
    try:
        db("DELETE FROM seen WHERE rowid NOT IN "
           "(SELECT rowid FROM seen ORDER BY rowid DESC LIMIT 10000)")
    except Exception:
        log.exception("не удалось почистить таблицу seen")


def handle(n):
    t = n.get("notificationType")
    log.info("уведомление: %s", t)

    if t == "ORDER_CREATED":
        items = "\n".join(f"• {i.get('offerId')} × {i.get('count')}" for i in n.get("items", []))
        tg_send(f"🛒 Новый заказ №{n.get('orderId')}\n{items}")

    elif t == "ORDER_CANCELLATION_REQUEST":
        tg_send(f"⚠️ Покупатель просит отменить заказ №{n.get('orderId')}.\n"
                "Подтвердите или отклоните в кабинете Маркета.")

    elif t == "ORDER_CANCELLED":
        tg_send(f"❌ Заказ №{n.get('orderId')} отменён")

    elif t == "ORDER_STATUS_UPDATED" and NOTIFY_STATUS:
        tg_send(f"📦 Заказ №{n.get('orderId')}: {n.get('status')} / {n.get('substatus')}")

    elif t == "CHAT_CREATED":
        order = chat_order(n.get("chatId", 0))
        head = f"💬 Новый чат по заказу №{order}" if order else f"💬 Новый чат {n['chatId']}"
        tg_send(f"{head}\nОткройте чат в кабинете Маркета.")

    elif t == "CHAT_MESSAGE_SENT":
        if not first_time(f"msg:{n.get('messageId')}"):
            return
        m = market("GET", f"/v2/businesses/{BUSINESS_ID}/chats/message",
                   params={"chatId": n["chatId"], "messageId": n["messageId"]}).get("result", {})
        if m.get("sender") == "PARTNER":  # это наше собственное сообщение
            return
        text = m.get("message") or "(вложение - откройте чат в кабинете Маркета)"
        order = chat_order(n["chatId"])
        head = f"💬 Чат по заказу №{order}" if order else f"💬 Чат {n['chatId']}"
        tg_send(f"{head}\n\n{text}\n\n↩️ Ответьте (Reply) на это сообщение, чтобы написать покупателю",
                market_chat_id=n["chatId"])

    elif t == "CHAT_ARBITRAGE_STARTED":
        tg_send(f"⚖️ Начался спор в чате {n.get('chatId')}. Откройте кабинет Маркета.")

    elif t == "CHAT_ARBITRAGE_FINISHED":
        tg_send(f"⚖️ Спор в чате {n.get('chatId')} завершён.")


def safe_handle(n):
    try:
        cleanup()
        handle(n)
    except Exception:
        log.exception("ошибка обработки %s", n)


# ---------- веб-сервер для Маркета ----------
app = Flask(__name__)


@app.route("/", defaults={"p": ""}, methods=["GET", "POST"])
@app.route("/<path:p>", methods=["GET", "POST"])
def webhook(p):
    if SECRET and p.split("/")[0] != SECRET:
        return "not found", 404
    if request.method == "GET":
        return "ok"
    n = request.get_json(silent=True) or {}
    # PING нужно отвечать за 1 секунду, поэтому всё остальное - в фоне
    if n.get("notificationType") != "PING":
        threading.Thread(target=safe_handle, args=(n,), daemon=True).start()
    body = {
        "version": "1.0.0",
        "name": "tg-bridge",
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return Response(json.dumps(body), status=200,
                    content_type="application/json; charset=utf-8")


# ---------- приём ваших сообщений из Telegram ----------
def on_tg_message(m):
    uid = m["chat"]["id"]
    text = m.get("text", "")
    if text.startswith("/start"):
        tg("sendMessage", chat_id=uid, text=f"Ваш chat_id: {uid}")
        return
    if uid != OWNER_ID or not text:
        return
    rep = m.get("reply_to_message")
    if not rep:
        tg_send("Чтобы ответить покупателю, нажмите Reply на его сообщение.")
        return
    rows = db("SELECT chat_id FROM tgmap WHERE tg_id=?", (rep["message_id"],))
    if not rows:
        tg_send("Не нашёл чат Маркета для этого сообщения. Ответьте на сообщение с пометкой 💬.")
        return
    try:
        market("POST", f"/v2/businesses/{BUSINESS_ID}/chats/message",
               params={"chatId": rows[0][0]}, json={"message": text})
        tg("sendMessage", chat_id=OWNER_ID, text="✅ Отправлено покупателю",
           reply_to_message_id=m["message_id"])
    except Exception as e:
        log.exception("не удалось отправить сообщение")
        tg_send(f"❌ Не отправилось: {e}")


def poll_telegram():
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                if u.get("message"):
                    on_tg_message(u["message"])
        except Exception:
            log.exception("ошибка опроса Telegram")
            time.sleep(5)


threading.Thread(target=poll_telegram, daemon=True).start()


if __name__ == "__main__":
    # Локальный запуск для проверки: python app.py
    # На сервере этот блок не нужен - там запускает gunicorn (см. SETUP.md).
    app.run(host="127.0.0.1", port=8000, threaded=True)
