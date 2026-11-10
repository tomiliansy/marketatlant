"""Мост Яндекс Маркет (DBS) <-> Telegram.

Маркет шлёт уведомления на этот сервер -> вы получаете их в Telegram.
Ответ (Reply) на сообщение о заказе или чате уходит покупателю в чат Маркета.
Кнопка «Доставлено» (с подтверждением) переводит заказ в статус DELIVERED.
Если заказ долго не отмечен доставленным, бот напоминает.

Запуск на сервере: gunicorn -w 1 --threads 4 -b 127.0.0.1:8000 app:app
(воркер строго один: опрос Telegram живёт внутри процесса)
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


# ---------- файл .env ----------
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
CAMPAIGN_ID = os.environ.get("CAMPAIGN_ID", "")  # id магазина (кампании), нужен для статуса «доставлен»
NOTIFY_STATUS = os.environ.get("NOTIFY_STATUS", "0") == "1"  # шуметь о каждой смене статуса
REMIND_MIN = int(os.environ.get("REMIND_MINUTES", "20"))  # через сколько минут напоминать
MAX_REMINDERS = int(os.environ.get("MAX_REMINDERS", "6"))  # сколько раз напоминать максимум
PROXIES = {"https": os.environ["TG_PROXY"]} if os.environ.get("TG_PROXY") else None
DB_PATH = os.environ.get("DB_PATH",
                         os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.db"))

MARKET_API = "https://api.partner.market.yandex.ru"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# ---------- маленькая база ----------
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


# tgmap: какое сообщение в Telegram относится к какому чату Маркета
db("CREATE TABLE IF NOT EXISTS tgmap (tg_id INTEGER PRIMARY KEY, chat_id INTEGER)")
# tgorder: какое сообщение в Telegram относится к какому заказу
db("CREATE TABLE IF NOT EXISTS tgorder (tg_id INTEGER PRIMARY KEY, order_id INTEGER)")
db("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY)")
# orders: state = new (ждёт выдачи) / done / cancelled / muted
db("CREATE TABLE IF NOT EXISTS orders ("
   "order_id INTEGER PRIMARY KEY, campaign_id INTEGER, tg_id INTEGER, chat_id INTEGER, "
   "created REAL, state TEXT, reminders INTEGER DEFAULT 0, next_remind REAL)")


def first_time(key):
    """Маркет может прислать одно событие несколько раз - отсекаем дубли."""
    if db("SELECT 1 FROM seen WHERE k=?", (key,)):
        return False
    db("INSERT INTO seen VALUES (?)", (key,))
    return True


def order_state(order_id):
    rows = db("SELECT state FROM orders WHERE order_id=?", (order_id,))
    return rows[0][0] if rows else None


def set_state(order_id, state):
    db("UPDATE orders SET state=? WHERE order_id=?", (state, order_id))


# ---------- Telegram ----------
def tg(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        json=params, proxies=PROXIES, timeout=70,
    )
    return r.json()


def done_button(order_id):
    return [[{"text": "✅ Доставлено", "callback_data": f"done:{order_id}"}]]


def confirm_buttons(order_id):
    return [[{"text": "✅ Да, данные выданы", "callback_data": f"yes:{order_id}"},
             {"text": "↩️ Отмена", "callback_data": f"no:{order_id}"}]]


def remind_buttons(order_id):
    return [[{"text": "✅ Доставлено", "callback_data": f"done:{order_id}"}],
            [{"text": "🔕 Не напоминать", "callback_data": f"mute:{order_id}"}]]


def tg_send(text, market_chat_id=None, order_id=None, buttons=None, reply_to=None):
    """Отправляет сообщение владельцу и запоминает, к какому чату/заказу оно относится."""
    if not OWNER_ID:
        log.warning("OWNER_ID не задан, сообщение не отправлено: %s", text[:80])
        return None
    params = {"chat_id": OWNER_ID, "text": text[:4000]}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = True
    res = tg("sendMessage", **params)
    if not res.get("ok"):
        log.error("Telegram не принял сообщение: %s", res)
        return None
    mid = res["result"]["message_id"]
    if market_chat_id:
        db("INSERT OR REPLACE INTO tgmap VALUES (?, ?)", (mid, market_chat_id))
    if order_id:
        db("INSERT OR REPLACE INTO tgorder VALUES (?, ?)", (mid, order_id))
    return mid


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


def open_chat(order_id):
    """Чат Маркета по заказу: берём известный или создаём (POST /chats/new)."""
    rows = db("SELECT chat_id FROM orders WHERE order_id=?", (order_id,))
    if rows and rows[0][0]:
        return rows[0][0]
    res = market("POST", f"/v2/businesses/{BUSINESS_ID}/chats/new",
                 json={"context": {"type": "ORDER", "id": int(order_id)}})
    chat_id = (res.get("result") or {}).get("chatId")
    if not chat_id:
        raise RuntimeError(f"Маркет не вернул chatId: {str(res)[:200]}")
    db("UPDATE orders SET chat_id=? WHERE order_id=?", (chat_id, order_id))
    return chat_id


def mark_delivered(order_id):
    """Сообщаем Маркету, что цифровой товар выдан (способ выдачи CHAT)."""
    rows = db("SELECT campaign_id FROM orders WHERE order_id=?", (order_id,))
    campaign = CAMPAIGN_ID or (rows[0][0] if rows and rows[0][0] else None)
    if not campaign:
        raise RuntimeError("не знаю campaignId магазина: добавьте CAMPAIGN_ID в .env")
    market("PUT", f"/v2/campaigns/{campaign}/orders/{order_id}/status",
           json={"order": {"status": "DELIVERED"}})
    set_state(order_id, "done")


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


# ---------- обработка уведомлений Маркета ----------
def handle(n):
    t = n.get("notificationType")
    log.info("уведомление: %s", t)

    if t == "ORDER_CREATED":
        oid = n.get("orderId")
        if not oid or not first_time(f"order:{oid}"):
            return
        oid = int(oid)
        items = "\n".join(f"• {i.get('offerId')} × {i.get('count')}" for i in n.get("items", []))
        mid = tg_send(f"🛒 Новый заказ №{oid}\n{items}\n\n"
                      "↩️ Ответьте (Reply) на это сообщение - текст уйдёт покупателю в чат Маркета.\n"
                      "Когда данные выданы, нажмите «✅ Доставлено».",
                      order_id=oid, buttons=done_button(oid))
        campaign = n.get("campaignId") or CAMPAIGN_ID or None
        db("INSERT OR REPLACE INTO orders (order_id, campaign_id, tg_id, chat_id, created, state, "
           "reminders, next_remind) VALUES (?, ?, ?, NULL, ?, 'new', 0, ?)",
           (oid, campaign, mid, time.time(), time.time() + REMIND_MIN * 60))

    elif t == "ORDER_CANCELLATION_REQUEST":
        tg_send(f"⚠️ Покупатель просит отменить заказ №{n.get('orderId')}.\n"
                "Подтвердите или отклоните в кабинете Маркета.")

    elif t == "ORDER_CANCELLED":
        if n.get("orderId"):
            set_state(int(n["orderId"]), "cancelled")
        tg_send(f"❌ Заказ №{n.get('orderId')} отменён")

    elif t == "ORDER_STATUS_UPDATED":
        oid, status = n.get("orderId"), n.get("status")
        if oid and status == "DELIVERED":
            set_state(int(oid), "done")
        elif oid and status == "CANCELLED":
            set_state(int(oid), "cancelled")
        if NOTIFY_STATUS:
            tg_send(f"📦 Заказ №{oid}: {status} / {n.get('substatus')}")

    elif t == "CHAT_CREATED":
        order = chat_order(n.get("chatId", 0))
        head = f"💬 Новый чат по заказу №{order}" if order else f"💬 Новый чат {n.get('chatId')}"
        tg_send(f"{head}\nОтветьте (Reply) на это сообщение, чтобы написать покупателю.",
                market_chat_id=n.get("chatId"), order_id=order)

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
                market_chat_id=n["chatId"], order_id=order)

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


# ---------- приём ваших сообщений и нажатий из Telegram ----------
def _markup(buttons):
    return {"reply_markup": {"inline_keyboard": buttons}} if buttons else {}


def on_tg_message(m):
    uid = m["chat"]["id"]
    text = m.get("text", "")
    if text.startswith("/start"):
        tg("sendMessage", chat_id=uid, text=f"Ваш chat_id: {uid}")
        return
    if uid != OWNER_ID:
        return
    if text.startswith("/open"):
        rows = db("SELECT order_id, created FROM orders WHERE state='new' ORDER BY created")
        if not rows:
            tg_send("Заказов, ожидающих выдачи, нет 👍")
            return
        lines = [f"• №{o} - {int((time.time() - c) / 60)} мин назад" for o, c in rows[:15]]
        buttons = [[{"text": f"✅ №{o} доставлен", "callback_data": f"done:{o}"}] for o, _ in rows[:15]]
        tg_send("Не отмечены доставленными:\n" + "\n".join(lines), buttons=buttons)
        return
    if not text:
        if any(k in m for k in ("photo", "document", "video", "voice", "audio", "sticker")):
            tg_send("Пока я передаю покупателю только текст. Файлы и фото отправьте в кабинете Маркета.")
        return
    rep = m.get("reply_to_message")
    if not rep:
        tg_send("Чтобы ответить покупателю, нажмите Reply на сообщение о заказе или чате.")
        return

    rows = db("SELECT chat_id FROM tgmap WHERE tg_id=?", (rep["message_id"],))
    chat_id = rows[0][0] if rows else None
    orows = db("SELECT order_id FROM tgorder WHERE tg_id=?", (rep["message_id"],))
    order_id = orows[0][0] if orows else None
    if chat_id is None and order_id is None:
        tg_send("Не нашёл заказ или чат для этого сообщения. Ответьте на сообщение с пометкой 🛒 или 💬.")
        return
    try:
        if chat_id is None:
            chat_id = open_chat(order_id)
        market("POST", f"/v2/businesses/{BUSINESS_ID}/chats/message",
               params={"chatId": chat_id}, json={"message": text})
    except Exception as e:
        log.exception("не удалось отправить сообщение")
        tg_send(f"❌ Не отправилось: {e}")
        return
    if order_id is None:
        order_id = chat_order(chat_id)
    buttons = done_button(order_id) if order_id and order_state(order_id) == "new" else None
    tg("sendMessage", chat_id=OWNER_ID, text="✅ Отправлено покупателю",
       reply_to_message_id=m["message_id"], **_markup(buttons))


def on_tg_callback(cb):
    uid = cb["from"]["id"]
    msg = cb.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    action, _, oid = (cb.get("data") or "").partition(":")

    def answer(text="", alert=False):
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=text[:190], show_alert=alert)

    def set_buttons(buttons):
        tg("editMessageReplyMarkup", chat_id=chat, message_id=mid,
           reply_markup={"inline_keyboard": buttons})

    if uid != OWNER_ID or not oid.isdigit():
        answer()
        return
    order_id = int(oid)
    if action == "done":
        set_buttons(confirm_buttons(order_id))
        answer("Подтвердите: данные покупателю выданы?")
    elif action == "no":
        set_buttons(done_button(order_id))
        answer("Отменено")
    elif action == "mute":
        set_state(order_id, "muted")
        set_buttons([])
        answer("Напоминания по заказу отключены")
    elif action == "yes":
        try:
            mark_delivered(order_id)
        except Exception as e:
            log.exception("не удалось отметить заказ доставленным")
            set_buttons(done_button(order_id))
            answer(f"Не удалось: {e}", alert=True)
            return
        set_buttons([])
        answer("Заказ отмечен доставленным")
        tg_send(f"✅ Заказ №{order_id} отмечен как доставленный")
    else:
        answer()


def poll_telegram():
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                try:
                    if u.get("message"):
                        on_tg_message(u["message"])
                    elif u.get("callback_query"):
                        on_tg_callback(u["callback_query"])
                except Exception:
                    log.exception("ошибка обработки обновления Telegram")
        except Exception:
            log.exception("ошибка опроса Telegram")
            time.sleep(5)


def reminder_loop():
    """Раз в минуту проверяем заказы, которые долго не отмечены доставленными."""
    while True:
        time.sleep(60)
        try:
            now = time.time()
            rows = db("SELECT order_id, tg_id, created, reminders FROM orders "
                      "WHERE state='new' AND next_remind <= ?", (now,))
            for oid, tg_id, created, cnt in rows:
                mins = int((now - created) / 60)
                tg_send(f"⏰ Заказ №{oid} уже {mins} мин не отмечен доставленным.\n"
                        "Данные покупателю отправлены? Если да - нажмите кнопку.",
                        order_id=oid, buttons=remind_buttons(oid), reply_to=tg_id)
                cnt += 1
                nxt = now + REMIND_MIN * 60 if cnt < MAX_REMINDERS else 1e18
                db("UPDATE orders SET reminders=?, next_remind=? WHERE order_id=?", (cnt, nxt, oid))
        except Exception:
            log.exception("ошибка в напоминаниях")


threading.Thread(target=poll_telegram, daemon=True).start()
threading.Thread(target=reminder_loop, daemon=True).start()


if __name__ == "__main__":
    # Локальный запуск для проверки: python app.py
    app.run(host="127.0.0.1", port=8000, threaded=True)
