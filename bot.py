import os, asyncio, aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

class AdminState(StatesGroup):
    password = State()
    spam_message = State()

async def api_post(path, json=None):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API_BASE}{path}", json=json) as r:
            r.raise_for_status()
            return await r.json()

async def api_get(path):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{API_BASE}{path}") as r:
            r.raise_for_status()
            return await r.json()

@router.message(Command("start"))
async def start_cmd(message: Message):
    await api_post("/users", {"user_id": message.from_user.id})
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть мини-апп", web_app=WebAppInfo(url="https://kramjmka.ru"))
    ]])
    await message.answer("Привет! ⚡️", reply_markup=kb)

@router.message(Command("admin"))
async def admin_cmd(message: Message):
    data = await api_get(f"/admins/{message.from_user.id}")
    if data["is_admin"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Рассылка", callback_data="spam")]
        ])
        await message.answer("Выберите админ-функцию:", reply_markup=kb)
    else:
        await message.answer("Вы не админ")

@router.message(Command("password"))
async def ask_password(message: Message, state: FSMContext):
    await message.answer("Введите пароль")
    await state.set_state(AdminState.password)

@router.message(AdminState.password)
async def handle_password(message: Message, state: FSMContext):
    if message.text.strip() == "admin123":
        await api_post(f"/admins/{message.from_user.id}")
        await message.answer("Админ-права выданы ✅")
    else:
        await message.answer("Неверный пароль ❌")
    await state.clear()

@router.callback_query(F.data == "spam")
async def spam_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await api_get(f"/admins/{cb.from_user.id}")
    if not data["is_admin"]:
        return await cb.message.answer("Вы не админ")
    await cb.message.answer("Отправьте текст рассылки одним сообщением:")
    await state.set_state(AdminState.spam_message)

@router.message(AdminState.spam_message)
async def spam_message(message: Message, state: FSMContext):
    if not (await api_get(f"/admins/{message.from_user.id}"))["is_admin"]:
        await state.clear()
        return await message.answer("Вы не админ")
    text = message.text.strip()
    users = await api_get("/users")
    sent = failed = 0
    for uid in users["user_ids"]:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            continue
    await state.clear()
    await message.answer(f"Рассылка окончена. Отправлено: {sent}, ошибок: {failed}.")

def main():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()
