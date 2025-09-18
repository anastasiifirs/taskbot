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

# Используем таймзону пользователя: Europe/Paris (как ты просила)
TZ = ZoneInfo("Europe/Paris")

# Состояния ConversationHandler
REGISTER_NAME, REGISTER_SURNAME, TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME = range(6)

# ---------- Вспомогательные функции для CSV ----------
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
    # deadline хранится в ISO формате (например: 2025-09-18T22:20:00+02:00)
    return load_csv(TASKS_FILE, ["id", "chief_id", "assignee_id", "text", "deadline", "status"])

def save_tasks(tasks):
    save_csv(TASKS_FILE, tasks, ["id", "chief_id", "assignee_id", "text", "deadline", "status"])

# ---------- UI / клавиатуры ----------
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

# ---------- Напоминания (job_queue PTB) ----------
async def send_deadline_reminder(context: ContextTypes.DEFAULT_TYPE):
    """
    context.job.data {'task_id','chat_id','task_text','deadline','role'}
    role: 'assignee' или 'chief' (для after-deadline)
    """
    try:
        data = context.job.data
        task_id = str(data.get("task_id"))
        # Проверим, что задача ещё не выполнена — защитимся от гонок
        tasks = load_tasks()
        task = next((t for t in tasks if t["id"] == task_id), None)
        if not task:
            logger.info("send_deadline_reminder: задача %s не найдена.", task_id)
            return
        if task.get("status") == "done":
            logger.info("send_deadline_reminder: задача %s уже выполнена — пропускаем напоминание.", task_id)
            return

        chat_id = int(data["chat_id"])
        task_text = data["task_text"]
        deadline_display = data["deadline"]
        # Для assignee добавим кнопку "Выполнено"
        if data.get("role") == "assignee":
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]])
            await context.bot.send_message(chat_id=chat_id, text=f"⏰ НАПОМИНАНИЕ: Задача '{task_text}' должна быть выполнена до {deadline_display}", reply_markup=keyboard)
        else:
            # начальнику: уведомление о просрочке
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ ПРОСРОЧЕНО: Задача '{task_text}' не выполнена к {deadline_display}")
    except Exception as e:
        logger.exception("Ошибка в send_deadline_reminder: %s", e)

def schedule_deadline_reminders_via_jobqueue(application: Application, task_id: str, chief_id: int, assignee_id: int, task_text: str, deadline_dt: datetime.datetime):
    """
    Планируем напоминания через application.job_queue.
    deadline_dt — timezone-aware datetime (в TZ).
    """
    jobq = application.job_queue
    now = datetime.datetime.now(TZ)

    # напоминание за 1 день
    r1 = deadline_dt - datetime.timedelta(days=1)
    if r1 > now:
        jobq.run_once(send_deadline_reminder, when=r1, data={
            "task_id": task_id,
            "chat_id": assignee_id,
            "task_text": task_text,
            "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"),
            "role": "assignee"
        })

    # напоминание за 1 час
    r2 = deadline_dt - datetime.timedelta(hours=1)
    if r2 > now:
        jobq.run_once(send_deadline_reminder, when=r2, data={
            "task_id": task_id,
            "chat_id": assignee_id,
            "task_text": task_text,
            "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"),
            "role": "assignee"
        })

    # напоминание начальнику через 1 час после дедлайна
    r3 = deadline_dt + datetime.timedelta(hours=1)
    if r3 > now:
        jobq.run_once(send_deadline_reminder, when=r3, data={
            "task_id": task_id,
            "chat_id": chief_id,
            "task_text": task_text,
            "deadline": deadline_dt.strftime("%d.%m.%Y %H:%M"),
            "role": "chief"
        })

def reload_reminders(application: Application):
    """
    При старте приложения — подгружаем задачи и восстанавливаем напоминания.
    """
    tasks = load_tasks()
    now = datetime.datetime.now(TZ)
    for t in tasks:
        try:
            if t.get("status") == "done":
                continue
            # deadline хранится ISO
            dl = t.get("deadline")
            if not dl:
                continue
            # fromisoformat поддерживает смещение, но может вернуть naive. Попробуем:
            try:
                deadline_dt = datetime.datetime.fromisoformat(dl)
            except Exception:
                # на всякий случай: parse as "%Y-%m-%d %H:%M"
                deadline_dt = datetime.datetime.strptime(dl, "%Y-%m-%d %H:%M")
                deadline_dt = deadline_dt.replace(tzinfo=TZ)

            # Если дедлайн в прошлом и прошло больше часа — можно сразу уведомить начальника (опционально)
            # Для простоты: планируем только будущие напоминания
            if deadline_dt > now:
                schedule_deadline_reminders_via_jobqueue(application, t["id"], int(t["chief_id"]), int(t["assignee_id"]), t["text"], deadline_dt)
            else:
                # если дедлайн прошел, но статус не done — можно отправить срочное уведомление начальнику сейчас
                pass
        except Exception as e:
            logger.exception("Ошибка при восстановлении напоминания для задачи %s: %s", t.get("id"), e)

# ---------- Регистрация пользователя ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if user:
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(f"🔑 С возвращением, {user['name']} {user['surname']}! Ты {user['role']}.", reply_markup=keyboard)
        return ConversationHandler.END

    # начинаем регистрацию
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

    # очистка флагов
    context.user_data.pop("tg_id", None)
    context.user_data.pop("name", None)
    context.user_data.pop("surname", None)
    context.user_data["conversation_active"] = False
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if user and user["role"] == "chief":
        help_text = ("👨‍💼 Команды для начальника:\n"
                     "📝 Создать задачу\n📋 Мои задачи\n👥 Сотрудники\n🔄 Изменить роли\n📊 Статистика\n"
                     "/add_user <tg_id> — добавить пользователя\n"
                     "/set_role <tg_id> <chief|manager> — изменить роль\n"
                     "/set_chief <tg_id> — назначить начальником")
    else:
        help_text = ("👨‍💼 Команды для сотрудника:\n"
                     "📋 Мои задачи\n✅ Выполненные\n❓ Помощь")
    await update.message.reply_text(help_text)

# ---------- Создание задачи ----------
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
        await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.MM.ГГГГ (например: 20.09.2025):")
        return DEADLINE_DATE
    try:
        day, month, year = map(int, date_str.split('.'))
        # проверка валидности
        dt = datetime.datetime(year, month, day)
        context.user_data["deadline_date"] = date_str
        await update.message.reply_text("Введите время дедлайна в формате ЧЧ:MM (например: 14:30):")
        return DEADLINE_TIME
    except ValueError:
        await update.message.reply_text("❌ Неверная дата. Попробуйте снова:")
        return DEADLINE_DATE

async def deadline_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text.strip()
    if not re.match(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$', time_str):
        await update.message.reply_text("❌ Неверный формат времени. Используйте ЧЧ:MM (например: 14:30):")
        return DEADLINE_TIME
    try:
        hours, minutes = map(int, time_str.split(':'))
        date_str = context.user_data.get("deadline_date")
        day, month, year = map(int, date_str.split('.'))
        # timezone-aware datetime in TZ
        deadline_dt = datetime.datetime(year, month, day, hours, minutes, tzinfo=TZ)
        now = datetime.datetime.now(TZ)
        if deadline_dt <= now:
            await update.message.reply_text("❌ Дедлайн должен быть в будущем. Введите дату заново:")
            return DEADLINE_DATE

        # сохраняем задачу (deadline в ISO)
        tasks = load_tasks()
        task_id = str(len(tasks) + 1)
        chief_id = str(update.effective_user.id)
        assignee_id = context.user_data.get("assignee_id")
        text = context.user_data.get("task_text", "").strip()
        deadline_iso = deadline_dt.isoformat()

        new_task = {
            "id": task_id,
            "chief_id": chief_id,
            "assignee_id": assignee_id,
            "text": text,
            "deadline": deadline_iso,
            "status": "new"
        }
        tasks.append(new_task)
        save_tasks(tasks)

        # планируем напоминания через job_queue (application доступен в context)
        try:
            schedule_deadline_reminders_via_jobqueue(context.application, task_id, int(chief_id), int(assignee_id), text, deadline_dt)
        except Exception as e:
            logger.exception("Ошибка при планировании напоминаний: %s", e)

        # отправляем задачу менеджеру
        users = load_users()
        assignee = next((u for u in users if u["tg_id"] == assignee_id), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else assignee_id

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}")]])
        try:
            await context.bot.send_message(int(assignee_id), f"📝 НОВАЯ ЗАДАЧА\n\n{text}\n⏰ Дедлайн: {deadline_dt.strftime('%d.%m.%Y %H:%M')}", reply_markup=keyboard)
        except Exception as e:
            logger.exception("Не удалось отправить задачу менеджеру: %s", e)
            await update.message.reply_text(f"⚠️ Не удалось отправить задачу сотруднику: {e}")

        await update.message.reply_text(f"✅ Задача создана и отправлена менеджеру {assignee_name}.")
        context.user_data.pop("task_text", None)
        context.user_data.pop("assignee_id", None)
        context.user_data.pop("deadline_date", None)
        context.user_data["conversation_active"] = False
        return ConversationHandler.END

    except Exception as e:
        logger.exception("Ошибка в deadline_time_handler: %s", e)
        await update.message.reply_text("❌ Ошибка при обработке времени. Попробуйте снова:")
        return DEADLINE_TIME

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["conversation_active"] = False
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# ---------- Отметка выполнения ----------
async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        task_id = query.data.split(":")[1]
        tasks = load_tasks()
        task = next((t for t in tasks if t["id"] == task_id), None)
        if not task:
            await query.edit_message_text("❌ Задача не найдена.")
            return
        if task.get("status") == "done":
            await query.edit_message_text("ℹ️ Эта задача уже помечена как выполненная.")
            return

        task["status"] = "done"
        save_tasks(tasks)

        # уведомляем начальника
        try:
            users = load_users()
            assignee = next((u for u in users if u["tg_id"] == task["assignee_id"]), None)
            assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else task["assignee_id"]
            await context.bot.send_message(int(task["chief_id"]), f"✅ Подчинённый {assignee_name} выполнил задачу:\n{task['text']}\n⏰ Дедлайн был: {task['deadline']}")
        except Exception:
            logger.exception("Ошибка при уведомлении начальника о выполнении.")
        await query.edit_message_text(f"✅ Задача выполнена: {task['text']}")
    except Exception as e:
        logger.exception("Ошибка в mark_done: %s", e)
        await query.edit_message_text("❌ Произошла ошибка при отметке задачи как выполненной.")

# ---------- Просмотр задач ----------
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
        try:
            deadline_dt = datetime.datetime.fromisoformat(t["deadline"])
        except Exception:
            deadline_dt = datetime.datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
            deadline_dt = deadline_dt.replace(tzinfo=TZ)
        deadline_str = deadline_dt.strftime("%d.%m.%Y %H:%M")
        msg += f"{status} Задача #{t['id']}\n📝 {t['text']}\n👤 Исполнитель: {assignee_name}\n⏰ Дедлайн: {deadline_str}\n📊 Статус: {t['status']}\n\n"
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
        try:
            deadline_dt = datetime.datetime.fromisoformat(t["deadline"])
        except Exception:
            deadline_dt = datetime.datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
            deadline_dt = deadline_dt.replace(tzinfo=TZ)
        deadline_str = deadline_dt.strftime("%d.%m.%Y %H:%M")
        msg += f"🎯 Задача #{t['id']}: {t['text']}\n⏰ Дедлайн был: {deadline_str}\n\n"
    await update.message.reply_text(msg)

# ---------- Сотрудники / роли ----------
async def show_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    subs = [u for u in users if u["chief_id"] == tg_id]
    if not subs:
        await update.message.reply_text("📭 У вас пока нет сотрудников")
        return
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
    buttons = [[InlineKeyboardButton(f"{s['name']} {s['surname']} ({s['role']})", callback_data=f"role:{s['tg_id']}")] for s in subs]
    await update.message.reply_text("Выберите сотрудника, чтобы переключить роль (manager <-> chief):", reply_markup=InlineKeyboardMarkup(buttons))

async def change_role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.data.split(":")[1]
    users = load_users()
    target = next((u for u in users if u["tg_id"] == user_id), None)
    if not target:
        await query.edit_message_text("❌ Сотрудник не найден")
        return
    old_role = target["role"]
    target["role"] = "chief" if old_role == "manager" else "manager"
    if target["role"] == "chief":
        target["chief_id"] = ""
    save_users(users)
    await query.edit_message_text(f"✅ Роль {target['name']} {target['surname']} изменена: {old_role} → {target['role']}")

# ---------- Глобальный обработчик текста ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # если идёт Conversation — не перехватываем
    if context.user_data.get("conversation_active"):
        return
    text = update.message.text
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return
    if text == "📝 Создать задачу" and user["role"] == "chief":
        return await task(update, context)
    if text == "📋 Мои задачи":
        await show_tasks(update, context)
        return
    if text == "✅ Выполненные" and user["role"] == "manager":
        await show_completed_tasks(update, context)
        return
    if text == "👥 Сотрудники" and user["role"] == "chief":
        await show_employees(update, context)
        return
    if text == "🔄 Изменить роли" and user["role"] == "chief":
        await change_role(update, context)
        return
    if text == "📊 Статистика" and user["role"] == "chief":
        # простая статистика
        tasks = load_tasks()
        user_tasks = [t for t in tasks if t["chief_id"] == tg_id]
        total = len(user_tasks)
        done = len([t for t in user_tasks if t["status"] == "done"])
        await update.message.reply_text(f"📊 Всего: {total}\n✅ Выполнено: {done}\n⏳ В процессе: {total - done}")
        return
    if text == "❓ Помощь":
        await help_command(update, context)
        return
    await update.message.reply_text("❌ Неизвестная команда")

# ---------- Доп. команды управления пользователями (простые) ----------
async def add_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /add_user <tg_id> <name> <surname> — добавляет менеджера под текущим chief """
    caller = str(update.effective_user.id)
    users = load_users()
    caller_user = next((u for u in users if u["tg_id"] == caller), None)
    if not caller_user or caller_user["role"] != "chief":
        await update.message.reply_text("❌ Только начальник может добавлять пользователей.")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Использование: /add_user <tg_id> <name> <surname>")
        return
    tg_id_new = args[0]
    name = args[1]
    surname = " ".join(args[2:])
    if any(u["tg_id"] == tg_id_new for u in users):
        await update.message.reply_text("❌ Пользователь с таким tg_id уже есть.")
        return
    users.append({"tg_id": tg_id_new, "name": name, "surname": surname, "role": "manager", "chief_id": caller})
    save_users(users)
    await update.message.reply_text(f"✅ Пользователь {name} {surname} добавлен как менеджер (ID {tg_id_new}).")

async def set_role_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /set_role <tg_id> <chief|manager> """
    caller = str(update.effective_user.id)
    users = load_users()
    caller_user = next((u for u in users if u["tg_id"] == caller), None)
    if not caller_user or caller_user["role"] != "chief":
        await update.message.reply_text("❌ Только начальник может менять роли.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Использование: /set_role <tg_id> <chief|manager>")
        return
    tg_id_target, new_role = args[0], args[1].lower()
    if new_role not in ("chief", "manager"):
        await update.message.reply_text("Роль должна быть chief или manager.")
        return
    users = load_users()
    target = next((u for u in users if u["tg_id"] == tg_id_target), None)
    if not target:
        await update.message.reply_text("❌ Пользователь не найден.")
        return
    old = target["role"]
    target["role"] = new_role
    if new_role == "chief":
        target["chief_id"] = ""
    save_users(users)
    await update.message.reply_text(f"✅ Роль {tg_id_target} изменена: {old} → {new_role}")

async def set_chief_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /set_chief <tg_id> — делает пользователя начальником (role -> chief) """
    caller = str(update.effective_user.id)
    users = load_users()
    caller_user = next((u for u in users if u["tg_id"] == caller), None)
    if not caller_user or caller_user["role"] != "chief":
        await update.message.reply_text("❌ Только начальник может назначать начальника.")
        return
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Использование: /set_chief <tg_id>")
        return
    tg_id_target = args[0]
    users = load_users()
    target = next((u for u in users if u["tg_id"] == tg_id_target), None)
    if not target:
        await update.message.reply_text("❌ Пользователь не найден.")
        return
    target["role"] = "chief"
    target["chief_id"] = ""
    save_users(users)
    await update.message.reply_text(f"✅ Пользователь {tg_id_target} теперь начальник.")

# ---------- MAIN (Railway ready) ----------
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
        per_message=True
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
        per_message=True
    )

    # Регистрация хендлеров
    app.add_handler(register_conv)
    app.add_handler(task_conv)
    app.add_handler(CallbackQueryHandler(mark_done, pattern="^done:"))
    app.add_handler(CallbackQueryHandler(change_role_callback, pattern="^role:"))

    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add_user", add_user_cmd))
    app.add_handler(CommandHandler("set_role", set_role_cmd))
    app.add_handler(CommandHandler("set_chief", set_chief_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # восстановление напоминаний
    try:
        reload_reminders(app)
    except Exception as e:
        logger.exception("Ошибка при восстановлении напоминаний: %s", e)

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
