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

# Состояния для ConversationHandler
TASK_TEXT, CHOOSE_USER, DEADLINE, CHOOSE_USER_FOR_ROLE, CONFIRM_ROLE_CHANGE = range(5)

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
            args=[task_id],
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
            args=[task_id],
            kwargs={
                "data": {
                    "task_id": task_id,
                    "chat_id": assignee_id,
                    "task_text": task_text,
                    "deadline": deadline_str
                }
            }
        )
    
    # Напоминание руководителю после дедлайна
    reminder_after = deadline + datetime.timedelta(hours=1)
    scheduler.add_job(
        send_deadline_reminder,
        DateTrigger(run_date=reminder_after),
        args=[task_id],
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

    buttons = []
    for u in subs:
        buttons.append([InlineKeyboardButton(f"👤 Сотрудник {u['tg_id']}", callback_data=f"assign:{u['tg_id']}")])
    
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Выберите сотрудника:", reply_markup=keyboard)
    return CHOOSE_USER

async def assign_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    assignee_id = query.data.split(":")[1]
    context.user_data["assignee_id"] = assignee_id
    
    await query.edit_message_text("Введите дедлайн в формате YYYY-MM-DD HH:MM:")
    return DEADLINE

async def deadline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deadline_str = update.message.text
    try:
        deadline = datetime.datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
        if deadline <= datetime.datetime.now():
            await update.message.reply_text("❌ Дедлайн должен быть в будущем. Попробуйте снова:")
            return DEADLINE
    except ValueError:
        await update.message.reply_text("⚠️ Неверный формат. Попробуйте снова (YYYY-MM-DD HH:MM):")
        return DEADLINE

    tasks = load_tasks()
    task_id = str(len(tasks) + 1)
    chief_id = str(update.effective_user.id)
    assignee_id = context.user_data["assignee_id"]
    text = context.user_data["task_text"]

    new_task = {
        "id": task_id,
        "chief_id": chief_id,
        "assignee_id": assignee_id,
        "text": text,
        "deadline": deadline_str,
        "status": "new"
    }
    tasks.append(new_task)
    save_tasks(tasks)

    # Планируем напоминания
    schedule_deadline_reminders(task_id, chief_id, assignee_id, text, deadline_str)

    # Отправляем задачу подчинённому
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]
    ])
    
    try:
        await context.bot.send_message(
            chat_id=int(assignee_id),
            text=f"📝 НОВАЯ ЗАДАЧА\n\n{text}\n\n⏰ Дедлайн: {deadline_str}",
            reply_markup=keyboard
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось отправить задачу сотруднику: {e}")

    await update.message.reply_text("✅ Задача сохранена и назначена.")
    return ConversationHandler.END

# --- Изменение ролей ---
async def change_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)

    if not user or user["role"] != "chief":
        await update.message.reply_text("❌ Только начальник может изменять роли.")
        return ConversationHandler.END

    subs = [u for u in users if u["chief_id"] == tg_id]
    
    if not subs:
        await update.message.reply_text("❌ У вас нет подчинённых.")
        return ConversationHandler.END

    buttons = []
    for u in subs:
        role_emoji = "👑" if u["role"] == "chief" else "👤"
        buttons.append([InlineKeyboardButton(f"{role_emoji} {u['tg_id']} ({u['role']})", callback_data=f"role_user:{u['tg_id']}")])
    
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Выберите сотрудника для изменения роли:", reply_markup=keyboard)
    return CHOOSE_USER_FOR_ROLE

async def choose_user_for_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split(":")[1]
    context.user_data["role_user_id"] = user_id
    
    users = load_users()
    user = next((u for u in users if u["tg_id"] == user_id), None)
    
    if not user:
        await query.edit_message_text("❌ Сотрудник не найден.")
        return ConversationHandler.END
    
    current_role = user["role"]
    new_role = "manager" if current_role == "chief" else "chief"
    
    context.user_data["new_role"] = new_role
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data="confirm_role:yes")],
        [InlineKeyboardButton("❌ Нет", callback_data="confirm_role:no")]
    ])
    
    await query.edit_message_text(
        f"Изменить роль сотрудника {user_id}?\n"
        f"Текущая роль: {current_role}\n"
        f"Новая роль: {new_role}\n\n"
        f"Подтвердите изменение:",
        reply_markup=keyboard
    )
    return CONFIRM_ROLE_CHANGE

async def confirm_role_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    confirmation = query.data.split(":")[1]
    
    if confirmation == "no":
        await query.edit_message_text("❌ Изменение роли отменено.")
        return ConversationHandler.END
    
    user_id = context.user_data["role_user_id"]
    new_role = context.user_data["new_role"]
    
    users = load_users()
    user = next((u for u in users if u["tg_id"] == user_id), None)
    
    if not user:
        await query.edit_message_text("❌ Сотрудник не найден.")
        return ConversationHandler.END
    
    # Сохраняем старую роль для сообщения
    old_role = user["role"]
    user["role"] = new_role
    save_users(users)
    
    # Отправляем уведомление сотруднику
    try:
        role_text = "начальником" if new_role == "chief" else "менеджером"
        await context.bot.send_message(
        chat_id=int(user_id),
        text=f"🎉 Ваша роль изменена! Теперь вы {role_text}.\n\n"
		f"Используйте /start для обновления меню."
	)
    except Exception as e:
        print(f"Ошибка при отправке уведомления сотруднику: {e}")
    
    await query.edit_message_text(
        f"✅ Роль сотрудника {user_id} изменена:\n"
        f"С {old_role} на {new_role}"
    )
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# --- Просмотр задач ---
async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь с помощью /start")
        return

    tasks = load_tasks()
    
    if user["role"] == "chief":
        user_tasks = [t for t in tasks if t["chief_id"] == tg_id]
        title = "📋 ЗАДАЧИ, КОТОРЫЕ ВЫ СОЗДАЛИ:\n\n"
    else:
        user_tasks = [t for t in tasks if t["assignee_id"] == tg_id]
        title = "📋 ВАШИ ТЕКУЩИЕ ЗАДАЧИ:\n\n"
    
    if not user_tasks:
        await update.message.reply_text("📭 Нет задач")
        return
    
    message = title
    for task in user_tasks:
        status_emoji = "✅" if task["status"] == "done" else "⏳"
        message += f"{status_emoji} Задача #{task['id']}: {task['text']}\n"
        message += f"   ⏰ Дедлайн: {task['deadline']}\n"
        message += f"   📊 Статус: {task['status']}\n\n"
    
    await update.message.reply_text(message)

async def show_completed_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user or user["role"] != "manager":
        await update.message.reply_text("❌ Эта функция доступна только менеджерам")
        return

    tasks = load_tasks()
    completed_tasks = [t for t in tasks if t["assignee_id"] == tg_id and t["status"] == "done"]
    
    if not completed_tasks:
        await update.message.reply_text("📭 Нет выполненных задач")
        return
    
    message = "✅ ВЫПОЛНЕННЫЕ ЗАДАЧИ:\n\n"
    for task in completed_tasks:
        message += f"🎯 Задача #{task['id']}: {task['text']}\n"
        message += f"   ⏰ Дедлайн был: {task['deadline']}\n\n"
    
    await update.message.reply_text(message)

# --- Выполнение задачи ---
async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task_id = query.data.split(":")[1]

    tasks = load_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return

    task["status"] = "done"
    save_tasks(tasks)

    try:
        chief_id = int(task["chief_id"])
        await context.bot.send_message(
            chief_id,
            f"✅ Подчинённый {task['assignee_id']} выполнил задачу:\n\n"
            f"{task['text']}\n\n"
            f"⏰ Дедлайн был: {task['deadline']}"
        )
    except Exception as e:
        print(f"Ошибка при отправке уведомления начальнику: {e}")

    await query.edit_message_text(f"✅ Задача выполнена: {task['text']}")

# --- Обработка текстовых сообщений (кнопки) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user:
        await update.message.reply_text("❌ Сначала зарегистрируйтесь с помощью /start")
        return

    if text == "📝 Создать задачу" and user["role"] == "chief":
        await task(update, context)
    elif text == "📋 Мои задачи":
        await show_tasks(update, context)
    elif text == "✅ Выполненные" and user["role"] == "manager":
        await show_completed_tasks(update, context)
    elif text == "👥 Сотрудники" and user["role"] == "chief":
        subs = [u for u in users if u["chief_id"] == tg_id]
        if subs:
            message = "👥 ВАШИ СОТРУДНИКИ:\n\n"
            for sub in subs:
                role_emoji = "👑" if sub["role"] == "chief" else "👤"
                message += f"{role_emoji} ID: {sub['tg_id']} - {sub['role']}\n"
            await update.message.reply_text(message)
        else:
            await update.message.reply_text("📭 Нет сотрудников")
    elif text == "🔄 Изменить роли" and user["role"] == "chief":
        await change_role(update, context)
    elif text == "❓ Помощь":
        await help_command(update, context)
    else:
        await update.message.reply_text("❌ Неизвестная команда")

# --- Создание и запуск приложения ---
app = Application.builder().token("8377447196:AAHPqerv_P6zgKvL9GIv_4mmz4ygSK5GOGE").build()

# ConversationHandler для создания задачи
task_conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^📝 Создать задачу$"), task)],
    states={
        TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_text_handler)],
        CHOOSE_USER: [CallbackQueryHandler(assign_task, pattern="^assign:")],
        DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_handler)],
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

# ConversationHandler для изменения ролей
role_conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^🔄 Изменить роли$"), change_role)],
    states={
        CHOOSE_USER_FOR_ROLE: [CallbackQueryHandler(choose_user_for_role, pattern="^role_user:")],
        CONFIRM_ROLE_CHANGE: [CallbackQueryHandler(confirm_role_change, pattern="^confirm_role:")],
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(task_conv_handler)
app.add_handler(role_conv_handler)
app.add_handler(CommandHandler("tasks", show_tasks))
app.add_handler(CallbackQueryHandler(mark_done, pattern="^done:"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("Бот запущен...")
app.run_polling()