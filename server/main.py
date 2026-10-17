"""Бенто · Ирина: бэкенд (FastAPI + aiogram 3 + SQLite + WebSocket).

Запуск одной командой:  python main.py
Настройки читаются из файла .env рядом с main.py (см. .env.example).
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           MenuButtonWebApp, Message, WebAppInfo)
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).with_name(".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bento")

CLIENT_TOKEN = os.getenv("CLIENT_BOT_TOKEN", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
CLIENT_URL = os.getenv("CLIENT_URL", "").strip()
ADMIN_URL = os.getenv("ADMIN_URL", "").strip()
ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).with_name("bento.db")))
PORT = int(os.getenv("PORT", "8000"))
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"  # только для локальной отладки без Telegram!
MSK = ZoneInfo("Europe/Moscow")

STATUSES = {"new", "accepted", "work", "ready", "done"}
CLIENT_NOTICE = {
    "accepted": "✅ Ирина приняла ваш заказ №{id}. Скоро свяжется, если нужно что-то уточнить.",
    "work": "👩‍🍳 Заказ №{id} уже на кухне, пахнет вкусно!",
    "ready": "🎁 Заказ №{id} готов! Договоримся о выдаче у м. Водный стадион или о Яндекс Доставке.",
    "done": "🛵 Заказ №{id} передан. Приятного аппетита! 💛",
}

db: Optional[aiosqlite.Connection] = None
client_bot: Optional[Bot] = None
admin_bot: Optional[Bot] = None
background: set[asyncio.Task] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL, nick TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new', payload TEXT NOT NULL, price INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id),
  sender TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_orders_tg ON orders(tg_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_order ON messages(order_id);
"""


# ---------------------------------------------------------------- утилиты
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%H:%M")


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    background.add(task)
    task.add_done_callback(background.discard)


def verify_init_data(init_data: str, token: str, max_age: int = 86400) -> Optional[dict]:
    """Проверка подписи Telegram WebApp initData. Возвращает dict пользователя или None."""
    if not init_data or not token:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    return user if isinstance(user, dict) and "id" in user else None


def auth_client(init_data: str) -> dict:
    user = verify_init_data(init_data, CLIENT_TOKEN)
    if user:
        return user
    if DEV_MODE and not init_data:
        return {"id": 1, "username": "dev_client", "first_name": "Dev"}
    raise HTTPException(401, "Откройте конструктор через бота в Telegram")


def auth_admin(init_data: str) -> Optional[dict]:
    user = verify_init_data(init_data, ADMIN_TOKEN)
    if user:
        return user if user["id"] in ADMIN_IDS else None
    if DEV_MODE and not init_data:
        return {"id": 0, "username": "dev_admin"}
    return None


def require_admin(init_data: str) -> dict:
    user = auth_admin(init_data)
    if not user:
        raise HTTPException(403, "Доступ только для сотрудников пекарни")
    return user


def web_kb(url: str, text: str) -> Optional[InlineKeyboardMarkup]:
    if not url.startswith("https://"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]])


async def safe_send(bot: Optional[Bot], chat_id: int, text: str, markup=None) -> bool:
    if bot is None or chat_id <= 0:
        return False
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
        return True
    except Exception as exc:  # пользователь заблокировал бота, нет /start и т.п.
        log.warning("send_message(%s) failed: %s", chat_id, exc)
        return False


async def notify_admins(text: str) -> None:
    for admin_id in ADMIN_IDS:
        await safe_send(admin_bot, admin_id, text, web_kb(ADMIN_URL, "🏪 Войти в пекарню"))


class Hub:
    """Менеджер WebSocket-подключений кабинета Ирины."""

    def __init__(self) -> None:
        self.conns: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.conns.add(ws)

    def drop(self, ws: WebSocket) -> None:
        self.conns.discard(ws)

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.conns):
            try:
                await ws.send_json(message)
            except Exception:
                self.drop(ws)


hub = Hub()


# ---------------------------------------------------------------- база данных
def msg_dict(row) -> dict:
    return {"id": row["id"], "f": "c" if row["sender"] == "client" else "m",
            "t": row["text"], "h": hhmm(row["created_at"])}


async def public_order(order_id: int) -> Optional[dict]:
    async with db.execute("SELECT * FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    async with db.execute("SELECT * FROM messages WHERE order_id=? ORDER BY id", (order_id,)) as cur:
        msgs = await cur.fetchall()
    p = json.loads(row["payload"])
    return {"id": row["id"], "nick": row["nick"], "status": row["status"], "price": row["price"],
            "rows": p["rows"], "k": p["k"], "short": p["short"], "wish": p["wish"],
            "created_at": row["created_at"], "messages": [msg_dict(m) for m in msgs]}


async def add_message(order_id: int, sender: str, text: str) -> dict:
    cur = await db.execute("INSERT INTO messages(order_id,sender,text,created_at) VALUES(?,?,?,?)",
                           (order_id, sender, text, now()))
    await db.commit()
    async with db.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)) as c:
        return msg_dict(await c.fetchone())


class OrderIn(BaseModel):
    rows: Annotated[list[Annotated[list[str], Field(min_length=2, max_length=2)]], Field(min_length=1, max_length=20)]
    price: int = Field(ge=0, le=200000)
    k: int = Field(default=1, ge=1, le=12)
    short: str = Field(max_length=80)
    wish: str = Field(default="", max_length=120)
    nick: str = Field(default="", max_length=64)


class OrderBody(BaseModel):
    order: OrderIn


class StatusBody(BaseModel):
    status: str


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


async def create_order(tg_id: int, nick: str, o: OrderIn) -> dict:
    ts = now()
    payload = {"rows": [[a[:80], b[:300]] for a, b in o.rows], "k": o.k, "short": o.short, "wish": o.wish}
    cur = await db.execute(
        "INSERT INTO orders(tg_id,nick,status,payload,price,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (tg_id, nick[:64], "new", json.dumps(payload, ensure_ascii=False), o.price, ts, ts))
    order_id = cur.lastrowid
    await db.execute("INSERT INTO messages(order_id,sender,text,created_at) VALUES(?,?,?,?)",
                     (order_id, "client", f"Здравствуйте! Оформил(а) заказ в конструкторе: {o.short}", ts))
    await db.commit()
    return await public_order(order_id)


# ---------------------------------------------------------------- боты
def build_client_dispatcher() -> Dispatcher:
    router = Router()

    @router.message(CommandStart())
    async def start(m: Message) -> None:
        await m.answer("Привет! Это бот Ирины: бенто-торты в Москве, м. Водный стадион 🍰\n"
                       "Нажмите кнопку, соберите торт мечты, а Ирина напишет вам сюда.",
                       reply_markup=web_kb(CLIENT_URL, "🎂 Собрать торт"))

    @router.message(F.text)
    async def text(m: Message) -> None:
        async with db.execute("SELECT * FROM orders WHERE tg_id=? AND status!='done' ORDER BY id DESC LIMIT 1",
                              (m.from_user.id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await m.answer("Чтобы сделать заказ, нажмите «🎂 Собрать торт» в меню бота.",
                           reply_markup=web_kb(CLIENT_URL, "🎂 Собрать торт"))
            return
        msg = await add_message(row["id"], "client", m.text[:1000])
        await hub.broadcast({"type": "chat", "order_id": row["id"], "message": msg})
        await notify_admins(f"💬 <b>{html.escape(row['nick'])}</b> (заказ №{row['id']}):\n{html.escape(m.text[:1000])}")

    dp = Dispatcher()
    dp.include_router(router)
    return dp


def build_admin_dispatcher() -> Dispatcher:
    router = Router()

    @router.message(CommandStart())
    async def start(m: Message) -> None:
        if m.from_user.id not in ADMIN_IDS:
            await m.answer(f"Доступ только для сотрудников пекарни. Ваш Telegram ID: <code>{m.from_user.id}</code>")
            return
        await m.answer("Пекарня открыта! 🥐", reply_markup=web_kb(ADMIN_URL, "🏪 Войти в пекарню"))

    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def setup_menu(bot: Bot, text: str, url: str) -> None:
    if not url.startswith("https://"):
        log.warning("URL для кнопки «%s» должен начинаться с https://, кнопка меню не установлена", text)
        return
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text=text, web_app=WebAppInfo(url=url)))
    except Exception as exc:
        log.warning("set_chat_menu_button failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, client_bot, admin_bot
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    polling: list[asyncio.Task] = []
    if CLIENT_TOKEN:
        client_bot = Bot(CLIENT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await setup_menu(client_bot, "🎂 Собрать торт", CLIENT_URL)
        polling.append(asyncio.create_task(build_client_dispatcher().start_polling(
            client_bot, handle_signals=False, allowed_updates=["message"])))
    else:
        log.warning("CLIENT_BOT_TOKEN не задан: клиентский бот отключён")
    if ADMIN_TOKEN:
        admin_bot = Bot(ADMIN_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await setup_menu(admin_bot, "🏪 Войти в пекарню", ADMIN_URL)
        polling.append(asyncio.create_task(build_admin_dispatcher().start_polling(
            admin_bot, handle_signals=False, allowed_updates=["message"])))
    else:
        log.warning("ADMIN_BOT_TOKEN не задан: служебный бот отключён")
    if DEV_MODE:
        log.warning("DEV_MODE=1: проверка подписи Telegram отключена. Не используйте в продакшне!")
    try:
        yield
    finally:
        for task in polling:
            task.cancel()
        await asyncio.gather(*polling, return_exceptions=True)
        for bot in (client_bot, admin_bot):
            if bot:
                await bot.session.close()
        client_bot = admin_bot = None
        await db.close()


# ---------------------------------------------------------------- HTTP / WebSocket API
app = FastAPI(title="Bento Irina API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "client_bot": client_bot is not None, "admin_bot": admin_bot is not None,
            "ws_clients": len(hub.conns)}


@app.post("/api/orders")
async def post_order(body: OrderBody, x_init_data: str = Header(default="")) -> dict:
    user = auth_client(x_init_data)
    since = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat(timespec="seconds")
    async with db.execute("SELECT COUNT(*) FROM orders WHERE tg_id=? AND created_at>?", (user["id"], since)) as cur:
        if (await cur.fetchone())[0] >= 10:
            raise HTTPException(429, "Слишком много заказов за час")
    o = body.order
    nick = f"@{user['username']}" if user.get("username") else (o.nick or user.get("first_name") or f"id{user['id']}")
    order = await create_order(user["id"], nick, o)
    await hub.broadcast({"type": "order_new", "order": order})
    lines = "\n".join(f"{html.escape(a)}: {html.escape(b)}" for a, b in order["rows"])
    spawn(notify_admins(f"🔔 <b>Новый заказ №{order['id']}</b> от {html.escape(nick)}\n{lines}\n💰 {order['price']} ₽"))
    spawn(safe_send(client_bot, user["id"], f"🧾 Заказ №{order['id']} отправлен Ирине. "
                    "Я напишу вам, когда она его примет. Вопросы можно задавать прямо здесь."))
    return {"id": order["id"]}


@app.get("/api/admin/orders")
async def list_orders(x_init_data: str = Header(default="")) -> list[dict]:
    require_admin(x_init_data)
    async with db.execute("SELECT id FROM orders WHERE status!='done' ORDER BY id") as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    return [await public_order(i) for i in ids]


@app.patch("/api/admin/orders/{order_id}")
async def set_status(order_id: int, body: StatusBody, x_init_data: str = Header(default="")) -> dict:
    require_admin(x_init_data)
    if body.status not in STATUSES:
        raise HTTPException(422, "Неизвестный статус")
    async with db.execute("SELECT tg_id,status FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if row["status"] != body.status:
        await db.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (body.status, now(), order_id))
        await db.commit()
        if body.status in CLIENT_NOTICE:
            spawn(safe_send(client_bot, row["tg_id"], CLIENT_NOTICE[body.status].format(id=order_id)))
    order = await public_order(order_id)
    await hub.broadcast({"type": "order_update", "order": order})
    return order


@app.post("/api/admin/orders/{order_id}/reply")
async def reply(order_id: int, body: ReplyBody, x_init_data: str = Header(default="")) -> dict:
    require_admin(x_init_data)
    async with db.execute("SELECT tg_id FROM orders WHERE id=?", (order_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if row["tg_id"] > 0:
        if client_bot is None:
            raise HTTPException(503, "Клиентский бот не запущен")
        if not await safe_send(client_bot, row["tg_id"], f"💬 <b>Ирина:</b>\n{html.escape(body.text)}"):
            raise HTTPException(502, "Telegram не доставил сообщение клиенту")
    msg = await add_message(order_id, "admin", body.text)
    await hub.broadcast({"type": "chat", "order_id": order_id, "message": msg})
    return msg


@app.post("/api/admin/simulate")
async def simulate(body: OrderBody, x_init_data: str = Header(default="")) -> dict:
    """Тестовый заказ: проходит тот же путь, что и настоящий, но без сообщений в Telegram клиенту."""
    require_admin(x_init_data)
    order = await create_order(0, body.order.nick or "@test_guest", body.order)
    await hub.broadcast({"type": "order_new", "order": order})
    return {"id": order["id"]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, init_data: str = Query(default="")) -> None:
    if not auth_admin(init_data):
        await ws.close(code=4401)
        return
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # клиент шлёт "ping" раз в 25 с, чтобы соединение не засыпало
    except WebSocketDisconnect:
        pass
    finally:
        hub.drop(ws)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
