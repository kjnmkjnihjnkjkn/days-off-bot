"""
🤖 Telegram-бот «Выходные сотрудников»
Оптимизированная версия для деплоя на Render
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional
import random

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import asyncpg
from asyncpg import Pool

# ============================================================================
# КОНФИГУРАЦИЯ (берётся из переменных окружения)
# ============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip()]

DEADLINE_HOUR = int(os.getenv("DEADLINE_HOUR", "18"))
MAX_WORKERS_PER_DAY = None  # Без лимита
AUTO_ASSIGN_ENABLED = os.getenv("AUTO_ASSIGN", "true").lower() == "true"

DAYS = {
    "mon": "Пн",
    "tue": "Вт", 
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс"
}

DAYS_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# ============================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# БАЗА ДАННЫХ
# ============================================================================

class Database:
    def __init__(self, pool: Pool):
        self.pool = pool
    
    async def init_tables(self):
        """Создание таблиц БД"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    username TEXT,
                    role TEXT NOT NULL DEFAULT 'worker',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS weeks (
                    id SERIAL PRIMARY KEY,
                    week_start_date DATE NOT NULL,
                    week_end_date DATE NOT NULL,
                    deadline_datetime TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS day_off_requests (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    week_id INTEGER NOT NULL REFERENCES weeks(id),
                    days_off TEXT[] NOT NULL,
                    confirmed_at TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, week_id)
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS violations_stats (
                    user_id BIGINT PRIMARY KEY REFERENCES users(id),
                    late_count INTEGER DEFAULT 0,
                    missed_count INTEGER DEFAULT 0,
                    auto_assigned_count INTEGER DEFAULT 0
                )
            """)
    
    async def get_or_create_user(self, user_id: int, name: str, username: str = None) -> dict:
        """Получить или создать пользователя"""
        async with self.pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id = $1", user_id
            )
            if not user:
                await conn.execute(
                    "INSERT INTO users (id, name, username, role) VALUES ($1, $2, $3, 'worker')",
                    user_id, name, username
                )
                await conn.execute(
                    "INSERT INTO violations_stats (user_id) VALUES ($1)",
                    user_id
                )
                user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            return dict(user)
    
    async def get_current_week(self) -> Optional[dict]:
        """Получить текущую неделю"""
        async with self.pool.acquire() as conn:
            week = await conn.fetchrow("""
                SELECT * FROM weeks 
                WHERE week_start_date <= CURRENT_DATE 
                AND week_end_date >= CURRENT_DATE
                ORDER BY week_start_date DESC LIMIT 1
            """)
            return dict(week) if week else None
    
    async def create_new_week(self):
        """Создать новую неделю"""
        today = datetime.now().date()
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        
        week_start = today + timedelta(days=days_until_monday)
        week_end = week_start + timedelta(days=6)
        deadline = datetime.combine(week_start - timedelta(days=1), datetime.min.time()).replace(hour=DEADLINE_HOUR)
        
        async with self.pool.acquire() as conn:
            week_id = await conn.fetchval("""
                INSERT INTO weeks (week_start_date, week_end_date, deadline_datetime)
                VALUES ($1, $2, $3)
                RETURNING id
            """, week_start, week_end, deadline)
            
        logger.info(f"Создана новая неделя {week_id}: {week_start} - {week_end}")
        return week_id
    
    async def get_user_request(self, user_id: int, week_id: int) -> Optional[dict]:
        """Получить запрос пользователя на неделю"""
        async with self.pool.acquire() as conn:
            req = await conn.fetchrow("""
                SELECT * FROM day_off_requests 
                WHERE user_id = $1 AND week_id = $2
            """, user_id, week_id)
            return dict(req) if req else None
    
    async def save_days_off(self, user_id: int, week_id: int, days: List[str], status: str = 'pending'):
        """Сохранить выбор дней"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO day_off_requests (user_id, week_id, days_off, status)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, week_id) 
                DO UPDATE SET days_off = $3, status = $4
            """, user_id, week_id, days, status)
    
    async def confirm_days_off(self, user_id: int, week_id: int, is_late: bool = False):
        """Подтвердить выбор"""
        status = 'late' if is_late else 'ok'
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE day_off_requests 
                SET confirmed_at = CURRENT_TIMESTAMP, status = $3
                WHERE user_id = $1 AND week_id = $2
            """, user_id, week_id, status)
            
            if is_late:
                await conn.execute("""
                    UPDATE violations_stats 
                    SET late_count = late_count + 1
                    WHERE user_id = $1
                """, user_id)
    
    async def get_week_status(self, week_id: int) -> List[dict]:
        """Получить статус всех работников на неделю"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT u.id, u.name, u.username, d.days_off, d.confirmed_at, d.status
                FROM users u
                LEFT JOIN day_off_requests d ON u.id = d.user_id AND d.week_id = $1
                WHERE u.role = 'worker'
                ORDER BY u.name
            """, week_id)
            return [dict(row) for row in rows]
    
    async def get_user_stats(self, user_id: int) -> dict:
        """Получить статистику пользователя"""
        async with self.pool.acquire() as conn:
            stats = await conn.fetchrow(
                "SELECT * FROM violations_stats WHERE user_id = $1", user_id
            )
            return dict(stats) if stats else {}
    
    async def auto_assign_days(self, user_id: int, week_id: int):
        """Автоназначение выходных"""
        days = ["sun", "mon"]
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO day_off_requests (user_id, week_id, days_off, confirmed_at, status)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP, 'auto')
                ON CONFLICT (user_id, week_id) DO NOTHING
            """, user_id, week_id, days)
            
            await conn.execute("""
                UPDATE violations_stats 
                SET missed_count = missed_count + 1, auto_assigned_count = auto_assigned_count + 1
                WHERE user_id = $1
            """, user_id)

# ============================================================================
# FSM СОСТОЯНИЯ
# ============================================================================

class SelectDaysStates(StatesGroup):
    selecting = State()

# ============================================================================
# КЛАВИАТУРЫ
# ============================================================================

def get_days_keyboard(selected_days: List[str], week_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора дней"""
    buttons = []
    row = []
    
    for i, (day_code, day_name) in enumerate(DAYS.items()):
        if day_code in selected_days:
            button_text = f"✅ {day_name}"
        else:
            button_text = day_name
        
        callback = f"day:{day_code}" if len(selected_days) < 2 or day_code in selected_days else "day:blocked"
        
        row.append(InlineKeyboardButton(text=button_text, callback_data=callback))
        
        if len(row) == 3 or i == len(DAYS) - 1:
            buttons.append(row)
            row = []
    
    if len(selected_days) == 2:
        buttons.append([InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm")])
    
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ============================================================================
# РОУТЕРЫ
# ============================================================================

router = Router()
db: Database = None
bot: Bot = None

# ============================================================================
# КОМАНДЫ
# ============================================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Приветствие"""
    user = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username
    )
    
    if user['role'] == 'admin' or message.from_user.id in ADMIN_IDS:
        text = (
            "👨‍💼 Добро пожаловать, администратор!\n\n"
            "Доступные команды:\n"
            "/status - статус выбора выходных\n"
            "/stats - статистика по работнику\n"
            "/select - выбрать свои выходные"
        )
    else:
        text = (
            "👷 Привет! Я помогу тебе выбрать выходные.\n\n"
            "Каждую неделю нужно выбрать ровно 2 выходных дня.\n"
            "Используй /select чтобы начать выбор."
        )
    
    await message.answer(text)


@router.message(Command("select"))
async def cmd_select(message: Message, state: FSMContext):
    """Начать выбор выходных"""
    user = await db.get_or_create_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username
    )
    
    week = await db.get_current_week()
    if not week:
        week_id = await db.create_new_week()
        week = await db.get_current_week()
    
    now = datetime.now()
    deadline = week['deadline_datetime']
    
    if now > deadline:
        await message.answer("⚠️ Дедлайн истёк! Обратись к администратору.")
        return
    
    request = await db.get_user_request(message.from_user.id, week['id'])
    selected = request['days_off'] if request else []
    
    await state.set_state(SelectDaysStates.selecting)
    await state.update_data(selected_days=selected, week_id=week['id'])
    
    text = (
        f"📅 Выбери 2 выходных дня на неделю {week['week_start_date']} - {week['week_end_date']}\n"
        f"⏱ Дедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"Выбрано: {len(selected)} / 2"
    )
    
    keyboard = get_days_keyboard(selected, week['id'])
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("day:"))
async def process_day_selection(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора дня"""
    day_code = callback.data.split(":")[1]
    
    if day_code == "blocked":
        easter_eggs = [
            "🙈 Эй-эй, только 2 дня! Не жадничай 😄",
            "🎯 Два. Выходных. Два! Не три, не четыре... ДВА!",
            "🤷‍♂️ Хочешь больше выходных? Поговори с боссом! 😅",
            "⚠️ Система перегрузки обнаружена! Вернись к двум дням 🤖",
        ]
        await callback.answer(random.choice(easter_eggs), show_alert=True)
        return
    
    data = await state.get_data()
    selected = data.get('selected_days', [])
    week_id = data.get('week_id')
    
    if day_code in selected:
        selected.remove(day_code)
    else:
        if len(selected) < 2:
            selected.append(day_code)
    
    if selected:
        await db.save_days_off(callback.from_user.id, week_id, selected, 'pending')
    
    await state.update_data(selected_days=selected)
    
    text = (
        f"📅 Выбери 2 выходных дня\n"
        f"Выбрано: {', '.join([DAYS[d] for d in selected]) if selected else 'нет'} ({len(selected)} / 2)"
    )
    
    keyboard = get_days_keyboard(selected, week_id)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "confirm")
async def confirm_selection(callback: CallbackQuery, state: FSMContext):
    """Подтверждение выбора"""
    data = await state.get_data()
    selected = data.get('selected_days', [])
    week_id = data.get('week_id')
    
    if len(selected) != 2:
        await callback.answer("⚠️ Нужно выбрать ровно 2 дня!", show_alert=True)
        return
    
    week = await db.get_current_week()
    is_late = datetime.now() > week['deadline_datetime']
    
    await db.confirm_days_off(callback.from_user.id, week_id, is_late)
    await state.clear()
    
    status_emoji = "⚠️" if is_late else "✅"
    status_text = "опоздал" if is_late else "вовремя"
    
    days_text = ", ".join([DAYS[d] for d in selected])
    text = (
        f"{status_emoji} Выходные сохранены!\n\n"
        f"📅 Дни: {days_text}\n"
        f"⏱ Зафиксировано: {datetime.now().strftime('%H:%M')}\n"
        f"📊 Статус: {status_text}"
    )
    
    if set(selected) == {"sat", "sun"}:
        text += "\n\n🎉 Классика! Суббота + Воскресенье - выбор чемпионов!"
    
    await callback.message.edit_text(text)
    await callback.answer("Сохранено!", show_alert=False)


@router.callback_query(F.data == "cancel")
async def cancel_selection(callback: CallbackQuery, state: FSMContext):
    """Отмена выбора"""
    await state.clear()
    await callback.message.edit_text("❌ Выбор отменён")
    await callback.answer()


@router.message(Command("status"))
async def cmd_status(message: Message):
    """Статус выбора выходных"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔️ Эта команда доступна только администраторам")
        return
    
    week = await db.get_current_week()
    if not week:
        await message.answer("❌ Активная неделя не найдена")
        return
    
    status = await db.get_week_status(week['id'])
    
    text = f"📊 Статус на неделю {week['week_start_date']} - {week['week_end_date']}\n\n"
    
    for user in status:
        if user['confirmed_at']:
            days = ", ".join([DAYS[d] for d in user['days_off']])
            time = user['confirmed_at'].strftime('%H:%M')
            
            if user['status'] == 'late':
                emoji = "⚠️"
            elif user['status'] == 'auto':
                emoji = "🤖"
            else:
                emoji = "✅"
            
            text += f"{emoji} {user['name']} — {days} ({time})\n"
        elif user['days_off']:
            days = ", ".join([DAYS[d] for d in user['days_off']])
            text += f"⏳ {user['name']} — {days} (не подтвердил)\n"
        else:
            text += f"❌ {user['name']} — не выбрал\n"
    
    await message.answer(text)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Статистика работника"""
    stats = await db.get_user_stats(message.from_user.id)
    
    text = (
        f"📊 Твоя статистика:\n\n"
        f"⚠️ Опозданий: {stats.get('late_count', 0)}\n"
        f"❌ Не сдал: {stats.get('missed_count', 0)}\n"
        f"🤖 Автоназначено: {stats.get('auto_assigned_count', 0)}"
    )
    
    await message.answer(text)


# ============================================================================
# ПАСХАЛКИ
# ============================================================================

@router.message(Command("кофе", "coffee"))
async def secret_coffee(message: Message):
    responses = [
        "☕️ Держи виртуальный кофе! Выходные уже близко...",
        "☕️ Кофе готов! Кстати, ты уже выбрал выходные? 👀",
        "☕️ *наливает кофе* Теперь иди выбирай 2 выходных!",
        "☕️ Эспрессо или выходные в субботу-воскресенье?",
    ]
    await message.answer(random.choice(responses))


@router.message(Command("мотивация", "motivation"))
async def secret_motivation(message: Message):
    quotes = [
        "💪 «Не откладывай на завтра то, что можно сделать в выходные»",
        "🔥 «Понедельник — это просто вторая попытка воскресенья»",
        "⭐️ «Работай усердно, отдыхай с умом — выбирай выходные вовремя!»",
        "🚀 «Ты можешь всё! Даже выбрать выходные до дедлайна»",
    ]
    await message.answer(random.choice(quotes))


@router.message(Command("secret"))
async def secret_command(message: Message):
    await message.answer(
        "🎮 Ты нашёл секретную команду!\n\n"
        "Доступные пасхалки:\n"
        "/кофе - получить виртуальный кофе\n"
        "/мотивация - мотивационная цитата\n\n"
        "Попробуй выбрать Сб+Вс для сюрприза! 😉"
    )


# ============================================================================
# ПЛАНИРОВЩИК
# ============================================================================

async def create_new_week_job():
    """Создание новой недели"""
    logger.info("Создание новой недели...")
    await db.create_new_week()
    
    async with db.pool.acquire() as conn:
        workers = await conn.fetch("SELECT id FROM users WHERE role = 'worker'")
        
        for worker in workers:
            try:
                await bot.send_message(
                    worker['id'],
                    "📅 Новая неделя началась! Не забудь выбрать 2 выходных дня.\n"
                    "Используй /select"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление {worker['id']}: {e}")


async def check_deadline_job():
    """Проверка дедлайна"""
    logger.info("Проверка дедлайна...")
    
    week = await db.get_current_week()
    if not week:
        return
    
    status = await db.get_week_status(week['id'])
    violators = []
    
    for user in status:
        if not user['confirmed_at']:
            violators.append(user)
            
            if AUTO_ASSIGN_ENABLED:
                await db.auto_assign_days(user['id'], week['id'])
                try:
                    await bot.send_message(
                        user['id'],
                        "❗️ Ты не выбрал выходные вовремя!\n"
                        "🤖 Автоматически назначены: Вс, Пн"
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить {user['id']}: {e}")
    
    if violators:
        names = "\n".join([f"– {u['name']}" for u in violators])
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"⚠️ Не выбрали выходные вовремя:\n{names}"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить админу {admin_id}: {e}")


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

async def main():
    global db, bot
    
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен!")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не установлен!")
    
    pool = await asyncpg.create_pool(DATABASE_URL)
    db = Database(pool)
    await db.init_tables()
    
    week = await db.get_current_week()
    if not week:
        await db.create_new_week()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(create_new_week_job, CronTrigger(day_of_week='sat', hour=0, minute=0))
    scheduler.add_job(check_deadline_job, CronTrigger(day_of_week='sun', hour=18, minute=1))
    scheduler.start()
    
    logger.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())