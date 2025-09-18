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

# ---------- Настройка логирования ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Настройки / файлы ----------
USERS_FILE = "users.csv"
TASKS_FILE = "tasks.csv"
TZ = ZoneInfo("Europe/Paris")  # таймзона

# Состояния ConversationHandler
REGISTER_NAME, REGISTER_SURNAME, TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME = range(6)

# ---------- CSV вспомогательные функции ----------
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
    try:
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
                                           text=f"⏰ НАПОМИНАНИЕ: Задача '{task_text}' должна быть выполнена до {deadline_display}",
                                           reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"⚠️ ПРОСРОЧЕНО: Задача '{task_text}' не выполнена к {deadline_display}")
    except Exception as e:
        logger.exception("Ошибка в send_deadline_reminder: %s", e)

def schedule_deadline_reminders_via_jobqueue(application: Application, task_id: str, chief_id: int, assignee_id: int, task_text: str, deadline_dt: datetime.datetime):
    jobq = application.job_queue
    now = datetime.datetime.now(TZ)
    r1 = deadline_dt - datetime.timedelta(days=1)
    if r1 > now:
        jobq.run_once(send_deadline_reminder, when=r1, data={"task_id": task_id, "chat_id": assignee_id, "task_text": task_text, "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"), "role": "assignee"})
    r2 = deadline_dt - datetime.timedelta(hours=1)
    if r2 > now:
        jobq.run_once(send_deadline_reminder, when=r2, data={"task_id": task_id, "chat_id": assignee_id, "task_text": task_text, "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"), "role": "assignee"})
    r3 = deadline_dt + datetime.timedelta(hours=1)
    if r3 > now:
        jobq.run_once(send_deadline_reminder, when=r3, data={"task_id": task_id, "chat_id": chief_id, "task_text": task_text, "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"), "role": "chief"})

def reload_reminders(application: Application):
    tasks = load_tasks()
    now = datetime.datetime.now(TZ)
    for t in tasks:
        try:
            if t.get("status") == "done": continue
            dl = t.get("deadline")
            if not dl: continue
            try:
                deadline_dt = datetime.datetime.fromisoformat(dl)
            except Exception:
                deadline_dt = datetime.datetime.strptime(dl, "%Y-%m-%d %H:%M")
                deadline_dt = deadline_dt.replace(tzinfo=TZ)
            if deadline_dt > now:
                schedule_deadline_reminders_via_jobqueue(application, t["id"], int(t["chief_id"]), int(t["assignee_id"]), t["text"], deadline_dt)
        except Exception as e:
            logger.exception("Ошибка при восстановлении напоминания для задачи %s: %s", t.get("id"), e)

# ---------- Регистрация ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if user:
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(f"🔑 С возвращением, {user['name']} {user['surname']}! Ты {user['role']}.", reply_markup=keyboard)
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
        await update.message.reply_text(f"👨‍💼 Привет, {name} {surname}! Ты зарегистрирован как НАЧАЛЬНИК.", reply_markup=keyboard)
    else:
        chiefs = [u for u in users if u["role"] == "chief"]
        chief_id = chiefs[0]["tg_id"] if chiefs else ""
        new_user = {"tg_id": tg_id, "name": name, "surname": surname, "role": "manager", "chief_id": chief_id}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("manager")
        await update.message.reply_text(f"👋 Привет, {name} {surname}! Ты зарегистрирован как МЕНЕДЖЕР.", reply_markup=keyboard)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# ---------- Остальные функции task, assign, deadline, mark_done, show_tasks и т.д. ----------
# вставь сюда все функции из твоего текущего кода без изменений.

# ---------- MAIN ----------
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN not found in environment variables.")
        return
    app = Application.builder().token(token).build()

    # /start всегда работает
    app.add_handler(CommandHandler("start", start))

    # Registration conversation
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True
    )

    # Task conversation
    task_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📝 Создать задачу$"), lambda u, c: task(u, c))],
        states={
            TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: task_text_handler(u, c))],
            CHOOSE_USER: [CallbackQueryHandler(assign_task, pattern="^assign:")],
            DEADLINE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_date_handler)],
            DEADLINE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_time_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True
    )

    # Добавляем все хендлеры
    app.add_handler(register_conv)
    app.add_handler(task_conv)
    app.add_handler(CallbackQueryHandler(mark_done, pattern="^done:"))
    app.add_handler(CallbackQueryHandler(change_role_callback, pattern="^role:"))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add_user", add_user_cmd))
    app.add_handler(CommandHandler("set_role", set_role_cmd))
    app.add_handler(CommandHandler("set_chief", set_chief_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Восстановление напоминаний
    reload_reminders(app)

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
