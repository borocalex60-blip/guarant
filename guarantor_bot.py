"""
Бот-гарант для сделок с NFT (TON).
Схема: покупатель платит TON на кошелёк гаранта -> продавец передаёт NFT ->
покупатель подтверждает -> админ вручную отправляет деньги продавцу.
Спор решает админ.

pip install aiogram>=3.4 aiohttp
Переменные окружения: BOT_TOKEN, ADMIN_ID, GUARANTOR_WALLET, FEE_PERCENT (опц.)
"""
import asyncio
import logging
import os
import sqlite3

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
WALLET = os.environ["GUARANTOR_WALLET"]
FEE = float(os.getenv("FEE_PERCENT", "5"))
TONCENTER = "https://toncenter.com/api/v2/getTransactions"

logging.basicConfig(level=logging.INFO)
router = Router()
db = sqlite3.connect(os.getenv("DB_PATH", "deals.db"), check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS deals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER, buyer_id INTEGER,
    nft TEXT, price REAL, seller_wallet TEXT, buyer_wallet TEXT,
    status TEXT DEFAULT 'new')""")
db.commit()
# статусы: new -> wait_pay -> paid -> delivered -> done | dispute -> done/refunded | cancelled


def get(deal_id):
    return db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()


def setst(deal_id, status):
    db.execute("UPDATE deals SET status=? WHERE id=?", (status, deal_id))
    db.commit()


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


class NewDeal(StatesGroup):
    nft = State()
    price = State()
    wallet = State()


# ---------- создание сделки (продавец) ----------
@router.message(CommandStart())
async def start(m: Message, command: CommandObject, bot: Bot):
    if command.args and command.args.startswith("deal"):
        return await join(m, int(command.args[4:]), bot)
    await m.answer("Я гарант для сделок с NFT.\n/new — создать сделку\n/my — мои сделки")


@router.message(Command("new"))
async def new(m: Message, state: FSMContext):
    await state.set_state(NewDeal.nft)
    await m.answer("Опишите NFT (название / ссылка / адрес):")


@router.message(NewDeal.nft)
async def f_nft(m: Message, state: FSMContext):
    await state.update_data(nft=m.text)
    await state.set_state(NewDeal.price)
    await m.answer("Цена в TON (например 25.5):")


@router.message(NewDeal.price)
async def f_price(m: Message, state: FSMContext):
    try:
        price = float(m.text.replace(",", "."))
        assert price > 0
    except Exception:
        return await m.answer("Введите число больше нуля.")
    await state.update_data(price=price)
    await state.set_state(NewDeal.wallet)
    await m.answer("Ваш TON-кошелёк для получения оплаты:")


@router.message(NewDeal.wallet)
async def f_wallet(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    cur = db.execute(
        "INSERT INTO deals(seller_id,nft,price,seller_wallet) VALUES(?,?,?,?)",
        (m.from_user.id, d["nft"], d["price"], m.text.strip()))
    db.commit()
    me = await bot.get_me()
    await m.answer(f"Сделка #{cur.lastrowid} создана.\nОтправьте покупателю ссылку:\n"
                   f"https://t.me/{me.username}?start=deal{cur.lastrowid}")


@router.message(Command("my"))
async def my(m: Message):
    rows = db.execute("SELECT * FROM deals WHERE seller_id=? OR buyer_id=? ORDER BY id DESC LIMIT 10",
                      (m.from_user.id, m.from_user.id)).fetchall()
    await m.answer("\n".join(f"#{r['id']} · {r['price']} TON · {r['status']}" for r in rows) or "Сделок нет.")


# ---------- покупатель заходит по ссылке ----------
async def join(m: Message, deal_id: int, bot: Bot):
    d = get(deal_id)
    if not d or d["status"] != "new":
        return await m.answer("Сделка не найдена или уже занята.")
    if d["seller_id"] == m.from_user.id:
        return await m.answer("Это ваша сделка, отправьте ссылку покупателю.")
    db.execute("UPDATE deals SET buyer_id=?, status='wait_pay' WHERE id=?", (m.from_user.id, deal_id))
    db.commit()
    await m.answer(
        f"Сделка #{deal_id}\nNFT: {d['nft']}\nЦена: {d['price']} TON\n\n"
        f"Переведите ровно {d['price']} TON на адрес:\n<code>{WALLET}</code>\n"
        f"Комментарий (обязательно): <code>deal{deal_id}</code>\n\n"
        "Оплата определится автоматически в течение ~1 минуты.",
        parse_mode="HTML", reply_markup=kb([("❌ Отменить", f"cancel:{deal_id}")]))
    await bot.send_message(d["seller_id"], f"Покупатель присоединился к сделке #{deal_id}. Ждём оплату.")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(c: CallbackQuery):
    d = get(int(c.data.split(":")[1]))
    if d and d["status"] == "wait_pay" and c.from_user.id == d["buyer_id"]:
        setst(d["id"], "cancelled")
        await c.message.edit_text("Сделка отменена.")
    await c.answer()


# ---------- проверка оплаты (фон) ----------
async def payment_watcher(bot: Bot):
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(TONCENTER, params={"address": WALLET, "limit": 40}) as r:
                    txs = (await r.json()).get("result", [])
            for tx in txs:
                msg = tx.get("in_msg") or {}
                comment = (msg.get("message") or "").strip()
                if not comment.startswith("deal") or not comment[4:].isdigit():
                    continue
                d = get(int(comment[4:]))
                paid = int(msg.get("value", 0)) / 1e9
                if d and d["status"] == "wait_pay" and paid >= d["price"] - 1e-6:
                    db.execute("UPDATE deals SET status='paid', buyer_wallet=? WHERE id=?",
                               (msg.get("source"), d["id"]))
                    db.commit()
                    await bot.send_message(d["buyer_id"], f"✅ Оплата по сделке #{d['id']} получена.")
                    await bot.send_message(
                        d["seller_id"],
                        f"✅ Покупатель оплатил сделку #{d['id']}. Деньги у гаранта.\n"
                        f"Передайте NFT покупателю и нажмите кнопку.",
                        reply_markup=kb([("📤 Я передал NFT", f"sent:{d['id']}")]))
        except Exception as e:
            logging.warning("watcher: %s", e)
        await asyncio.sleep(30)


@router.message(Command("confirm_pay"))  # ручное подтверждение оплаты админом
async def confirm_pay(m: Message, command: CommandObject, bot: Bot):
    if m.from_user.id != ADMIN_ID or not command.args:
        return
    d = get(int(command.args))
    if d and d["status"] == "wait_pay":
        setst(d["id"], "paid")
        await bot.send_message(d["seller_id"], f"✅ Оплата по #{d['id']} подтверждена. Передайте NFT.",
                               reply_markup=kb([("📤 Я передал NFT", f"sent:{d['id']}")]))
        await m.answer("Ок")


# ---------- передача и подтверждение ----------
@router.callback_query(F.data.startswith("sent:"))
async def sent(c: CallbackQuery, bot: Bot):
    d = get(int(c.data.split(":")[1]))
    if not d or d["status"] != "paid" or c.from_user.id != d["seller_id"]:
        return await c.answer("Недоступно", show_alert=True)
    setst(d["id"], "delivered")
    await c.message.edit_text("Ждём подтверждения покупателя.")
    await bot.send_message(
        d["buyer_id"], f"Продавец передал NFT по сделке #{d['id']}. Проверьте получение:",
        reply_markup=kb([("✅ Получил, всё ок", f"got:{d['id']}")],
                        [("⚠️ Проблема / спор", f"dispute:{d['id']}")]))
    await c.answer()


@router.callback_query(F.data.startswith("got:"))
async def got(c: CallbackQuery, bot: Bot):
    d = get(int(c.data.split(":")[1]))
    if not d or d["status"] != "delivered" or c.from_user.id != d["buyer_id"]:
        return await c.answer("Недоступно", show_alert=True)
    setst(d["id"], "done")
    payout = round(d["price"] * (1 - FEE / 100), 4)
    await c.message.edit_text("Сделка завершена, спасибо!")
    await bot.send_message(d["seller_id"], f"Покупатель подтвердил сделку #{d['id']}. Выплата {payout} TON в обработке.")
    await bot.send_message(
        ADMIN_ID, f"💸 К выплате по #{d['id']}: {payout} TON\nНа: <code>{d['seller_wallet']}</code>",
        parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data.startswith("dispute:"))
async def dispute(c: CallbackQuery, bot: Bot):
    d = get(int(c.data.split(":")[1]))
    if not d or d["status"] not in ("paid", "delivered") or c.from_user.id not in (d["buyer_id"], d["seller_id"]):
        return await c.answer("Недоступно", show_alert=True)
    setst(d["id"], "dispute")
    await c.message.edit_text("Спор открыт, с вами свяжется администратор.")
    await bot.send_message(
        ADMIN_ID, f"⚠️ Спор по #{d['id']} ({d['price']} TON)\nNFT: {d['nft']}\n"
                  f"Продавец: {d['seller_id']}\nПокупатель: {d['buyer_id']}",
        reply_markup=kb([("💰 Выплатить продавцу", f"adm_release:{d['id']}")],
                        [("↩️ Вернуть покупателю", f"adm_refund:{d['id']}")]))
    await c.answer()


@router.callback_query(F.data.startswith("adm_"))
async def admin_resolve(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        return await c.answer()
    action, did = c.data.split(":")
    d = get(int(did))
    if not d or d["status"] != "dispute":
        return await c.answer("Уже решено", show_alert=True)
    if action == "adm_release":
        setst(d["id"], "done")
        text = f"Выплатить {d['price']} TON продавцу: {d['seller_wallet']}"
    else:
        setst(d["id"], "refunded")
        text = f"Вернуть {d['price']} TON покупателю: {d['buyer_wallet']}"
    await c.message.edit_text(f"#{d['id']}: {text}")
    for uid in (d["buyer_id"], d["seller_id"]):
        await bot.send_message(uid, f"Спор по сделке #{d['id']} решён администратором.")
    await c.answer()


async def main():
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(payment_watcher(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
