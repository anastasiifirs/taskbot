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

# --- ConversationHandler состояния ---
TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME, CHOOSE_USER_FOR_ROLE, CONFIRM_ROLE_CHANGE = range(6)

# --- CSV функции ---
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

# --- Напоминания ---
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
    
    # За день
    reminder_1_day = deadline - datetime.timedelta(days=1)
    if reminder_1_day > datetime.datetime.now():
        scheduler.add_job(
            send_deadline_reminder,
            DateTrigger(run_date=reminder_1_day),
            kwargs={"data": {"task_id": task_id,"chat_id": assignee_id,"task_text": task_text,"deadline": deadline_str}}
        )
    # За час
    reminder_1_hour = deadline - datetime.timedelta(hours=1)
    if reminder_1_hour > datetime.datetime.now():
        scheduler.add_job(
            send_deadline_reminder,
            DateTrigger(run_date=reminder_1_hour),
            kwargs={"data": {"task_id": task_id,"chat_id": assignee_id,"task_text": task_text,"deadline": deadline_str}}
        )
    # Просрочено начальнику
    reminder_after = deadline + datetime.timedelta(hours=1)
    scheduler.add_job(
        send_deadline_reminder,
        DateTrigger(run_date=reminder_after),
        kwargs={"data": {"task_id": task_id,"chat_id": chief_id,"task_text": task_text,"deadline": f"{deadline_str} (ПРОСРОЧЕНО)"}}
    )

# --- Старт и помощь ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)

    if not users:
        new_user = {"tg_id": tg_id, "role": "chief", "chief_id": ""}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("chief")
        await update.message.reply_text("👨‍💼 Вы зарегистрированы как НАЧАЛЬНИК.", reply_markup=keyboard)
        return
    if not user:
        chiefs = [u for u in users if u["role"] == "chief"]
        chief_id = chiefs[0]["tg_id"] if chiefs else ""
        new_user = {"tg_id": tg_id, "role": "manager", "chief_id": chief_id}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("manager")
        await update.message.reply_text("👋 Вы зарегистрированы как МЕНЕДЖЕР.", reply_markup=keyboard)
    else:
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(f"🔑 С возвращением! Вы {user['role']}.", reply_markup=keyboard)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if user and user["role"] == "chief":
        help_text = "👨‍💼 Команды начальника:\n📝 Создать задачу\n📋 Мои задачи\n👥 Сотрудники\n🔄 Изменить роли\n📊 Статистика"
    else:
        help_text = "👨‍💼 Команды сотрудника:\n📋 Мои задачи\n✅ Выполненные\n❓ Помощь"
    await update.message.reply_text(help_text)

# --- ConversationHandler для задачи ---
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
    await update.message.reply_text("Выберите менеджера:", reply_markup=InlineKeyboardMarkup(buttons))
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

    # Отправляем менеджеру
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]])
    try:
        await context.bot.send_message(int(assignee_id),
            f"📝 НОВАЯ ЗАДАЧА\n\n{text}\n⏰ Дедлайн: {deadline_str}", reply_markup=keyboard)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось отправить задачу: {e}")

    await update.message.reply_text("✅ Задача создана и отправлена менеджеру.")
    return ConversationHandler.END

# --- Обработка ReplyKeyboardMarkup (кнопки) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return

    if text == "📝 Создать задачу" and user["role"] == "chief":
        return await task(update, context)
    elif text == "📋 Мои задачи":
        await show_tasks(update, context)
    elif text == "✅ Выполненные" and user["role"] == "manager":
        await show_completed_tasks(update, context)
    elif text == "❓ Помощь":
        await help_command(update, context)
    else:
        await update.message.reply_text("❌ Неизвестная команда")

# --- Запуск бота ---
app = Application.builder().token("8377447196:AAHPqerv_P6zgKvL9GIv_4mmz4ygSK5GOGE").build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# --- ConversationHandler для задачи ---
task_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^📝 Создать задачу$"), task)],
    states={
        TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_text_handler)],
        CHOOSE_USER: [CallbackQueryHandler(assign_task, pattern="^assign:")],
        DEADLINE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_date_handler)],
        DEADLINE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_time_handler)]
    },
    fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Операция отменена."))],
    per_message=True  # важно для корректной работы ConversationHandler
)

# --- Добавляем обработчики в правильном порядке ---
app = Application.builder().token("8377447196:AAHPqerv_P6zgKvL9GIv_4mmz4ygSK5GOGE").build()

# ConversationHandler первым, чтобы перехватывал сообщения до глобального хэндлера
app.add_handler(task_conv)
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))

# Глобальный хэндлер для кнопок ReplyKeyboardMarkup
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("Бот запущен...")
app.run_polling()
