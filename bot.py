import csv
import os
import datetime
import re
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

# Состояния ConversationHandler
REGISTER_NAME, REGISTER_SURNAME, TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME = range(6)

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
    return load_csv(USERS_FILE, ["tg_id", "name", "surname", "role", "chief_id"])

def save_users(users):
    save_csv(USERS_FILE, users, ["tg_id", "name", "surname", "role", "chief_id"])

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
    
    # Напоминание за 1 день
    reminder_1_day = deadline - datetime.timedelta(days=1)
    if reminder_1_day > datetime.datetime.now():
        scheduler.add_job(
            send_deadline_reminder,
            DateTrigger(run_date=reminder_1_day),
            kwargs={"data":{"task_id": task_id,"chat_id": assignee_id,"task_text": task_text,"deadline": deadline_str}}
        )
    
    # Напоминание за 1 час
    reminder_1_hour = deadline - datetime.timedelta(hours=1)
    if reminder_1_hour > datetime.datetime.now():
        scheduler.add_job(
            send_deadline_reminder,
            DateTrigger(run_date=reminder_1_hour),
            kwargs={"data":{"task_id": task_id,"chat_id": assignee_id,"task_text": task_text,"deadline": deadline_str}}
        )
    
    # После дедлайна для начальника
    reminder_after = deadline + datetime.timedelta(hours=1)
    scheduler.add_job(
        send_deadline_reminder,
        DateTrigger(run_date=reminder_after),
        kwargs={"data":{"task_id": task_id,"chat_id": chief_id,"task_text": task_text,"deadline": f"{deadline_str} (ПРОСРОЧЕНО)"}}
    )

# --- Регистрация пользователя ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)

    if user:
        # Пользователь уже зарегистрирован
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(
            f"🔑 С возвращением, {user['name']} {user['surname']}! Ты {user['role']}.",
            reply_markup=keyboard
        )
        return

    # Новый пользователь
    context.user_data["tg_id"] = tg_id
    await update.message.reply_text("👋 Добро пожаловать! Для регистрации введите ваше имя:")
    return REGISTER_NAME

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Теперь введите вашу фамилию:")
    return REGISTER_SURNAME

async def register_surname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["surname"] = update.message.text
    tg_id = context.user_data["tg_id"]
    name = context.user_data["name"]
    surname = context.user_data["surname"]
    
    users = load_users()
    
    if not users:
        # Первый пользователь - начальник
        new_user = {"tg_id": tg_id, "name": name, "surname": surname, "role": "chief", "chief_id": ""}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("chief")
        await update.message.reply_text(
            f"👨‍💼 Привет, {name} {surname}! Ты зарегистрирован как НАЧАЛЬНИК.",
            reply_markup=keyboard
        )
    else:
        # Обычный пользователь - менеджер
        chiefs = [u for u in users if u["role"] == "chief"]
        chief_id = chiefs[0]["tg_id"] if chiefs else ""
        new_user = {"tg_id": tg_id, "name": name, "surname": surname, "role": "manager", "chief_id": chief_id}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("manager")
        await update.message.reply_text(
            f"👋 Привет, {name} {surname}! Ты зарегистрирован как МЕНЕДЖЕР.",
            reply_markup=keyboard
        )
    
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if user and user["role"] == "chief":
        help_text = ("👨‍💼 Команды для начальника:\n📝 Создать задачу\n📋 Мои задачи\n👥 Сотрудники\n🔄 Изменить роли\n📊 Статистика")
    else:
        help_text = ("👨‍💼 Команды для сотрудника:\n📋 Мои задачи\n✅ Выполненные\n❓ Помощь")
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
        buttons.append([InlineKeyboardButton(f"👤 {u['name']} {u['surname']}", callback_data=f"assign:{u['tg_id']}")])
    
    await update.message.reply_text("Выберите сотрудника:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_USER

async def assign_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["assignee_id"] = query.data.split(":")[1]
    await query.edit_message_text("Введите дату дедлайна в формате ДД.ММ.ГГГГ (например: 20.09.2025):")
    return DEADLINE_DATE

async def deadline_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    
    # Проверка формата даты
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
        await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ (например: 20.09.2025):")
        return DEADLINE_DATE
    
    try:
        day, month, year = map(int, date_str.split('.'))
        # Проверяем, что дата корректна
        datetime.datetime(year, month, day)
        context.user_data["deadline_date"] = date_str
        await update.message.reply_text("Введите время дедлайна в формате ЧЧ:MM (например: 14:30):")
        return DEADLINE_TIME
    except ValueError:
        await update.message.reply_text("❌ Неверная дата. Проверьте правильность ввода (например: 20.09.2025):")
        return DEADLINE_DATE

async def deadline_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text.strip()
    
    # Простая проверка формата времени ЧЧ:MM
    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        await update.message.reply_text("❌ Неверный формат времени. Используйте ЧЧ:MM (например: 14:30):")
        return DEADLINE_TIME
    
    try:
        # Разбираем время
        hours, minutes = map(int, time_str.split(':'))
        
        # Проверяем корректность часов и минут
        if hours < 0 or hours > 23 or minutes < 0 or minutes > 59:
            await update.message.reply_text("❌ Неверное время. Часы должны быть от 0 до 23, минуты от 0 до 59:")
            return DEADLINE_TIME
        
        date_str = context.user_data["deadline_date"]
        day, month, year = map(int, date_str.split('.'))
        
        deadline = datetime.datetime(year, month, day, hours, minutes)
        if deadline <= datetime.datetime.now():
            await update.message.reply_text("❌ Дедлайн должен быть в будущем. Введите дату заново:")
            return DEADLINE_DATE
        
        # Сохраняем задачу
        tasks = load_tasks()
        task_id = str(len(tasks) + 1)
        chief_id = str(update.effective_user.id)
        assignee_id = context.user_data["assignee_id"]
        text = context.user_data["task_text"]
        deadline_str = deadline.strftime("%Y-%m-%d %H:%M")

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

        # Напоминания
        schedule_deadline_reminders(task_id, chief_id, assignee_id, text, deadline_str)

        # Уведомление менеджеру
        users = load_users()
        assignee = next((u for u in users if u["tg_id"] == assignee_id), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else assignee_id
        
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]])
        try:
            await context.bot.send_message(
                int(assignee_id), 
                f"📝 НОВАЯ ЗАДАЧА\n\n{text}\n⏰ Дедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}",
                reply_markup=keyboard
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Не удалось отправить задачу сотруднику: {e}")

        await update.message.reply_text(f"✅ Задача создана и отправлена менеджеру {assignee_name}.")
        return ConversationHandler.END
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обработке времени. Попробуйте снова:")
        return DEADLINE_TIME

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# --- Отмечаем задачу выполненной ---
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
        users = load_users()
        assignee = next((u for u in users if u["tg_id"] == task["assignee_id"]), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else task["assignee_id"]
        
        await context.bot.send_message(
            int(task["chief_id"]),
            f"✅ Подчинённый {assignee_name} выполнил задачу:\n{task['text']}\n⏰ Дедлайн был: {task['deadline']}"
        )
    except:
        pass
    
    await query.edit_message_text(f"✅ Задача выполнена: {task['text']}")

# --- Просмотр задач ---
async def show_tasks(update, context):
    tg_id = str(update.effective_user.id)
    tasks = load_tasks()
    user_tasks = [t for t in tasks if t["assignee_id"] == tg_id or t["chief_id"] == tg_id]
    
    if not user_tasks:
        await update.message.reply_text("📭 Нет задач")
        return
    
    users = load_users()
    msg = ""
    for t in user_tasks:
        status = "✅" if t["status"] == "done" else "⏳"
        assignee = next((u for u in users if u["tg_id"] == t["assignee_id"]), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else t["assignee_id"]
        
        deadline_dt = datetime.datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
        deadline_str = deadline_dt.strftime("%d.%m.%Y %H:%M")
        
        msg += f"{status} Задача #{t['id']}\n"
        msg += f"📝 {t['text']}\n"
        msg += f"👤 Исполнитель: {assignee_name}\n"
        msg += f"⏰ Дедлайн: {deadline_str}\n"
        msg += f"📊 Статус: {t['status']}\n\n"
    
    await update.message.reply_text(msg)

async def show_completed_tasks(update, context):
    tg_id = str(update.effective_user.id)
    tasks = load_tasks()
    completed = [t for t in tasks if t["assignee_id"] == tg_id and t["status"] == "done"]
    
    if not completed:
        await update.message.reply_text("📭 Нет выполненных задач")
        return
    
    msg = "✅ ВЫПОЛНЕННЫЕ ЗАДАЧИ:\n\n"
    for t in completed:
        deadline_dt = datetime.datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
        deadline_str = deadline_dt.strftime("%d.%m.%Y %H:%M")
        msg += f"🎯 Задача #{t['id']}: {t['text']}\n⏰ Дедлайн был: {deadline_str}\n\n"
    
    await update.message.reply_text(msg)

# --- Новые функции для обработки кнопок ---
async def show_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    subs = [u for u in users if u["chief_id"] == tg_id]
    
    if not subs:
        await update.message.reply_text("📭 У вас пока нет сотрудников")
    else:
        message = "👥 Ваши сотрудники:\n\n"
        for sub in subs:
            role_emoji = "👑" if sub["role"] == "chief" else "👤"
            message += f"{role_emoji} {sub['name']} {sub['surname']} (ID: {sub['tg_id']}) - {sub['role']}\n"
        await update.message.reply_text(message)

async def change_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    subs = [u for u in users if u["chief_id"] == tg_id]
    
    if not subs:
        await update.message.reply_text("📭 У вас нет сотрудников для изменения ролей")
        return
    
    await update.message.reply_text("🔄 Функция изменения ролей будет реализована в ближайшее время")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    tasks = load_tasks()
    user_tasks = [t for t in tasks if t["chief_id"] == tg_id]
    
    if not user_tasks:
        await update.message.reply_text("📊 Статистика:\nНет созданных задач")
        return
    
    total_tasks = len(user_tasks)
    completed_tasks = len([t for t in user_tasks if t["status"] == "done"])
    pending_tasks = total_tasks - completed_tasks
    
    message = f"📊 Статистика:\n\n"
    message += f"📋 Всего задач: {total_tasks}\n"
    message += f"✅ Выполнено: {completed_tasks}\n"
    message += f"⏳ В процессе: {pending_tasks}\n"
    message += f"📈 Процент выполнения: {round((completed_tasks/total_tasks)*100 if total_tasks > 0 else 0)}%"
    
    await update.message.reply_text(message)

# --- Глобальный обработчик текста ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем, активен ли ConversationHandler
    if context.user_data.get('_conversation_state'):
        return
    
    text = update.message.text
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return
        
    if text == "📝 Создать задачу" and user["role"] == "chief":
        await task(update, context)
    elif text == "📋 Мои задачи":
        await show_tasks(update, context)
    elif text == "✅ Выполненные" and user["role"] == "manager":
        await show_completed_tasks(update, context)
    elif text == "👥 Сотрудники" and user["role"] == "chief":
        await show_employees(update, context)
    elif text == "🔄 Изменить роли" and user["role"] == "chief":
        await change_role(update, context)
    elif text == "📊 Статистика" and user["role"] == "chief":
        await show_statistics(update, context)
    elif text == "❓ Помощь":
        await help_command(update, context)
    else:
        await update.message.reply_text("❌ Неизвестная команда")

# --- Настройка приложения ---
app = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()

# ConversationHandler для регистрации
register_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
        REGISTER_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname)],
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

# ConversationHandler для создания задачи
task_conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.Regex("^📝 Создать задачу$"), task)],
    states={
        TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_text_handler)],
        CHOOSE_USER: [CallbackQueryHandler(assign_task, pattern="^assign:")],
        DEADLINE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_date_handler)],
        DEADLINE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_time_handler)]
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)

app.add_handler(register_conv_handler)
app.add_handler(CommandHandler("help", help_command))
app.add_handler(task_conv_handler)
app.add_handler(CallbackQueryHandler(mark_done, pattern="^done:"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("Бот запущен...")
app.run_polling()
