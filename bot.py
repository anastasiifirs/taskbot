import os
import logging
import datetime
from typing import Dict, Any, Optional, List

from flask import Flask
from threading import Thread

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import psycopg2
from psycopg2.extras import RealDictCursor


# ----------------- FLASK KEEP-ALIVE ДЛЯ REPLIT -----------------
app = Flask(__name__)


@app.route("/")
def home():
    return "TaskBot is running"


def _run_web():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    """
    Запускаем небольшой веб-сервер в отдельном потоке.
    Его будет пинговать UptimeRobot, чтобы Replit не засыпал.
    """
    t = Thread(target=_run_web)
    t.daemon = True
    t.start()


# ----------------- ЛОГИРОВАНИЕ И ВРЕМЕННАЯ ЗОНА -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Локальный часовой пояс (GMT+3)
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=3))

# ----------------- КОНСТАНТЫ -----------------
ROLE_EMPLOYEE = "employee"
ROLE_MANAGER = "manager"
ROLE_DIRECTOR = "director"  # директор над всеми отделами

DEFAULT_ARCHIVE_DAYS = int(os.getenv("DEFAULT_ARCHIVE_DAYS", "30"))
REMINDER_WINDOW_MINUTES = int(os.getenv("REMINDER_WINDOW_MINUTES", "60"))

# Conversation states
(
    CHOOSING_ROLE,
    ENTER_NAME,
    NEWTASK_CHOOSE_ASSIGNEE,
    NEWTASK_WAIT_TEXT,
    NEWTASK_WAIT_DEADLINE,
) = range(5)


# ----------------- РАБОТА С БД -----------------
def get_db_connection() -> psycopg2.extensions.connection:
    """
    Всегда работаем только с PostgreSQL.
    Если DATABASE_URL не задан или подключиться не удалось — падаем с ошибкой.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL не задан в переменных окружения.")

    try:
        conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.exception("Не удалось подключиться к БД")
        raise RuntimeError(f"Не удалось подключиться к БД: {e}")


def init_db_schema() -> None:
    """Создаёт таблицы, если их ещё нет, и добавляет новые поля."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                role TEXT NOT NULL DEFAULT 'employee',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        # Добавляем колонку department, если её нет (для структуры отделов)
        cur.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS department TEXT;
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                chief_id BIGINT NOT NULL,
                assignee_id BIGINT NOT NULL,
                text TEXT NOT NULL,
                deadline TIMESTAMPTZ NOT NULL,
                is_done BOOLEAN NOT NULL DEFAULT FALSE,
                done_at TIMESTAMPTZ,
                is_archived BOOLEAN NOT NULL DEFAULT FALSE,
                reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
    conn.close()
    logger.info("Схема БД инициализирована.")


# ----------------- МОДЕЛЬ: ПОЛЬЗОВАТЕЛИ -----------------
def save_user(
    user_id: int,
    full_name: str,
    username: str,
    role: Optional[str],
    department: Optional[str] = None,
) -> None:
    """
    Создаёт или обновляет пользователя.
    department можно потом править вручную в БД (через Neon).
    Если department=None, при обновлении он НЕ затирает уже существующий отдел.
    """
    if not role:
        role = ROLE_EMPLOYEE

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (id, full_name, username, role, department)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                full_name  = EXCLUDED.full_name,
                username   = EXCLUDED.username,
                role       = EXCLUDED.role,
                department = COALESCE(users.department, EXCLUDED.department)
            ;
            """,
            (user_id, full_name, username, role, department),
        )
    conn.close()


def set_user_role(user_id: int, role: str) -> None:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users
               SET role = %s
             WHERE id = %s
            """,
            (role, user_id),
        )
    conn.close()


def set_user_department(user_id: int, department: Optional[str]) -> None:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users
               SET department = %s
             WHERE id = %s
            """,
            (department, user_id),
        )
    conn.close()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users ORDER BY created_at")
        rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------- МОДЕЛЬ: НАСТРОЙКИ -----------------
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
    conn.close()
    if row:
        return row["value"]
    return default


def set_setting(key: str, value: str) -> None:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )
    conn.close()


def get_archive_days() -> int:
    val = get_setting("archive_days", str(DEFAULT_ARCHIVE_DAYS))
    try:
        return int(val)
    except Exception:
        return DEFAULT_ARCHIVE_DAYS


# ----------------- МОДЕЛЬ: ЗАДАЧИ -----------------
def create_task(
    chief_id: int,
    assignee_id: int,
    text: str,
    deadline: datetime.datetime,
) -> int:
    """Создать задачу и вернуть её ID."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (chief_id, assignee_id, text, deadline)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (chief_id, assignee_id, text, deadline),
        )
        new_id = cur.fetchone()["id"]
    conn.close()
    return int(new_id)


def list_open_tasks_for_user(user_id: int) -> List[Dict[str, Any]]:
    """Невыполненные и неархивированные задачи для конкретного сотрудника."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM tasks
            WHERE assignee_id = %s
              AND is_done = FALSE
              AND is_archived = FALSE
            ORDER BY deadline
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_open_tasks_for_assignee(assignee_id: int) -> List[Dict[str, Any]]:
    return list_open_tasks_for_user(assignee_id)


def list_open_tasks_for_department_scope(department: str) -> List[Dict[str, Any]]:
    """
    Все невыполненные задачи сотрудников менеджера:
    — с его отделом
    — И без отдела (department IS NULL).
    """
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.*
            FROM tasks t
            JOIN users u ON t.assignee_id = u.id
            WHERE t.is_archived = FALSE
              AND t.is_done = FALSE
              AND (u.department = %s OR u.department IS NULL)
            ORDER BY t.deadline
            """,
            (department,),
        )
        rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_open_tasks() -> List[Dict[str, Any]]:
    """Все невыполненные и неархивированные задачи (для директора)."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM tasks
            WHERE is_archived = FALSE
              AND is_done = FALSE
            ORDER BY deadline
            """
        )
        rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_task_done(task_id: int) -> None:
    conn = get_db_connection()
    now = datetime.datetime.now(datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
               SET is_done = TRUE,
                   done_at = %s
             WHERE id = %s
            """,
            (now, task_id),
        )
    conn.close()


def find_task(task_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_tasks_for_archiving() -> int:
    """Помечает старые задачи как архивные. Возвращает количество."""
    archive_days = get_archive_days()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=archive_days
    )
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
               SET is_archived = TRUE
             WHERE is_archived = FALSE
               AND (
                        (is_done = TRUE AND done_at < %s)
                     OR (is_done = FALSE AND deadline < %s)
                   )
            """,
            (cutoff, cutoff),
        )
        updated = cur.rowcount
    conn.close()
    return updated


def list_tasks_near_deadline() -> List[Dict[str, Any]]:
    """
    Задачи с дедлайном в ближайшее REMINDER_WINDOW_MINUTES,
    по которым ещё не было напоминания.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    window_end = now + datetime.timedelta(minutes=REMINDER_WINDOW_MINUTES)
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM tasks
             WHERE is_done = FALSE
               AND is_archived = FALSE
               AND reminder_sent = FALSE
               AND deadline BETWEEN %s AND %s
            """,
            (now, window_end),
        )
        rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_reminder_sent(task_id: int) -> None:
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET reminder_sent = TRUE WHERE id = %s", (task_id,)
        )
    conn.close()


# ----------- СТАТИСТИКА -----------


def get_user_stats(user_id: int) -> Dict[str, int]:
    """
    Статистика по задачам сотрудника:
    - total_all        — всего задач (включая архив)
    - done_all         — всего выполнено
    - open_current     — сейчас открыто (невыполненные и неархивированные)
    - done_last_30days — выполнено за последние 30 дней
    """
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total_all,
                COUNT(*) FILTER (WHERE is_done = TRUE) AS done_all,
                COUNT(*) FILTER (WHERE is_done = FALSE AND is_archived = FALSE) AS open_current,
                COUNT(*) FILTER (
                    WHERE is_done = TRUE
                      AND done_at >= NOW() - INTERVAL '30 days'
                ) AS done_last_30days
            FROM tasks
            WHERE assignee_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
    conn.close()
    if not row:
        return {
            "total_all": 0,
            "done_all": 0,
            "open_current": 0,
            "done_last_30days": 0,
        }
    return {
        "total_all": row["total_all"],
        "done_all": row["done_all"],
        "open_current": row["open_current"],
        "done_last_30days": row["done_last_30days"],
    }


# ----------------- УТИЛИТЫ ФОРМАТИРОВАНИЯ -----------------
def utc_to_local(dt: datetime.datetime) -> datetime.datetime:
    """Переводит дату/время из UTC в локальное (GMT+3)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def format_task_line(task: Dict[str, Any]) -> str:
    deadline = task["deadline"]
    if isinstance(deadline, str):
        try:
            deadline_dt = datetime.datetime.fromisoformat(deadline)
        except Exception:
            deadline_dt = datetime.datetime.now(datetime.timezone.utc)
    else:
        deadline_dt = deadline

    deadline_local = utc_to_local(deadline_dt)
    deadline_str = deadline_local.strftime("%d.%m.%Y %H:%M")
    return f"#{task['id']} до {deadline_str}: {task['text']}"


def role_human(role: str) -> str:
    return {
        ROLE_EMPLOYEE: "Сотрудник",
        ROLE_MANAGER: "Руководитель отдела",
        ROLE_DIRECTOR: "Директор",
    }.get(role, role)


def main_keyboard(role: str) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton("📋 Мои задачи")],
        [KeyboardButton("➕ Новая задача")],
        [KeyboardButton("📊 Моя статистика")],
    ]
    if role in (ROLE_MANAGER, ROLE_DIRECTOR):
        buttons.append([KeyboardButton("👥 Задачи сотрудников")])
        buttons.append([KeyboardButton("📋 Сотрудники")])
        buttons.append([KeyboardButton("📊 Статистика сотрудников")])
        buttons.append([KeyboardButton("⚙️ Настройки архивации")])

    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def get_manageable_users(manager: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Список сотрудников, которыми может управлять пользователь:
    - директор: все пользователи, кроме себя
    - руководитель: сотрудники своего отдела и без отдела (department IS NULL), кроме себя
    """
    all_users = [u for u in get_all_users() if u["id"] != manager["id"]]
    if manager["role"] == ROLE_DIRECTOR:
        return all_users
    if manager["role"] == ROLE_MANAGER:
        dept = manager.get("department")
        if not dept:
            return []
        return [
            u
            for u in all_users
            if (u.get("department") == dept or u.get("department") is None)
        ]
    return []


# ----------------- ОБРАБОТЧИКИ КОМАНД -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Всегда даём пользователю выбрать роль заново:
    Сотрудник / Руководитель отдела / Директор.
    После выбора роли попросим ввести Имя и Фамилию.
    """
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    keyboard = [
        [
            InlineKeyboardButton("Сотрудник", callback_data="role:employee"),
            InlineKeyboardButton("Руководитель отдела", callback_data="role:manager"),
        ],
        [
            InlineKeyboardButton("Директор", callback_data="role:director"),
        ],
    ]
    await update.message.reply_text(
        "Привет! Я бот для постановки задач и напоминаний.\n\n"
        "Кто вы в команде?\n"
        "Выберите роль (её всегда можно будет сменить через /start):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHOOSING_ROLE


async def set_role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обработка выбора роли. Сохраняем роль во временное хранилище и просим ввести имя.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "role:employee":
        role = ROLE_EMPLOYEE
        human = "Сотрудник"
    elif data == "role:manager":
        role = ROLE_MANAGER
        human = "Руководитель отдела"
    else:
        role = ROLE_DIRECTOR
        human = "Директор"

    context.user_data["chosen_role"] = role

    await query.edit_message_text(f"Роль выбрана: {human}.")
    await query.message.reply_text(
        "Теперь напишите, пожалуйста, ваше Имя и Фамилию, "
        "как их должны видеть руководители в списке сотрудников.\n\n"
        "Например: «Иван Иванов»"
    )
    return ENTER_NAME


async def enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Получаем от пользователя Имя и Фамилию, сохраняем пользователя в БД.
    """
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    full_name = update.message.text.strip()
    role = context.user_data.get("chosen_role", ROLE_EMPLOYEE)

    save_user(
        user_id=user.id,
        full_name=full_name,
        username=user.username or "",
        role=role,
        department=None,  # отдел при необходимости задаётся через БД или через /setdept
    )

    await update.message.reply_text(
        f"Отлично! Сохранила:\n"
        f"Роль: {role_human(role)}\n"
        f"Имя: {full_name}\n\n"
        f"Теперь можете пользоваться ботом.",
        reply_markup=main_keyboard(role),
    )

    context.user_data.pop("chosen_role", None)
    return ConversationHandler.END


# ---------- СОЗДАНИЕ НОВОЙ ЗАДАЧИ ----------
async def newtask_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    u = get_user(user.id)
    role = u.get("role") if u else ROLE_EMPLOYEE

    # РУКОВОДИТЕЛЬ ОТДЕЛА ИЛИ ДИРЕКТОР
    if role in (ROLE_MANAGER, ROLE_DIRECTOR):
        users = get_manageable_users(u)

        if role == ROLE_DIRECTOR:
            title = "Кому поставить задачу? Выберите сотрудника:"
        else:
            title = (
                "Кому из вашего отдела или из сотрудников без отдела "
                "поставить задачу?"
            )

        if not users:
            await update.message.reply_text(
                "Подходящих сотрудников пока нет. Убедитесь, что они написали боту /start "
                "и им при необходимости проставлен отдел (department) в БД.",
                reply_markup=main_keyboard(role),
            )
            return ConversationHandler.END

        keyboard = []
        for u2 in users:
            name = u2["full_name"] or (u2["username"] or str(u2["id"]))
            dept = u2.get("department")
            if dept:
                btn_text = f"{name} (отдел: {dept})"
            else:
                btn_text = f"{name} (без отдела)"
            keyboard.append(
                [InlineKeyboardButton(btn_text, callback_data=f"assignee:{u2['id']}")]
            )

        # всегда добавляем вариант «поставить задачу себе»
        keyboard.append(
            [InlineKeyboardButton("Поставить задачу себе", callback_data=f"assignee:{user.id}")]
        )

        await update.message.reply_text(
            title,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return NEWTASK_CHOOSE_ASSIGNEE

    # СОТРУДНИК — СТАВИТ ЗАДАЧУ СЕБЕ
    else:
        context.user_data["newtask"] = {
            "chief_id": user.id,
            "assignee_id": user.id,
        }
        await update.message.reply_text(
            "Напишите текст задачи одним сообщением."
        )
        return NEWTASK_WAIT_TEXT


async def newtask_choose_assignee(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    _, raw_id = data.split(":", 1)
    assignee_id = int(raw_id)

    chief_id = query.from_user.id
    context.user_data["newtask"] = {
        "chief_id": chief_id,
        "assignee_id": assignee_id,
    }

    await query.edit_message_text("Теперь отправьте текст задачи одним сообщением.")
    return NEWTASK_WAIT_TEXT


async def newtask_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    text = update.message.text.strip()
    context.user_data.setdefault("newtask", {})["text"] = text

    await update.message.reply_text(
        "Укажите дедлайн (время GMT+3) в формате ДД.ММ.ГГГГ ЧЧ:ММ или ГГГГ-ММ-ДД ЧЧ:ММ"
    )
    return NEWTASK_WAIT_DEADLINE


def parse_deadline(text: str) -> Optional[datetime.datetime]:
    """
    Парсим дедлайн, который пользователь вводит в локальном времени GMT+3,
    и переводим его в UTC для хранения в БД (TIMESTAMPTZ).
    """
    text = text.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            local_dt = dt.replace(tzinfo=LOCAL_TZ)
            return local_dt.astimezone(datetime.timezone.utc)
        except ValueError:
            continue
    return None


async def newtask_got_deadline(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message:
        return ConversationHandler.END

    dl_text = update.message.text
    deadline = parse_deadline(dl_text)
    if not deadline:
        await update.message.reply_text(
            "Не удалось распознать дату. Попробуйте ещё раз в формате ДД.ММ.ГГГГ ЧЧ:ММ (GMT+3)."
        )
        return NEWTASK_WAIT_DEADLINE

    nt = context.user_data.get("newtask", {})
    chief_id = nt.get("chief_id", update.effective_user.id)
    assignee_id = nt.get("assignee_id", update.effective_user.id)
    task_text = nt.get("text", "")

    task_id = create_task(
        chief_id=chief_id,
        assignee_id=assignee_id,
        text=task_text,
        deadline=deadline,
    )

    context.user_data["newtask"] = {}

    deadline_local = utc_to_local(deadline)
    deadline_str = deadline_local.strftime('%d.%m.%Y %H:%M')

    # Имя того, кто поставил задачу
    creator = get_user(chief_id)
    creator_name = creator["full_name"] if creator else "Неизвестно"

    assignee_mention = f"<a href='tg://user?id={assignee_id}'>сотруднику</a>"

    # Подтверждение для руководителя/директора
    await update.message.reply_html(
        f"Задача #{task_id} создана и назначена {assignee_mention}.\n"
        f"Дедлайн: {deadline_str} (GMT+3)\n"
        f"Поставил(а): {creator_name}"
    )

    # Уведомление исполнителю
    if assignee_id != update.effective_user.id:
        try:
            await context.bot.send_message(
                chat_id=assignee_id,
                text=(
                    f"Вам поставлена новая задача #{task_id}.\n"
                    f"Текст: {task_text}\n\n"
                    f"Дедлайн: {deadline_str} (GMT+3)\n"
                    f"Поставил(а): {creator_name}"
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление исполнителю: {e}")

    u = get_user(update.effective_user.id)
    role = u.get("role") if u else ROLE_EMPLOYEE
    await update.message.reply_text(
        "Что дальше?", reply_markup=main_keyboard(role)
    )
    return ConversationHandler.END


# ---------- СПИСОК ЗАДАЧ ----------
async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    tasks = list_open_tasks_for_user(user_id)

    # Если нет активных задач — сообщаем и выходим
    if not tasks:
        await update.message.reply_text("У вас нет невыполненных задач.")
        return

    lines = []
    keyboard = []

    for t in tasks:
        line = format_task_line(t)
        creator = get_user(t["chief_id"])
        creator_name = creator["full_name"] if creator else "Неизвестно"
        line += f"\nПоставил(а): {creator_name}"
        lines.append(line)
        keyboard.append(
            [InlineKeyboardButton(f"✅ #{t['id']}", callback_data=f"done:{t['id']}")]
        )

    await update.message.reply_text(
        "Ваши невыполненные задачи:\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )



async def team_tasks_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    u = get_user(user_id)
    role = u.get("role") if u else ROLE_EMPLOYEE

    if role not in (ROLE_MANAGER, ROLE_DIRECTOR):
        await update.message.reply_text("Эта функция доступна только руководителям и директору.")
        return

    users = get_manageable_users(u)

    if role == ROLE_MANAGER:
        manager_dept = u.get("department")
        if not manager_dept:
            await update.message.reply_text(
                "У вас не указан отдел. Попросите администратора/директора прописать department в БД."
            )
            return
        title = f"Выберите сотрудника своего отдела «{manager_dept}» или без отдела:"
        extra_button_text = "Все задачи моего отдела и без отдела"
        extra_button_data = "filter_assignee:dept"
    else:
        title = "Выберите сотрудника для просмотра его задач:"
        extra_button_text = "Все задачи всех сотрудников"
        extra_button_data = "filter_assignee:all"

    if not users:
        await update.message.reply_text("Сотрудников для отображения пока нет.")
        return

    keyboard = []
    for u2 in users:
        name = u2["full_name"] or (u2["username"] or str(u2["id"]))
        dept = u2.get("department")
        if dept:
            btn_text = f"{name} (отдел: {dept})"
        else:
            btn_text = f"{name} (без отдела)"
        keyboard.append(
            [InlineKeyboardButton(btn_text, callback_data=f"filter_assignee:{u2['id']}")]
        )

    keyboard.append([InlineKeyboardButton(extra_button_text, callback_data=extra_button_data)])

    await update.message.reply_text(
        title,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def team_tasks_filter_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    _, val = data.split(":", 1)
    requester_id = query.from_user.id
    requester = get_user(requester_id) or {}
    role = requester.get("role")
    requester_dept = requester.get("department")

    if val == "dept":
        if role != ROLE_MANAGER:
            await query.edit_message_text("Этот фильтр доступен только руководителям отделов.")
            return
        if not requester_dept:
            await query.edit_message_text("У вас не указан отдел.")
            return
        tasks = list_open_tasks_for_department_scope(requester_dept)
        title = f"Все невыполненные задачи вашего отдела «{requester_dept}» и сотрудников без отдела:"
    elif val == "all":
        if role != ROLE_DIRECTOR:
            await query.edit_message_text("Этот фильтр доступен только директору.")
            return
        tasks = list_all_open_tasks()
        title = "Все невыполненные задачи всех сотрудников:"
    else:
        assignee_id = int(val)
        assignee = get_user(assignee_id)
        if not assignee:
            await query.edit_message_text("Сотрудник не найден.")
            return

        if role == ROLE_MANAGER:
            if not requester_dept or not (
                assignee.get("department") == requester_dept
                or assignee.get("department") is None
            ):
                await query.edit_message_text(
                    "Вы не можете просматривать задачи этого сотрудника (он не из вашего отдела и не без отдела)."
                )
                return
        # директор видит всех

        tasks = list_open_tasks_for_assignee(assignee_id)
        name = assignee["full_name"] or (assignee["username"] or str(assignee["id"]))
        title = f"Невыполненные задачи сотрудника {name}:"

    if not tasks:
        await query.edit_message_text("Нет невыполненных задач по выбранному фильтру.")
        return

    lines = []
    keyboard = []
    for t in tasks:
        line = format_task_line(t)
        assignee = get_user(t["assignee_id"])
        creator = get_user(t["chief_id"])

        assignee_name = assignee["full_name"] if assignee else "Неизвестно"
        creator_name = creator["full_name"] if creator else "Неизвестно"

        line += f"\nИсполнитель: {assignee_name}"
        line += f"\nПоставил(а): {creator_name}"

        lines.append(line)
        keyboard.append(
            [InlineKeyboardButton(f"✅ #{t['id']}", callback_data=f"done:{t['id']}")]
        )



# ---------- ОТМЕТКА О ВЫПОЛНЕНИИ ----------
async def mark_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    _, raw_id = data.split(":", 1)
    task_id = int(raw_id)

    task = find_task(task_id)
    if not task:
        await query.edit_message_text("Задача не найдена.")
        return

    user_id = query.from_user.id
    if user_id not in (task["assignee_id"], task["chief_id"]):
        await query.answer(
            "Завершать задачу может только исполнитель или руководитель, который поставил задачу.",
            show_alert=True,
        )
        return

    mark_task_done(task_id)

    await query.edit_message_text(f"Задача #{task_id} отмечена как выполненная.")
    other = task["chief_id"] if user_id == task["assignee_id"] else task["assignee_id"]
    try:
        await context.bot.send_message(
            chat_id=other, text=f"Задача #{task_id} была отмечена как выполненная."
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о выполнении: {e}")


# ---------- СТАТИСТИКА ДЛЯ СОТРУДНИКОВ И РУКОВОДИТЕЛЕЙ ----------

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    stats = get_user_stats(user.id)

    text = (
        f"📊 Ваша статистика по задачам:\n\n"
        f"Всего задач (за всё время): {stats['total_all']}\n"
        f"Выполнено (за всё время): {stats['done_all']}\n"
        f"Сейчас открыто (невыполненные, не в архиве): {stats['open_current']}\n"
        f"Выполнено за последние 30 дней: {stats['done_last_30days']}\n"
    )
    await update.message.reply_text(text)


async def team_stats_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Для руководителей: выбрать сотрудника и посмотреть его статистику."""
    user_id = update.effective_user.id
    u = get_user(user_id)
    role = u.get("role") if u else ROLE_EMPLOYEE

    if role not in (ROLE_MANAGER, ROLE_DIRECTOR):
        await update.message.reply_text("Статистика по сотрудникам доступна только руководителям и директору.")
        return

    users = get_manageable_users(u)
    if not users:
        await update.message.reply_text("Нет сотрудников для отображения статистики.")
        return

    keyboard = []
    for u2 in users:
        name = u2["full_name"] or (u2["username"] or str(u2["id"]))
        dept = u2.get("department")
        if dept:
            btn_text = f"{name} (отдел: {dept})"
        else:
            btn_text = f"{name} (без отдела)"
        keyboard.append(
            [InlineKeyboardButton(btn_text, callback_data=f"stats_for:{u2['id']}")]
        )

    await update.message.reply_text(
        "Выберите сотрудника для просмотра его статистики:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def team_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показ статистики по выбранному сотруднику (для руководителей/директора)."""
    query = update.callback_query
    await query.answer()
    data = query.data
    _, raw_id = data.split(":", 1)
    assignee_id = int(raw_id)

    assignee = get_user(assignee_id)
    if not assignee:
        await query.edit_message_text("Сотрудник не найден.")
        return

    stats = get_user_stats(assignee_id)
    name = assignee["full_name"] or (assignee["username"] or str(assignee["id"]))
    dept = assignee.get("department") or "без отдела"
    r = role_human(assignee.get("role", ROLE_EMPLOYEE))

    text = (
        f"📊 Статистика по сотруднику {name}\n"
        f"Роль: {r}\n"
        f"Отдел: {dept}\n\n"
        f"Всего задач (за всё время): {stats['total_all']}\n"
        f"Выполнено (за всё время): {stats['done_all']}\n"
        f"Сейчас открыто (невыполненные, не в архиве): {stats['open_current']}\n"
        f"Выполнено за последние 30 дней: {stats['done_last_30days']}\n"
    )

    await query.edit_message_text(text)


# ---------- СПИСОК СОТРУДНИКОВ И УПРАВЛЕНИЕ РОЛЯМИ/ОТДЕЛАМИ ----------

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список сотрудников для руководителя/директора."""
    user_id = update.effective_user.id
    u = get_user(user_id)
    role = u.get("role") if u else ROLE_EMPLOYEE

    if role not in (ROLE_MANAGER, ROLE_DIRECTOR):
        await update.message.reply_text("Список сотрудников доступен только руководителям и директору.")
        return

    users = get_manageable_users(u) if role == ROLE_MANAGER else [uu for uu in get_all_users()]
    if not users:
        await update.message.reply_text("Сотрудников пока нет.")
        return

    lines = []
    for u2 in users:
        rid = u2["id"]
        name = u2["full_name"] or (u2["username"] or str(rid))
        dept = u2.get("department") or "без отдела"
        r = role_human(u2.get("role", ROLE_EMPLOYEE))
        lines.append(f"{name} — {r}, отдел: {dept}, id: {rid}")

    text = (
        "📋 Список сотрудников:\n\n" +
        "\n".join(lines) +
        "\n\nДля изменения роли: /setrole <id> <employee|manager|director>\n"
        "Для изменения отдела: /setdept <id> <название отдела или none>"
    )
    await update.message.reply_text(text)


async def set_role_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /setrole <id> <role> — только для директора."""
    user_id = update.effective_user.id
    u = get_user(user_id)
    if not u or u.get("role") != ROLE_DIRECTOR:
        await update.message.reply_text("Менять роли может только директор.")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "Использование: /setrole <telegram_id> <employee|manager|director>"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("telegram_id должен быть числом.")
        return

    new_role = args[1].strip()
    if new_role not in (ROLE_EMPLOYEE, ROLE_MANAGER, ROLE_DIRECTOR):
        await update.message.reply_text(
            "Роль должна быть одной из: employee, manager, director."
        )
        return

    target = get_user(target_id)
    if not target:
        await update.message.reply_text("Сотрудник с таким id не найден.")
        return

    set_user_role(target_id, new_role)
    await update.message.reply_text(
        f"Роль пользователя {target_id} обновлена на: {role_human(new_role)}."
    )


async def set_dept_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /setdept <id> <department or none> — только для директора."""
    user_id = update.effective_user.id
    u = get_user(user_id)
    if not u or u.get("role") != ROLE_DIRECTOR:
        await update.message.reply_text("Менять отделы может только директор.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /setdept <telegram_id> <название отдела или none>\n"
            "Пример: /setdept 123456 Отдел продаж"
        )
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("telegram_id должен быть числом.")
        return

    dept_raw = " ".join(args[1:]).strip()
    department = None if dept_raw.lower() == "none" else dept_raw

    target = get_user(target_id)
    if not target:
        await update.message.reply_text("Сотрудник с таким id не найден.")
        return

    set_user_department(target_id, department)
    if department is None:
        await update.message.reply_text(
            f"Отдел у пользователя {target_id} сброшен (теперь без отдела)."
        )
    else:
        await update.message.reply_text(
            f"Отдел пользователя {target_id} установлен: {department}."
        )


# ---------- НАСТРОЙКИ АРХИВАЦИИ ----------
async def archive_settings_entry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id
    u = get_user(user_id)
    role = u.get("role") if u else ROLE_EMPLOYEE
    if role not in (ROLE_MANAGER, ROLE_DIRECTOR):
        await update.message.reply_text("Настройки архивации доступны только руководителям и директору.")
        return

    current_days = get_archive_days()
    keyboard = [
        [
            InlineKeyboardButton("15 дней", callback_data="arch_days:15"),
            InlineKeyboardButton("30 дней", callback_data="arch_days:30"),
            InlineKeyboardButton("60 дней", callback_data="arch_days:60"),
        ]
    ]
    await update.message.reply_text(
        f"Через сколько дней старые задачи будут попадать в архив и не показываться в списке?\n"
        f"Сейчас установлено: {current_days} дн.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def archive_settings_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    _, raw_val = query.data.split(":", 1)
    days = int(raw_val)
    set_setting("archive_days", str(days))
    await query.edit_message_text(
        f"Период архивации задач установлен: {days} дней.\n"
        f"Все задачи, которые старше этого срока (выполненные или сильно просроченные), "
        f"будут автоматически убираться из списка."
    )


# ---------- ПЛАНОВЫЕ ЗАДАЧИ (REMINDERS & ARCHIVE) ----------
async def scheduled_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = list_tasks_near_deadline()
    if not tasks:
        return

    for t in tasks:
        task_id = t["id"]
        deadline = t["deadline"]
        if isinstance(deadline, str):
            try:
                deadline_dt = datetime.datetime.fromisoformat(deadline)
            except Exception:
                deadline_dt = datetime.datetime.now(datetime.timezone.utc)
        else:
            deadline_dt = deadline

        deadline_local = utc_to_local(deadline_dt)
        deadline_str = deadline_local.strftime("%d.%m.%Y %H:%M")

        msg = (
            f"Напоминание о задаче #{task_id}:\n"
            f"{t['text']}\n\n"
            f"Дедлайн: {deadline_str} (GMT+3)"
        )
        # Исполнитель
        try:
            await context.bot.send_message(chat_id=t["assignee_id"], text=msg)
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание исполнителю: {e}")
        # Руководитель, который поставил задачу
        if t["chief_id"] != t["assignee_id"]:
            try:
                await context.bot.send_message(chat_id=t["chief_id"], text=msg)
            except Exception as e:
                logger.warning(f"Не удалось отправить напоминание руководителю: {e}")

        set_reminder_sent(task_id)


async def scheduled_archive(context: ContextTypes.DEFAULT_TYPE) -> None:
    count = mark_tasks_for_archiving()
    if count:
        logger.info(f"Автоархивация задач: помечено {count} шт.")


# ---------- ПРОЧЕЕ ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    archive_days = get_archive_days()
    text = (
        "Я бот для постановки задач внутри команды и напоминаний о дедлайнах.\n\n"
        "Роли:\n"
        "• Сотрудник — видит свои задачи и ставит задачи только себе.\n"
        "• Руководитель отдела — видит задачи своего отдела и сотрудников без отдела,\n"
        "  может ставить им задачи и смотреть статистику.\n"
        "• Директор — видит задачи всех, может ставить задачи любому сотруднику,\n"
        "  управлять ролями и отделами.\n\n"
        "Основные команды и кнопки:\n"
        "• /start — выбор роли и ввод ФИО (можно сменить в любой момент).\n"
        "• 📋 Мои задачи — список ваших невыполненных задач.\n"
        "• ➕ Новая задача — создать задачу.\n"
        "• 📊 Моя статистика — ваша личная статистика по задачам.\n"
        "• 👥 Задачи сотрудников — задачи сотрудников (для руководителей/директора).\n"
        "• 📋 Сотрудники — список сотрудников (для руководителей/директора).\n"
        "• 📊 Статистика сотрудников — статистика по каждому сотруднику.\n"
        "• ⚙️ Настройки архивации — период, через который задачи попадают в архив.\n\n"
        "Управление через команды (для директора):\n"
        "• /setrole <id> <employee|manager|director> — изменить роль сотрудника.\n"
        "• /setdept <id> <название отдела или none> — назначить или убрать отдел.\n\n"
        f"Сейчас задачи автоматически архивируются через {archive_days} дн. "
        f"после выполнения или сильной просрочки. Время дедлайнов — GMT+3."
    )
    await update.message.reply_text(text)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка текстовых сообщений с клавиатуры."""
    text = update.message.text.strip()
    if text == "📋 Мои задачи":
        await my_tasks(update, context)
    elif text == "➕ Новая задача":
        await newtask_entry(update, context)
    elif text == "📊 Моя статистика":
        await my_stats(update, context)
    elif text == "👥 Задачи сотрудников":
        await team_tasks_entry(update, context)
    elif text == "📋 Сотрудники":
        await list_users(update, context)
    elif text == "📊 Статистика сотрудников":
        await team_stats_entry(update, context)
    elif text == "⚙️ Настройки архивации":
        await archive_settings_entry(update, context)
    else:
        await update.message.reply_text(
            "Не понял команду. Используйте кнопки или /help."
        )


# ----------------- MAIN -----------------
def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN не задан в переменных окружения.")

    # Инициализируем БД (Neon) и таблицы
    init_db_schema()

    application: Application = ApplicationBuilder().token(token).build()

    # Conversation для выбора роли + ввода имени
    role_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_ROLE: [
                CallbackQueryHandler(
                    set_role_callback,
                    pattern=r"^role:(employee|manager|director)$",
                )
            ],
            ENTER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # Conversation для новой задачи
    newtask_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newtask", newtask_entry),
            MessageHandler(
                filters.TEXT & filters.Regex("^➕ Новая задача$"), newtask_entry
            ),
        ],
        states={
            NEWTASK_CHOOSE_ASSIGNEE: [
                CallbackQueryHandler(newtask_choose_assignee, pattern=r"^assignee:")
            ],
            NEWTASK_WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, newtask_got_text)
            ],
            NEWTASK_WAIT_DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, newtask_got_deadline)
            ],
        },
        fallbacks=[],
    )

    application.add_handler(role_conv)
    application.add_handler(newtask_conv)

    # Остальные хендлеры
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("tasks", my_tasks))
    application.add_handler(CommandHandler("my_stats", my_stats))
    application.add_handler(CommandHandler("team_stats", team_stats_entry))
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("setrole", set_role_command))
    application.add_handler(CommandHandler("setdept", set_dept_command))

    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex("^📋 Мои задачи$"), my_tasks)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex("^📊 Моя статистика$"), my_stats)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^👥 Задачи сотрудников$"),
            team_tasks_entry,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^📋 Сотрудники$"),
            list_users,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^📊 Статистика сотрудников$"),
            team_stats_entry,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^⚙️ Настройки архивации$"),
            archive_settings_entry,
        )
    )

    application.add_handler(
        CallbackQueryHandler(team_tasks_filter_callback, pattern=r"^filter_assignee:")
    )
    application.add_handler(
        CallbackQueryHandler(mark_done_callback, pattern=r"^done:\d+")
    )
    application.add_handler(
        CallbackQueryHandler(archive_settings_callback, pattern=r"^arch_days:")
    )
    application.add_handler(
        CallbackQueryHandler(team_stats_callback, pattern=r"^stats_for:")
    )

    # Роутер для всех остальных текстовых сообщений (кнопки/свободный текст)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_router)
    )

    # Плановые задачи — напоминания и архивация
    job_queue = application.job_queue
    job_queue.run_repeating(scheduled_reminders, interval=300, first=60)
    job_queue.run_repeating(scheduled_archive, interval=3600, first=120)

    logger.info(
        "Бот запущен. Не забудьте настроить UptimeRobot для keep-alive, "
        "если запускаете его на Replit."
    )

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    keep_alive()  # для Replit
    main()
