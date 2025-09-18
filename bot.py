import csv
import os
import datetime
import re
import logging
from zoneinfo import ZoneInfo  # Python 3.9+
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Файлы ----------
USERS_FILE = "users.csv"
TASKS_FILE = "tasks.csv"

TZ = ZoneInfo("Europe/Paris")

# ---------- Состояния ConversationHandler ----------
REGISTER_NAME, REGISTER_SURNAME, TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME = range(6)

# ---------- CSV ----------
def load_csv(filename, fieldnames):
    if not os.path.exists(filename):
        with open(filename, "w", newline='', encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
        return []
    with open(filename, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)

def save_csv(filename, data, fieldnames):
    with open(filename, "w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

def load_users():
    return load_csv(USERS_FILE, ["tg_id", "name", "surname", "role", "chief_id"])

def save_users(users):
    save_csv(USERS_FILE, users, ["tg_id", "name", "surname", "role", "chief_id"])

def load_tasks():
    return load_csv(TASKS_FILE, ["id", "chief_id", "assignee_id", "text", "deadline", "status"])

def save_tasks(tasks):
    save_csv(TASKS_FILE, tasks, ["id", "chief_id", "assignee_id", "text", "deadline", "status"])

# ---------- Клавиатуры ----------
def get_main_keyboard(role):
    if role == "chief":
        buttons = [
            [KeyboardButton("📝 Создать задачу"), KeyboardButton("📋 Мои задачи")],
            [KeyboardButton("👥 Сотрудники"), KeyboardButton("🔄 Изменить роли")],
            [KeyboardButton("📊 Статистика")]
        ]
    else:
        buttons = [
            [KeyboardButton("📋 Мои задачи"), KeyboardButton("✅ Выполненные")],
            [KeyboardButton("❓ Помощь")]
        ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ---------- Напоминания ----------
async def send_deadline_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    task_id = str(data.get("task_id"))
    tasks = load_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task or task.get("status") == "done":
        return
    chat_id = int(data["chat_id"])
    task_text = data["task_text"]
    deadline_display = data["deadline"]
    if data.get("role") == "assignee":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]])
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"⏰ НАПОМИНАНИЕ: Задача '{task_text}' до {deadline_display}",
                                       reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"⚠️ ПРОСРОЧЕНО: Задача '{task_text}' не выполнена к {deadline_display}")

def schedule_deadline_reminders_via_jobqueue(application: Application, task_id: str, chief_id: int, assignee_id: int,
                                             task_text: str, deadline_dt: datetime.datetime):
    now = datetime.datetime.now(TZ)
    jobq = application.job_queue
    reminders = [
        deadline_dt - datetime.timedelta(days=1),
        deadline_dt - datetime.timedelta(hours=1),
        deadline_dt + datetime.timedelta(hours=1)
    ]
    roles = ["assignee", "assignee", "chief"]
    chats = [assignee_id, assignee_id, chief_id]
    for dt, role, chat in zip(reminders, roles, chats):
        if dt > now:
            jobq.run_once(send_deadline_reminder, when=dt, data={
                "task_id": task_id,
                "chat_id": chat,
                "task_text": task_text,
                "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"),
                "role": role
            })

def reload_reminders(application: Application):
    tasks = load_tasks()
    now = datetime.datetime.now(TZ)
    for t in tasks:
        if t.get("status") == "done" or not t.get("deadline"):
            continue
        try:
            deadline_dt = datetime.datetime.fromisoformat(t["deadline"])
        except:
            deadline_dt = datetime.datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if deadline_dt > now:
            schedule_deadline_reminders_via_jobqueue(application, t["id"], int(t["chief_id"]),
                                                     int(t["assignee_id"]), t["text"], deadline_dt)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if user:
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(f"🔑 С возвращением, {user['name']} {user['surname']}! Ты {user['role']}.",
                                        reply_markup=keyboard)
        return ConversationHandler.END
    context.user_data["conversation_active"] = True
    context.user_data["tg_id"] = tg_id
    await update.message.reply_text("👋 Добро пожаловать! Введите ваше имя:")
    return REGISTER_NAME

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Введите вашу фамилию:")
    return REGISTER_SURNAME

async def register_surname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    surname = update.message.text.strip()
    context.user_data["surname"] = surname
    tg_id = context.user_data.get("tg_id")
    name = context.user_data.get("name")
    users = load_users()
    if not users:
        new_user = {"tg_id": tg_id, "name": name, "surname": surname, "role": "chief", "chief_id": ""}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("chief")
        await update.message.reply_text(f"👨‍💼 Привет, {name} {surname}! Ты зарегистрирован как НАЧАЛЬНИК.",
                                        reply_markup=keyboard)
    else:
        chiefs = [u for u in users if u["role"] == "chief"]
        chief_id = chiefs[0]["tg_id"] if chiefs else ""
        new_user = {"tg_id": tg_id, "name": name, "surname": surname, "role": "manager", "chief_id": chief_id}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("manager")
        await update.message.reply_text(f"👋 Привет, {name} {surname}! Ты зарегистрирован как МЕНЕДЖЕР.",
                                        reply_markup=keyboard)
    context.user_data.pop("tg_id", None)
    context.user_data.pop("name", None)
    context.user_data.pop("surname", None)
    context.user_data["conversation_active"] = False
    return ConversationHandler.END

# ---------- Task handlers ----------
async def task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if not user or user["role"] != "chief":
        await update.message.reply_text("❌ Только начальник может создавать задачи.")
        return ConversationHandler.END
    context.user_data["conversation_active"] = True
    await update.message.reply_text("Введите текст задачи:")
    return TASK_TEXT

async def task_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["task_text"] = update.message.text.strip()
    tg_id = str(update.effective_user.id)
    users = load_users()
    subs = [u for u in users if u["chief_id"] == tg_id and u["role"] == "manager"]
    if not subs:
        await update.message.reply_text("❌ У вас нет подчинённых.")
        context.user_data["conversation_active"] = False
        return ConversationHandler.END
    buttons = [[InlineKeyboardButton(f"👤 {u['name']} {u['surname']}", callback_data=f"assign:{u['tg_id']}")] for u in subs]
    await update.message.reply_text("Выберите сотрудника:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_USER

async def assign_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assignee_id = query.data.split(":")[1]
    context.user_data["assignee_id"] = assignee_id
    await query.edit_message_text("Введите дату дедлайна в формате ДД.MM.ГГГГ (например: 20.09.2025):")
    return DEADLINE_DATE

async def deadline_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
        await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.MM.ГГГГ:")
        return DEADLINE_DATE
    context.user_data["deadline_date"] = date_str
    await update.message.reply_text("Введите время дедлайна в формате ЧЧ:MM (например: 14:30):")
    return DEADLINE_TIME

async def deadline_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text.strip()
    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        await update.message.reply_text("❌ Неверный формат времени. Используйте ЧЧ:MM:")
        return DEADLINE_TIME
    hours, minutes = map(int, time_str.split(':'))
    day, month, year = map(int, context.user_data["deadline_date"].split('.'))
    deadline_dt = datetime.datetime(year, month, day, hours, minutes, tzinfo=TZ)
    now = datetime.datetime.now(TZ)
    if deadline_dt <= now:
        await update.message.reply_text("❌ Дедлайн должен быть в будущем. Введите дату заново:")
        return DEADLINE_DATE
    tasks = load_tasks()
    task_id = str(len(tasks) + 1)
    chief_id = str(update.effective_user.id)
    assignee_id = context.user_data.get("assignee_id")
    text = context.user_data.get("task_text", "").strip()
    deadline_iso = deadline_dt.isoformat()
    new_task = {"id": task_id, "chief_id": chief_id, "assignee_id": assignee_id,
                "text": text, "deadline": deadline_iso, "status": "new"}
    tasks.append(new_task)
    save_tasks(tasks)
    schedule_deadline_reminders_via_jobqueue(context.application, task_id, int(chief_id), int(assignee_id), text, deadline_dt)
    await update.message.reply_text(f"✅ Задача создана и отправлена менеджеру.")
    context.user_data.pop("task_text", None)
    context.user_data.pop("assignee_id", None)
    context.user_data.pop("deadline_date", None)
    context.user_data["conversation_active"] = False
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["conversation_active"] = False
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# ---------- MAIN ----------
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN not found in environment variables.")
        return
    app = Application.builder().token(token).build()

    # Registration conversation
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Task creation conversation
    task_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📝 Создать задачу$"), task)],
        states={
            TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_text_handler)],
            CHOOSE_USER: [CallbackQueryHandler(assign_task, pattern="^assign:")],
            DEADLINE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_date_handler)],
            DEADLINE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_time_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(register_conv)
    app.add_handler(task_conv)

    # CallbackQueryHandlers
    app.add_handler(CallbackQueryHandler(send_deadline_reminder))
    # ... сюда можно добавить другие CallbackQueryHandler

    reload_reminders(app)

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
