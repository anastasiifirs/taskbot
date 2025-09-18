import csv
import os
import datetime
import re
import logging
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

# ---------- Состояния ConversationHandler ----------
REGISTER_NAME, REGISTER_SURNAME, TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME, \
CHOOSE_USER_FOR_ROLE, CONFIRM_ROLE_CHANGE = range(8)

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
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ НАПОМИНАНИЕ: Задача '{task_text}' до {deadline_display}",
            reply_markup=keyboard
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ ПРОСРОЧЕНО: Задача '{task_text}' не выполнена к {deadline_display}"
        )

def schedule_deadline_reminders(application: Application, task_id: str, chief_id: int, assignee_id: int,
                               task_text: str, deadline_dt: datetime.datetime):
    now = datetime.datetime.now()
    jobq = application.job_queue
    
    reminders = [
        (deadline_dt - datetime.timedelta(days=1), "assignee", assignee_id),
        (deadline_dt - datetime.timedelta(hours=1), "assignee", assignee_id),
        (deadline_dt + datetime.timedelta(hours=1), "chief", chief_id)
    ]
    
    for reminder_time, role, chat_id in reminders:
        if reminder_time > now:
            jobq.run_once(
                send_deadline_reminder,
                when=reminder_time,
                data={
                    "task_id": task_id,
                    "chat_id": chat_id,
                    "task_text": task_text,
                    "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"),
                    "role": role
                }
            )

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if user:
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(
            f"🔑 С возвращением, {user['name']} {user['surname']}! Ты {user['role']}.",
            reply_markup=keyboard
        )
        return ConversationHandler.END
    
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
    tg_id = context.user_data["tg_id"]
    name = context.user_data["name"]
    
    users = load_users()
    
    if not users:
        new_user = {"tg_id": tg_id, "name": name, "surname": surname, "role": "chief", "chief_id": ""}
        users.append(new_user)
        save_users(users)
        keyboard = get_main_keyboard("chief")
        await update.message.reply_text(
            f"👨‍💼 Привет, {name} {surname}! Ты зарегистрирован как НАЧАЛЬНИК.",
            reply_markup=keyboard
        )
    else:
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
        help_text = (
            "👨‍💼 Команды для начальника:\n"
            "📝 Создать задачу - назначить новую задачу\n"
            "📋 Мои задачи - просмотреть все задачи\n"
            "👥 Сотрудники - список ваших подчиненных\n"
            "🔄 Изменить роли - изменить роль сотрудника\n"
            "📊 Статистика - отчет по выполнению задач"
        )
    else:
        help_text = (
            "👨‍💼 Команды для сотрудника:\n"
            "📋 Мои задачи - просмотреть текущие задачи\n"
            "✅ Выполненные - просмотреть выполненные задачи\n"
            "❓ Помощь - показать эту справку"
        )
    
    await update.message.reply_text(help_text)

# ---------- Task handlers ----------
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
    context.user_data["task_text"] = update.message.text.strip()
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
    assignee_id = query.data.split(":")[1]
    context.user_data["assignee_id"] = assignee_id
    await query.edit_message_text("Введите дату дедлайна в формате ДД.ММ.ГГГГ (например: 20.09.2025):")
    return DEADLINE_DATE

async def deadline_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
        await update.message.reply_text("❌ Неверный формат дата. Используйте ДД.ММ.ГГГГ (например: 20.09.2025):")
        return DEADLINE_DATE
    
    try:
        day, month, year = map(int, date_str.split('.'))
        # Проверяем корректность даты
        datetime.datetime(year, month, day)
        context.user_data["deadline_date"] = date_str
        await update.message.reply_text("Введите время дедлайна в формате ЧЧ:MM (например: 14:30 или 9:00):")
        return DEADLINE_TIME
    except ValueError:
        await update.message.reply_text("❌ Неверная дата. Проверьте правильность ввода (например: 20.09.2025):")
        return DEADLINE_DATE

async def deadline_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text.strip()
    logger.info(f"Получено время: '{time_str}'")
    
    try:
        # Простая проверка - пытаемся разобрать время
        if ':' not in time_str:
            await update.message.reply_text("❌ Используйте формат ЧЧ:MM (например: 14:30)")
            return DEADLINE_TIME
        
        parts = time_str.split(':')
        if len(parts) != 2:
            await update.message.reply_text("❌ Используйте формат ЧЧ:MM (например: 14:30)")
            return DEADLINE_TIME
        
        hours = int(parts[0])
        minutes = int(parts[1])
        
        logger.info(f"Разобрано: hours={hours}, minutes={minutes}")
        
        # Проверяем корректность времени
        if hours < 0 or hours > 23:
            await update.message.reply_text("❌ Часы должны быть от 0 до 23:")
            return DEADLINE_TIME
        
        if minutes < 0 or minutes > 59:
            await update.message.reply_text("❌ Минуты должны быть от 0 до 59:")
            return DEADLINE_TIME
        
        date_str = context.user_data.get("deadline_date")
        if not date_str:
            logger.error("Дата не найдена в context.user_data")
            await update.message.reply_text("❌ Ошибка: дата не найдена. Начните заново.")
            return ConversationHandler.END
        
        logger.info(f"Дата из контекста: '{date_str}'")
        
        day, month, year = map(int, date_str.split('.'))
        
        # Создаем datetime объект
        deadline_dt = datetime.datetime(year, month, day, hours, minutes)
        now = datetime.datetime.now()
        
        logger.info(f"Создан deadline_dt: {deadline_dt}, now: {now}")
        
        if deadline_dt <= now:
            await update.message.reply_text("❌ Дедлайн должен быть в будущем. Введите дату заново:")
            return DEADLINE_DATE
        
        # Сохраняем задачу
        tasks = load_tasks()
        task_id = str(len(tasks) + 1)
        chief_id = str(update.effective_user.id)
        assignee_id = context.user_data.get("assignee_id")
        text = context.user_data.get("task_text", "")
        
        logger.info(f"Данные задачи: chief_id={chief_id}, assignee_id={assignee_id}, text={text}")
        
        if not assignee_id or not text:
            logger.error("Данные задачи потеряны")
            await update.message.reply_text("❌ Ошибка: данные задачи потеряны. Начните заново.")
            return ConversationHandler.END
        
        deadline_str = deadline_dt.strftime("%Y-%m-%d %H:%M")

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
        logger.info(f"Задача сохранена: {new_task}")

        # Планируем напоминания
        try:
            schedule_deadline_reminders(
                context.application, 
                task_id, 
                int(chief_id), 
                int(assignee_id), 
                text, 
                deadline_dt
            )
            logger.info("Напоминания запланированы")
        except Exception as e:
            logger.error(f"Ошибка при планировании напоминаний: {e}")

        # Отправляем задачу менеджеру
        users = load_users()
        assignee = next((u for u in users if u["tg_id"] == assignee_id), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else assignee_id
        
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]])
        
        try:
            await context.bot.send_message(
                int(assignee_id),
                f"📝 НОВАЯ ЗАДАЧА\n\n{text}\n⏰ Дедлайн: {deadline_dt.strftime('%d.%m.%Y %H:%M')}",
                reply_markup=keyboard
            )
            logger.info(f"Задача отправлена менеджеру {assignee_id}")
        except Exception as e:
            logger.error(f"Не удалось отправить задачу сотруднику: {e}")
            await update.message.reply_text(f"⚠️ Не удалось отправить задачу сотруднику: {e}")

        # Очищаем данные
        for key in ["task_text", "assignee_id", "deadline_date"]:
            context.user_data.pop(key, None)
        
        await update.message.reply_text(f"✅ Задача создана и отправлена менеджеру {assignee_name}.")
        return ConversationHandler.END
        
    except ValueError as e:
        logger.error(f"ValueError в обработке времени: {e}", exc_info=True)
        await update.message.reply_text("❌ Неверный формат времени. Используйте числа (например: 14:30):")
        return DEADLINE_TIME
    except Exception as e:
        logger.error(f"ОБЩАЯ ОШИБКА в обработке времени: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте снова:")
        return DEADLINE_TIME

# --- Функции для изменения ролей ---
async def change_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user or user["role"] != "chief":
        await update.message.reply_text("❌ Только начальник может изменять роли.")
        return ConversationHandler.END
    
    subs = [u for u in users if u["chief_id"] == tg_id]
    
    if not subs:
        await update.message.reply_text("📭 У вас нет сотрудников для изменения ролей.")
        return ConversationHandler.END
    
    buttons = []
    for u in subs:
        role_emoji = "👑" if u["role"] == "chief" else "👤"
        buttons.append([InlineKeyboardButton(
            f"{role_emoji} {u['name']} {u['surname']} ({u['role']})", 
            callback_data=f"role_user:{u['tg_id']}"
        )])
    
    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        "Выберите сотрудника для изменения роли:",
        reply_markup=keyboard
    )
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
    context.user_data["current_role"] = current_role
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data="confirm_role:yes")],
        [InlineKeyboardButton("❌ Нет", callback_data="confirm_role:no")]
    ])
    
    await query.edit_message_text(
        f"Изменить роль сотрудника {user['name']} {user['surname']}?\n"
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
    current_role = context.user_data["current_role"]
    
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
        logger.error(f"Ошибка при отправке уведомления сотруднику: {e}")
    
    await query.edit_message_text(
        f"✅ Роль сотрудника {user['name']} {user['surname']} изменена:\n"
        f"С {old_role} на {new_role}"
    )
    
    # Очищаем данные
    for key in ["role_user_id", "new_role", "current_role"]:
        context.user_data.pop(key, None)
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# ---------- Mark task as done ----------
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
    
    # Уведомляем начальника
    try:
        users = load_users()
        assignee = next((u for u in users if u["tg_id"] == task["assignee_id"]), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else task["assignee_id"]
        
        await context.bot.send_message(
            int(task["chief_id"]),
            f"✅ Подчинённый {assignee_name} выполнил задачу:\n{task['text']}\n⏰ Дедлайн был: {task['deadline']}"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления начальнику: {e}")
    
    await query.edit_message_text(f"✅ Задача выполнена: {task['text']}")

# ---------- Other functions ----------
async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def show_completed_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            message += f"{role_emoji} {sub['name']} {sub['surname']} - {sub['role']}\n"
        await update.message.reply_text(message)

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

# ---------- Text message handler ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# ---------- MAIN ----------
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN not found in environment variables.")
        return
    
    app = Application.builder().token(token).build()

    # Регистрация
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Создание задачи
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

    # Изменение ролей (ДОБАВЬТЕ ЭТОТ БЛОК)
    role_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔄 Изменить роли$"), change_role)],
        states={
            CHOOSE_USER_FOR_ROLE: [CallbackQueryHandler(choose_user_for_role, pattern="^role_user:")],
            CONFIRM_ROLE_CHANGE: [CallbackQueryHandler(confirm_role_change, pattern="^confirm_role:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Добавляем обработчики (ДОБАВЬТЕ role_conv)
    app.add_handler(register_conv)
    app.add_handler(task_conv)
    app.add_handler(role_conv)  # ← ДОБАВЬТЕ ЭТУ СТРОЧКУ
    app.add_handler(CallbackQueryHandler(mark_done, pattern="^done:"))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
