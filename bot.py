import csv
import os
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

USERS_FILE = "users.csv"
TASKS_FILE = "tasks.csv"

scheduler = BackgroundScheduler()
scheduler.start()

# --- Состояния для ConversationHandler ---
TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME, CHOOSE_USER_FOR_ROLE, CONFIRM_ROLE_CHANGE = range(6)

# --- Работа с CSV ---
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
    return load_csv(USERS_FILE, ["tg_id", "role", "chief_id"])

def save_users(users):
    save_csv(USERS_FILE, users, ["tg_id", "role", "chief_id"])

def load_tasks():
    return load_csv(TASKS_FILE, ["id", "chief_id", "assignee_id", "text", "deadline", "status"])

def save_tasks(tasks):
    save_csv(TASKS_FILE, tasks, ["id", "chief_id", "assignee_id", "text", "deadline", "status"])

# --- Клавиатуры ---
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

# --- Напоминания о дедлайнах ---
async def send_deadline_reminder(context):
    task_id = context.job.data["task_id"]
    chat_id = context.job.data["chat_id"]
    task_text = context.job.data["task_text"]
    deadline = context.job.data["deadline"]
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ НАПОМИНАНИЕ: Задача '{task_text}' должна быть выполнена до {deadline}"
    )

def schedule_deadline_reminders(task_id, chief_id, assignee_id, task_text, deadline_str):
    deadline = datetime.datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
    
    # Напоминание за 1 день
    reminder_1_day = deadline - datetime.timedelta(days=1)
    if reminder_1_day > datetime.datetime.now():
        scheduler.add_job(
            send_deadline_reminder,
            DateTrigger(run_date=reminder_1_day),
            kwargs={
                "data": {
                    "task_id": task_id,
                    "chat_id": assignee_id,
                    "task_text": task_text,
                    "deadline": deadline_str
                }
            }
        )
    
    # Напоминание за 1 час
    reminder_1_hour = deadline - datetime.timedelta(hours=1)
    if reminder_1_hour > datetime.datetime.now():
        scheduler.add_job(
            send_deadline_reminder,
            DateTrigger(run_date=reminder_1_hour),
            kwargs={
                "data": {
                    "task_id": task_id,
                    "chat_id": assignee_id,
                    "task_text": task_text,
                    "deadline": deadline_str
                }
            }
        )
    
    # Напоминание начальнику после дедлайна
    reminder_after = deadline + datetime.timedelta(hours=1)
    scheduler.add_job(
        send_deadline_reminder,
        DateTrigger(run_date=reminder_after),
        kwargs={
            "data": {
                "task_id": task_id,
                "chat_id": chief_id,
                "task_text": task_text,
                "deadline": f"{deadline_str} (ПРОСРОЧЕНО)"
            }
        }
    )

# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)

    if not users:  # первый пользователь становится начальником
        new_user = {"tg_id": tg_id, "role": "chief", "chief_id": ""}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("chief")
        await update.message.reply_text(
            "👨‍💼 Привет! Ты зарегистрирован как НАЧАЛЬНИК.\n\n"
            "Используй кнопки ниже для управления задачами:",
            reply_markup=keyboard
        )
        return

    if not user:
        chiefs = [u for u in users if u["role"] == "chief"]
        chief_id = chiefs[0]["tg_id"] if chiefs else ""
        new_user = {"tg_id": tg_id, "role": "manager", "chief_id": chief_id}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("manager")
        await update.message.reply_text(
            "👋 Привет! Ты зарегистрирован как МЕНЕДЖЕР.\n\n"
            "Используй кнопки ниже для работы с задачами:",
            reply_markup=keyboard
        )
    else:
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(
            f"🔑 С возвращением! Ты {user['role']}.",
            reply_markup=keyboard
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if user and user["role"] == "chief":
        help_text = (
            "👨‍💼 Команды для начальника:\n\n"
            "📝 Создать задачу - назначить новую задачу сотруднику\n"
            "📋 Мои задачи - просмотреть все созданные задачи\n"
            "👥 Сотрудники - список ваших подчиненных\n"
            "🔄 Изменить роли - изменить роль сотрудника\n"
            "📊 Статистика - отчет по выполнению задач"
        )
    else:
        help_text = (
            "👨‍💼 Команды для сотрудника:\n\n"
            "📋 Мои задачи - просмотреть текущие задачи\n"
            "✅ Выполненные - просмотреть выполненные задачи\n"
            "❓ Помощь - показать эту справку"
        )
    
    await update.message.reply_text(help_text)

# --- Создание задачи ---
async def task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)

    if not user or user["role"] != "chief":
        await update.message.reply_text("❌ Только начальник может создавать задачи.")
        return ConversationHandler.END

    await update.message.reply_text("Введите текст задачи:")
    return TASK_TEXT

async def task_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["task_text"] = update.message.text
    tg_id = str(update.effective_user.id)
    
    users = load_users()
    subs = [u for u in users if u["chief_id"] == tg_id and u["role"] == "manager"]

    if not subs:
        await update.message.reply_text("❌ У вас нет подчинённых.")
        return ConversationHandler.END

    buttons = [[InlineKeyboardButton(f"👤 {u['tg_id']}", callback_data=f"assign:{u['tg_id']}")] for u in subs]
    await update.message.reply_text("Выберите сотрудника:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_USER

async def assign_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["assignee_id"] = query.data.split(":")[1]
    await query.edit_message_text("Введите дату дедлайна (DD.MM.YYYY):")
    return DEADLINE_DATE

async def deadline_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["deadline_date"] = update.message.text
    await update.message.reply_text("Введите время дедлайна (HH:MM):")
    return DEADLINE_TIME

async def deadline_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = context.user_data["deadline_date"]
    time_str = update.message.text
    try:
        deadline = datetime.datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
        if deadline <= datetime.datetime.now():
            await update.message.reply_text("❌ Дедлайн должен быть в будущем. Попробуйте снова:")
            return DEADLINE_DATE
    except ValueError:
        await update.message.reply_text("⚠️ Неверный формат даты или времени. Попробуйте снова.")
        return DEADLINE_DATE

    tasks = load_tasks()
    task_id = str(len(tasks) + 1)
    chief_id = str(update.effective_user.id)
    assignee_id = context.user_data["assignee_id"]
    text = context.user_data["task_text"]
    deadline_str = deadline.strftime("%Y-%m-%d %H:%M")

    new_task = {"id": task_id, "chief_id": chief_id, "assignee_id": assignee_id,
                "text": text, "deadline": deadline_str, "status": "new"}
    tasks.append(new_task)
    save_tasks(tasks)

    # Планируем напоминания
    schedule_deadline_reminders(task_id, chief_id, assignee_id, text, deadline_str)

    # Отправляем уведомление менеджеру
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]])
    try:
        await context.bot.send_message(int(assignee_id),
            f"📝 НОВАЯ ЗАДАЧА\n\n{text}\n⏰ Дедлайн: {deadline_str}", reply_markup=keyboard)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось отправить задачу сотруднику: {e}")

    await update.message.reply_text("✅ Задача создана и отправлена менеджеру.")
    return ConversationHandler.END

# --- Добавляем оставшиеся функции: mark_done, show_tasks, show_completed_tasks, change_role и др. ---
# Эти функции остаются как у тебя в текущей версии, можно подключить их без изменений.

# --- ConversationHandler для задач ---
task_conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^📝 Создать задачу$"), task)],
    states={
        TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_text_handler)],
        CHOOSE_USER: [CallbackQueryHandler(assign_task, pattern="^assign:")],
        DEADLINE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_date_handler)],
        DEADLINE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_time_handler)],
    },
    fallbacks=[CommandHandler("cancel", lambda u,c: u.message.reply_text("Операция отменена."))]
)

app = Application.builder().token("8377447196:AAHPqerv_P6zgKvL9GIv_4mmz4ygSK5GOGE").build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(task_conv_handler)

print("Бот запущен...")
app.run_polling()