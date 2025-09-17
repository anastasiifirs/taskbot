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
ADD_USER, SET_ROLE_USER, SET_ROLE_CHOICE, SET_CHIEF_USER = range(6,10)

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
    data = context.job.data
    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=f"⏰ НАПОМИНАНИЕ: Задача '{data['task_text']}' должна быть выполнена до {data['deadline']}"
    )

def schedule_deadline_reminders(task_id, chief_id, assignee_id, task_text, deadline_str):
    deadline = datetime.datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
    reminders = [
        ("1_day", deadline - datetime.timedelta(days=1)),
        ("1_hour", deadline - datetime.timedelta(hours=1)),
        ("after_deadline", deadline + datetime.timedelta(hours=1))
    ]
    for name, run_time in reminders:
        if run_time > datetime.datetime.now():
            chat_id = assignee_id if name != "after_deadline" else chief_id
            display_deadline = deadline_str if name != "after_deadline" else f"{deadline_str} (ПРОСРОЧЕНО)"
            scheduler.add_job(
                send_deadline_reminder,
                DateTrigger(run_date=run_time),
                kwargs={"data":{"task_id":task_id,"chat_id":chat_id,"task_text":task_text,"deadline":display_deadline}}
            )

# --- Старт и помощь ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"]==tg_id), None)
    if not users:
        users.append({"tg_id":tg_id,"role":"chief","chief_id":""})
        save_users(users)
        keyboard = get_main_keyboard("chief")
        await update.message.reply_text("👨‍💼 Ты зарегистрирован как НАЧАЛЬНИК.", reply_markup=keyboard)
        return
    if not user:
        chief_id = next((u["tg_id"] for u in users if u["role"]=="chief"), "")
        users.append({"tg_id":tg_id,"role":"manager","chief_id":chief_id})
        save_users(users)
        keyboard = get_main_keyboard("manager")
        await update.message.reply_text("👋 Ты зарегистрирован как МЕНЕДЖЕР.", reply_markup=keyboard)
    else:
        keyboard = get_main_keyboard(user["role"])
        await update.message.reply_text(f"🔑 С возвращением! Ты {user['role']}.", reply_markup=keyboard)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"]==tg_id), None)
    if not user: return
    if user["role"]=="chief":
        text = ("👨‍💼 Начальник:\n📝 Создать задачу\n📋 Мои задачи\n👥 Сотрудники\n🔄 Изменить роли\n📊 Статистика\n"
                "➕ /add_user\n⚙️ /set_role\n👑 /set_chief")
    else:
        text = "👨‍💼 Менеджер:\n📋 Мои задачи\n✅ Выполненные\n❓ Помощь"
    await update.message.reply_text(text)

# --- Добавление пользователя ---
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"]==tg_id), None)
    if not user or user["role"]!="chief":
        await update.message.reply_text("❌ Только начальник может добавлять пользователей")
        return ConversationHandler.END
    await update.message.reply_text("Введите TG ID нового пользователя:")
    return ADD_USER

async def add_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_id = update.message.text
    users = load_users()
    if any(u["tg_id"]==new_id for u in users):
        await update.message.reply_text("❌ Пользователь уже существует")
        return ConversationHandler.END
    chief_id = str(update.effective_user.id)
    users.append({"tg_id":new_id,"role":"manager","chief_id":chief_id})
    save_users(users)
    await update.message.reply_text(f"✅ Пользователь {new_id} добавлен как менеджер под вашим руководством")
    return ConversationHandler.END

# --- Изменение роли ---
async def set_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"]==tg_id), None)
    if not user or user["role"]!="chief":
        await update.message.reply_text("❌ Только начальник может менять роли")
        return ConversationHandler.END
    await update.message.reply_text("Введите TG ID сотрудника, чью роль хотите изменить:")
    return SET_ROLE_USER

async def set_role_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.text
    users = load_users()
    target = next((u for u in users if u["tg_id"]==user_id), None)
    if not target:
        await update.message.reply_text("❌ Пользователь не найден")
        return ConversationHandler.END
    context.user_data["set_role_user_id"] = user_id
    role_options = InlineKeyboardMarkup([
        [InlineKeyboardButton("manager", callback_data="role:manager"),
         InlineKeyboardButton("chief", callback_data="role:chief")]
    ])
    await update.message.reply_text(f"Выберите новую роль для {user_id}:", reply_markup=role_options)
    return SET_ROLE_CHOICE

async def set_role_choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    new_role = query.data.split(":")[1]
    user_id = context.user_data["set_role_user_id"]
    users = load_users()
    target = next((u for u in users if u["tg_id"]==user_id), None)
    if target:
        old_role = target["role"]
        target["role"] = new_role
        save_users(users)
        await query.edit_message_text(f"✅ Роль пользователя {user_id} изменена: {old_role} → {new_role}")
        try:
            await context.bot.send_message(int(user_id), f"🎉 Ваша роль изменена! Теперь вы {new_role}")
        except: pass
    return ConversationHandler.END

# --- Назначение нового начальника ---
async def set_chief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"]==tg_id), None)
    if not user or user["role"]!="chief":
        await update.message.reply_text("❌ Только начальник может назначить нового начальника")
        return ConversationHandler.END
    await update.message.reply_text("Введите TG ID сотрудника, которого хотите сделать начальником:")
    return SET_CHIEF_USER

async def set_chief_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_chief_id = update.message.text
    users = load_users()
    target = next((u for u in users if u["tg_id"]==new_chief_id), None)
    if not target:
        await update.message.reply_text("❌ Пользователь не найден")
        return ConversationHandler.END
    target["role"] = "chief"
    target["chief_id"] = ""
    save_users(users)
    await update.message.reply_text(f"✅ Пользователь {new_chief_id} теперь начальник!")
    try:
        await context.bot.send_message(int(new_chief_id), "🎉 Вы назначены начальником! Используйте /start для обновления меню.")
    except: pass
    return ConversationHandler.END

# --- ConversationHandlers ---
add_user_conv = ConversationHandler(
    entry_points=[CommandHandler("add_user", add_user)],
    states={ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_user_handler)]},
    fallbacks=[CommandHandler("cancel", lambda u,c:u.message.reply_text("Операция отменена"))]
)

set_role_conv = ConversationHandler(
    entry_points=[CommandHandler("set_role", set_role)],
    states={
        SET_ROLE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_role_user_handler)],
        SET_ROLE_CHOICE: [CallbackQueryHandler(set_role_choice_handler, pattern="^role:")]
    },
    fallbacks=[CommandHandler("cancel", lambda u,c:u.message.reply_text("Операция отменена"))]
)

set_chief_conv = ConversationHandler(
    entry_points=[CommandHandler("set_chief", set_chief)],
    states={SET_CHIEF_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_chief_user_handler)]},
    fallbacks=[CommandHandler("cancel", lambda u,c:u.message.reply_text("Операция отменена"))]
)

# --- Здесь добавляем также код создания задач, дедлайнов, уведомлений и mark_done ---
# Твой уже исправленный ConversationHandler с DEADLINE_DATE и DEADLINE_TIME можно вставить сюда

app = Application.builder().token("8377447196:AAHPqerv_P6zgKvL9GIv_4mmz4ygSK5GOGE").build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(add_user_conv)
app.add_handler(set_role_conv)
app.add_handler(set_chief_conv)

print("Бот запущен...")
app.run_polling()
