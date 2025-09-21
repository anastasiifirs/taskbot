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
import psycopg2
from psycopg2.extras import RealDictCursor
import json
import time

# ---------- Логирование ----------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Состояния ConversationHandler ----------
REGISTER_NAME, REGISTER_SURNAME, TASK_TEXT, CHOOSE_USER, DEADLINE_DATE, DEADLINE_TIME, \
CHOOSE_USER_FOR_ROLE, CHOOSE_NEW_ROLE, CONFIRM_ROLE_CHANGE = range(9)


# ---------- Database Functions ----------
def get_db_connection():
    try:
        database_url = os.getenv("DATABASE_URL")
        
        if not database_url:
            logger.error("DATABASE_URL not found in environment variables")
            return None
        
        # Логируем для отладки (уберите в продакшене)
        logger.info(f"Connecting to database: {database_url[:30]}...")
        
        # Конвертируем postgres:// в postgresql:// для совместимости
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
            logger.info("Converted postgres:// to postgresql://")
        
        # Подключаемся с SSL для Railway
        conn = psycopg2.connect(
            database_url,
            cursor_factory=RealDictCursor,
            sslmode='require'
        )
        logger.info("Database connection successful")
        return conn
        
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None
        
def init_database():
    """Инициализация таблиц при первом запуске"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.error("Не удалось подключиться к базе данных")
            return False
            
        cursor = conn.cursor()
        
        # Создаем таблицу пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id VARCHAR(20) PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                surname VARCHAR(100) NOT NULL,
                role VARCHAR(20) NOT NULL,
                chief_id VARCHAR(20),
                department VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Создаем таблицу задач
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                chief_id VARCHAR(20) NOT NULL,
                assignee_id VARCHAR(20) NOT NULL,
                text TEXT NOT NULL,
                deadline TIMESTAMP NOT NULL,
                status VARCHAR(20) DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chief_id) REFERENCES users(tg_id),
                FOREIGN KEY (assignee_id) REFERENCES users(tg_id)
            )
        """)
        
        conn.commit()
        logger.info("Database tables initialized successfully")
        return True
        
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def load_users():
    """Загрузка всех пользователей из БД"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            # Возвращаем временное хранилище, если БД недоступна
            return temp_users
            
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users ORDER BY created_at")
        users = cursor.fetchall()
        
        # Конвертируем RealDictRow в обычные dict
        return [dict(user) for user in users]
        
    except Exception as e:
        logger.error(f"Error loading users: {e}")
        # Возвращаем временное хранилище при ошибке
        return temp_users
    finally:
        if conn:
            conn.close()
            
def save_user(user):
    """Сохранение одного пользователя в БД"""
    global temp_users
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            # Сохраняем во временное хранилище, если БД недоступна
            # Удаляем старого пользователя с таким tg_id если существует
            temp_users = [u for u in temp_users if u['tg_id'] != user['tg_id']]
            temp_users.append(user)
            logger.info(f"Saved user {user['tg_id']} to temporary storage")
            return True
            
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO users (tg_id, name, surname, role, chief_id, department)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tg_id) DO UPDATE SET
                name = EXCLUDED.name,
                surname = EXCLUDED.surname,
                role = EXCLUDED.role,
                chief_id = EXCLUDED.chief_id,
                department = EXCLUDED.department
        """, (
            user['tg_id'], user['name'], user['surname'], 
            user['role'], user.get('chief_id'), user.get('department')
        ))
        
        conn.commit()
        logger.info(f"Saved user {user['tg_id']} to database")
        return True
        
    except Exception as e:
        logger.error(f"Error saving user: {e}")
        # Сохраняем во временное хранилище при ошибке
        temp_users = [u for u in temp_users if u['tg_id'] != user['tg_id']]
        temp_users.append(user)
        logger.info(f"Saved user {user['tg_id']} to temporary storage due to error")
        return False
    finally:
        if conn:
            conn.close()
            
def load_tasks():
    """Загрузка всех задач из БД"""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            # Возвращаем временное хранилище, если БД недоступна
            return temp_tasks
            
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks ORDER BY created_at")
        tasks = cursor.fetchall()
        
        # Конвертируем и форматируем даты
        tasks_list = []
        for task in tasks:
            task_dict = dict(task)
            if task_dict['deadline']:
                task_dict['deadline'] = task_dict['deadline'].strftime("%Y-%m-%d %H:%M")
            tasks_list.append(task_dict)
            
        return tasks_list
        
    except Exception as e:
        logger.error(f"Error loading tasks: {e}")
        # Возвращаем временное хранилище при ошибке
        return temp_tasks
    finally:
        if conn:
            conn.close()
            
def save_task(task):
    """Сохранение одной задачи в БД"""
    global temp_tasks  # ДОБАВЬТЕ ЭТО В НАЧАЛО ФУНКЦИИ
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            # Сохраняем во временное хранилище, если БД недоступна
            if 'id' not in task or not task['id']:
                # Генерируем ID для новой задачи
                task_id = len(temp_tasks) + 1
                task['id'] = task_id
            else:
                # Обновляем существующую задачу
                temp_tasks = [t for t in temp_tasks if t['id'] != task['id']]
            
            temp_tasks.append(task)
            logger.info(f"Saved task to temporary storage: {task}")
            return task['id']
            
        cursor = conn.cursor()
        
        # Преобразуем строку даты в datetime
        deadline = datetime.datetime.strptime(task['deadline'], "%Y-%m-%d %H:%M")
        
        if 'id' in task and task['id']:
            # Обновление существующей задачи
            cursor.execute("""
                UPDATE tasks 
                SET chief_id = %s, assignee_id = %s, text = %s, deadline = %s, status = %s
                WHERE id = %s
            """, (
                task['chief_id'], task['assignee_id'],
                task['text'], deadline, task['status'], task['id']
            ))
            task_id = task['id']
        else:
            # Вставка новой задачи
            cursor.execute("""
                INSERT INTO tasks (chief_id, assignee_id, text, deadline, status)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                task['chief_id'], task['assignee_id'],
                task['text'], deadline, task.get('status', 'new')
            ))
            result = cursor.fetchone()
            task_id = result['id'] if result else None
        
        conn.commit()
        logger.info(f"Saved task to database: {task}")
        
        return task_id
        
    except Exception as e:
        logger.error(f"Error saving task: {e}")
        # Сохраняем во временное хранилище при ошибке
        if 'id' not in task or not task['id']:
            # Генерируем ID для новой задачи
            task_id = len(temp_tasks) + 1
            task['id'] = task_id
        else:
            # Обновляем существующую задачу
            temp_tasks = [t for t in temp_tasks if t['id'] != task['id']]
        
        temp_tasks.append(task)
        logger.info(f"Saved task to temporary storage due to error: {task}")
        return task['id']
    finally:
        if conn:
            conn.close()
# Временное хранилище данных (если БД недоступна)
temp_users = []
temp_tasks = []

def get_user_subordinates(chief_id, users=None):
    """Получить всех подчиненных пользователя (рекурсивно)"""
    if users is None:
        users = load_users()
        if not users:  # Если БД недоступна, используем временное хранилище
            users = temp_users
    
    direct_subordinates = [u for u in users if u.get('chief_id') == chief_id]
    all_subordinates = direct_subordinates.copy()
    
    for subordinate in direct_subordinates:
        all_subordinates.extend(get_user_subordinates(subordinate['tg_id'], users))
    
    return all_subordinates

def is_user_subordinate(user_id, chief_id, users=None):
    """Проверить, является ли пользователь подчиненным"""
    if users is None:
        users = load_users()
        if not users:  # Если БД недоступна, используем временное хранилище
            users = temp_users
    
    user = next((u for u in users if u['tg_id'] == user_id), None)
    if not user:
        return False
    
    current_chief_id = user.get('chief_id')
    if not current_chief_id:
        return False
    
    if current_chief_id == chief_id:
        return True
    
    # Рекурсивная проверка по цепочке начальников
    return is_user_subordinate(current_chief_id, chief_id, users)

def filter_old_tasks(tasks, max_days_old=2):
    """Фильтрует задачи, выполненные более чем max_days_old дней назад"""
    now = datetime.datetime.now()
    filtered_tasks = []
    
    for task in tasks:
        if task.get("status") != "done":
            # Невыполненные задачи всегда показываем
            filtered_tasks.append(task)
        else:
            # Для выполненных задач проверяем дату
            try:
                if isinstance(task['deadline'], str):
                    deadline_dt = datetime.datetime.strptime(task['deadline'], "%Y-%m-%d %H:%M")
                else:
                    deadline_dt = task['deadline']
                
                # Проверяем, не прошло ли более max_days_old дней с дедлайна
                days_passed = (now - deadline_dt).days
                if days_passed <= max_days_old:
                    filtered_tasks.append(task)
            except (ValueError, KeyError):
                # Если не удалось разобрать дату, показываем задачу
                filtered_tasks.append(task)
    
    return filtered_tasks

# ---------- Клавиатуры ----------
def get_main_keyboard(role):
    if role == "director":
        buttons = [
            [KeyboardButton("📝 Создать задачу"), KeyboardButton("📋 Все задачи")],
            [KeyboardButton("👥 Все сотрудники"), KeyboardButton("🔄 Изменить роли")],
            [KeyboardButton("📊 Статистика")]
        ]
    elif role == "chief":
        buttons = [
            [KeyboardButton("📝 Создать задачу"), KeyboardButton("📋 Задачи отдела")],
            [KeyboardButton("👥 Мои сотрудники"), KeyboardButton("📊 Статистика отдела")]
        ]
    else:  # manager
        buttons = [
            [KeyboardButton("📋 Мои задачи"), KeyboardButton("✅ Выполненные")],
            [KeyboardButton("❓ Помощь")]
        ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    
# ---------- Напоминания ----------
async def send_deadline_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    task_id = data.get("task_id")
    tasks = load_tasks()
    task = next((t for t in tasks if str(t["id"]) == str(task_id)), None)
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
            try:
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
            except Exception as e:
                logger.error(f"Ошибка планирования напоминания: {e}")

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if user:
        keyboard = get_main_keyboard(user["role"])
        role_name = "Директор управления" if user["role"] == "director" else "Начальник отдела" if user["role"] == "chief" else "Менеджер"
        await update.message.reply_text(
            f"🔑 С возвращением, {user['name']} {user['surname']}! Ты {role_name}.",
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
    global temp_users
    surname = update.message.text.strip()
    context.user_data["surname"] = surname
    tg_id = context.user_data["tg_id"]
    name = context.user_data["name"]
    
    users = load_users()
    
    if not users:
        # Первый пользователь - директор управления
        new_user = {
            "tg_id": tg_id, 
            "name": name, 
            "surname": surname, 
            "role": "director", 
            "chief_id": None,
            "department": "management"
        }
        save_user(new_user)  # Эта функция уже сохраняет во временное хранилище
        # УБЕРИТЕ ЭТУ СТРОКУ: temp_users.append(new_user)
        keyboard = get_main_keyboard("director")
        await update.message.reply_text(
            f"👑 Привет, {name} {surname}! Ты зарегистрирован как ДИРЕКТОР УПРАВЛЕНИЯ.",
            reply_markup=keyboard
        )
        return ConversationHandler.END
    else:
        # Обычный пользователь - менеджер
        # Находим директора как начальника по умолчанию
        director = next((u for u in users if u["role"] == "director"), None)
        chief_id = director["tg_id"] if director else None
        
        new_user = {
            "tg_id": tg_id, 
            "name": name, 
            "surname": surname, 
            "role": "manager", 
            "chief_id": chief_id,
            "department": "general"
        }
        save_user(new_user)  # Эта функция уже сохраняет во временное хранилище

        keyboard = get_main_keyboard("manager")
        await update.message.reply_text(
            f"👋 Привет, {name} {surname}! Ты зарегистрирован как МЕНЕДЖЕР.",
            reply_markup=keyboard
        )
        return ConversationHandler.END
        
# ---------- Task handlers ----------
async def task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    # Проверяем роль пользователя из базы данных, а не из контекста
    if not user or user["role"] not in ["director", "chief"]:
        await update.message.reply_text("❌ Только директор и начальники могут создавать задачи.")
        return ConversationHandler.END
    
    context.user_data["task_creator_role"] = user["role"]
    context.user_data["task_creator_id"] = tg_id
    
    await update.message.reply_text("Введите текст задачи:")
    return TASK_TEXT
    
async def task_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["task_text"] = update.message.text.strip()
    tg_id = str(update.effective_user.id)
    users = load_users()
    
    creator_role = context.user_data.get("task_creator_role", "")
    creator_id = context.user_data.get("task_creator_id", "")
    
    if creator_role == "director":
        # Директор видит всех сотрудников кроме себя
        subs = [u for u in users if u["tg_id"] != creator_id]
    elif creator_role == "chief":
        # Начальник видит только менеджеров (своих подчиненных и других менеджеров)
        # Все менеджеры в системе
        subs = [u for u in users if u["role"] == "manager"]
    else:
        await update.message.reply_text("❌ Только директор и начальники могут создавать задачи.")
        return ConversationHandler.END
    
    if not subs:
        await update.message.reply_text("❌ Нет доступных сотрудников для назначения задачи.")
        return ConversationHandler.END
    
    buttons = []
    for u in subs:
        role_emoji = "👑" if u["role"] == "director" else "👤" if u["role"] == "chief" else "💼"
        buttons.append([InlineKeyboardButton(
            f"{role_emoji} {u['name']} {u['surname']} ({u['role']})", 
            callback_data=f"assign:{u['tg_id']}"
        )])
    
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
        await update.message.reply_text("❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ (например: 20.09.2025):")
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
            "chief_id": chief_id,
            "assignee_id": assignee_id,
            "text": text,
            "deadline": deadline_str,
            "status": "new"
        }
        
        # Сохраняем задачу и получаем её ID из БД
        task_id = save_task(new_task)
        if not task_id:
            await update.message.reply_text("❌ Ошибка при сохранении задачи.")
            return ConversationHandler.END

        # Планируем напоминания
        try:
            schedule_deadline_reminders(
                context.application, 
                str(task_id), 
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
        for key in ["task_text", "assignee_id", "deadline_date", "task_creator_role", "task_creator_id"]:
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
    
    if not user or user["role"] != "director":
        await update.message.reply_text("❌ Только директор управления может изменять роли.")
        return ConversationHandler.END
    
    # Директор может выбирать любого сотрудника кроме себя
    subs = [u for u in users if u["tg_id"] != tg_id]
    
    if not subs:
        await update.message.reply_text("📭 Нет других сотрудников для изменения ролей.")
        return ConversationHandler.END
    
    buttons = []
    for u in subs:
        role_emoji = "👑" if u["role"] == "director" else "👤" if u["role"] == "chief" else "💼"
        role_name = "Директор" if u["role"] == "director" else "Начальник" if u["role"] == "chief" else "Менеджер"
        buttons.append([InlineKeyboardButton(
            f"{role_emoji} {u['name']} {u['surname']} ({role_name})", 
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
    
    # Предлагаем доступные роли для изменения
    available_roles = []
    if current_role == "director":
        await query.edit_message_text("❌ Нельзя изменить роль директора.")
        return ConversationHandler.END
    elif current_role == "chief":
        available_roles = ["manager", "director"]
    else:  # manager
        available_roles = ["chief", "director"]
    
    context.user_data["available_roles"] = available_roles
    context.user_data["current_role"] = current_role
    
    buttons = []
    for role in available_roles:
        role_name = "Директор управления" if role == "director" else "Начальник отдела" if role == "chief" else "Менеджер"
        buttons.append([InlineKeyboardButton(role_name, callback_data=f"choose_role:{role}")])
    
    keyboard = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(
        f"Выберите новую роль для {user['name']} {user['surname']}:\n"
        f"Текущая роль: {current_role}",
        reply_markup=keyboard
    )
    return CHOOSE_NEW_ROLE

async def choose_new_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    new_role = query.data.split(":")[1]
    context.user_data["new_role"] = new_role
    
    user_id = context.user_data["role_user_id"]
    users = load_users()
    user = next((u for u in users if u["tg_id"] == user_id), None)
    
    if not user:
        await query.edit_message_text("❌ Сотрудник не найден.")
        return ConversationHandler.END
    
    current_role = context.user_data["current_role"]
    
    role_names = {
        "director": "Директор управления",
        "chief": "Начальник отдела", 
        "manager": "Менеджер"
    }
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data="confirm_role:yes")],
        [InlineKeyboardButton("❌ Нет", callback_data="confirm_role:no")]
    ])
    
    await query.edit_message_text(
        f"Изменить роль сотрудника {user['name']} {user['surname']}?\n"
        f"Текущая роль: {role_names.get(current_role, current_role)}\n"
        f"Новая роль: {role_names.get(new_role, new_role)}\n\n"
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
    old_role = context.user_data["current_role"]
    
    users = load_users()
    user = next((u for u in users if u["tg_id"] == user_id), None)
    
    if not user:
        await query.edit_message_text("❌ Сотрудник не найден.")
        return ConversationHandler.END
    
    # Сохраняем новую роль
    user["role"] = new_role
    
    # Обрабатываем логику изменения иерархии
    if new_role == "chief" and old_role == "manager":
        # Повышение менеджера до начальника
        user["department"] = f"Отдел {user['surname']}"
        # Начальник подчиняется директору
        director = next((u for u in users if u["role"] == "director"), None)
        if director:
            user["chief_id"] = director["tg_id"]
        
    elif new_role == "manager" and old_role == "chief":
        # Понижение начальника до менеджера
        user["department"] = None
        # Находим нового начальника (директора)
        director = next((u for u in users if u["role"] == "director"), None)
        if director:
            user["chief_id"] = director["tg_id"]
        
        # Переназначаем всех подчиненных бывшего начальника директору
        subordinates = get_user_subordinates(user_id, users)
        for sub in subordinates:
            if sub["tg_id"] != user_id:
                sub["chief_id"] = director["tg_id"] if director else None
                save_user(sub)
    
    elif new_role == "director":
        # Назначение директором
        user["chief_id"] = None
        user["department"] = "management"
    
    # Сохраняем изменения пользователя
    save_user(user)
    
    # Принудительно обновляем данные пользователя
    users = load_users()  # Перезагружаем пользователей
    updated_user = next((u for u in users if u["tg_id"] == user_id), None)
    
    if not updated_user:
        await query.edit_message_text("❌ Ошибка при обновлении роли.")
        return ConversationHandler.END
    
    # Отправляем уведомление сотруднику с новым меню
    try:
        role_names = {
            "director": "директором управления",
            "chief": "начальником отдела", 
            "manager": "менеджером"
        }
        role_text = role_names.get(new_role, new_role)
        
        keyboard = get_main_keyboard(new_role)
        
        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"🎉 Ваша роль изменена! Теперь вы {role_text}.\n\n"
                 f"Используйте /start для обновления меню.",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления сотруднику: {e}")
    
    role_names = {
        "director": "Директор управления",
        "chief": "Начальник отдела",
        "manager": "Менеджер"
    }
    
    await query.edit_message_text(
        f"✅ Роль сотрудника {user['name']} {user['surname']} изменена:\n"
        f"С {role_names.get(old_role, old_role)} на {role_names.get(new_role, new_role)}\n\n"
        f"Сотруднику отправлено уведомление с новым меню."
    )
    
    # Очищаем данные
    for key in ["role_user_id", "new_role", "current_role", "available_roles"]:
        context.user_data.pop(key, None)
    
    return ConversationHandler.END

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительное обновление меню"""
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return
    
    keyboard = get_main_keyboard(user["role"])
    role_name = "Директор управления" if user["role"] == "director" else "Начальник отдела" if user["role"] == "chief" else "Менеджер"
    
    await update.message.reply_text(
        f"🔄 Меню обновлено! Ты {role_name}.",
        reply_markup=keyboard
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущей операции"""
    await update.message.reply_text("Операция отменена.")
    # Очищаем user_data
    context.user_data.clear()
    return ConversationHandler.END
    
# ---------- Mark task as done ----------
async def mark_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    task_id = query.data.split(":")[1]
    tasks = load_tasks()
    task = next((t for t in tasks if str(t["id"]) == task_id), None)
    
    if not task:
        await query.edit_message_text("❌ Задача не найдена.")
        return
    
    task["status"] = "done"
    save_task(task)
    
    # Уведомляем начальника
    try:
        users = load_users()
        assignee = next((u for u in users if u["tg_id"] == task["assignee_id"]), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else task["assignee_id"]
        
        deadline_dt = datetime.datetime.strptime(task['deadline'], "%Y-%m-%d %H:%M")
        deadline_str = deadline_dt.strftime("%d.%m.%Y %H:%M")
        
        await context.bot.send_message(
            int(task["chief_id"]),
            f"✅ Подчинённый {assignee_name} выполнил задачу:\n{task['text']}\n⏰ Дедлайн был: {deadline_str}"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления начальнику: {e}")
    
    await query.edit_message_text(f"✅ Задача выполнена: {task['text']}")

# ---------- Task display functions ----------
async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    tasks = load_tasks()
    
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return
    
    # Фильтруем старые выполненные задачи
    tasks = filter_old_tasks(tasks, max_days_old=2)
    
    # Определяем, какие задачи показывать в зависимости от роли
    if user["role"] == "director":
        # Директор видит все задачи
        user_tasks = tasks
        title = "📋 ВСЕ ЗАДАЧИ В СИСТЕМЕ:\n\n"
    elif user["role"] == "chief":
        # Начальник видит задачи, которые он поставил, и задачи своего отдела
        user_tasks = [t for t in tasks if t["chief_id"] == tg_id or 
                     is_user_subordinate(t["assignee_id"], tg_id, users)]
        title = "📋 ЗАДАЧИ МОЕГО ОТДЕЛА:\n\n"
    else:  # manager
        # Менеджер видит только свои задачи
        user_tasks = [t for t in tasks if t["assignee_id"] == tg_id]
        title = "📋 МОИ ЗАДАЧИ:\n\n"
    
    if not user_tasks:
        await update.message.reply_text("📭 Нет задач")
        return
    
    msg = title
    for t in user_tasks:
        status = "✅" if t["status"] == "done" else "⏳"
        assignee = next((u for u in users if u["tg_id"] == t["assignee_id"]), None)
        assignee_name = f"{assignee['name']} {assignee['surname']}" if assignee else t["assignee_id"]
        
        chief = next((u for u in users if u["tg_id"] == t["chief_id"]), None)
        chief_name = f"{chief['name']} {chief['surname']}" if chief else t["chief_id"]
        
        deadline_dt = datetime.datetime.strptime(t["deadline"], "%Y-%m-%d %H:%M")
        deadline_str = deadline_dt.strftime("%d.%m.%Y %H:%M")
        
        msg += f"{status} Задача #{t['id']}\n"
        msg += f"📝 {t['text']}\n"
        msg += f"👤 Исполнитель: {assignee_name}\n"
        if user["role"] == "director":
            msg += f"👑 Постановщик: {chief_name}\n"
        msg += f"⏰ Дедлайн: {deadline_str}\n"
        msg += f"📊 Статус: {'Выполнено' if t['status'] == 'done' else 'В работе'}\n\n"
    
    await update.message.reply_text(msg)

async def show_completed_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    tasks = load_tasks()
    
    if not user or user["role"] != "manager":
        await update.message.reply_text("❌ Эта функция доступна только менеджерам.")
        return
    
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

# ---------- Employee display functions ----------
async def show_employees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return
    
    if user["role"] == "director":
        # Директор видит всех
        subs = [u for u in users if u["tg_id"] != tg_id]
        title = "👥 ВСЕ СОТРУДНИКИ:\n\n"
    elif user["role"] == "chief":
        # Начальник видит только своих подчиненных
        subs = get_user_subordinates(tg_id, users)
        title = "👥 ВАШИ СОТРУДНИКИ:\n\n"
    else:
        await update.message.reply_text("❌ Эта функция доступна только директору и начальникам.")
        return
    
    if not subs:
        await update.message.reply_text("📭 Нет сотрудников")
        return
    
    message = title
    for sub in subs:
        role_emoji = "👑" if sub["role"] == "director" else "👤" if sub["role"] == "chief" else "💼"
        role_name = "Директор" if sub["role"] == "director" else "Начальник" if sub["role"] == "chief" else "Менеджер"
        message += f"{role_emoji} {sub['name']} {sub['surname']} - {role_name}"
        if sub["role"] == "chief":
            message += f" ({sub.get('department', 'без отдела')})"
        message += "\n"
    
    await update.message.reply_text(message)

# ---------- Statistics functions ----------
async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    tasks = load_tasks()
    
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return
    
    if user["role"] == "director":
        # Статистика по всем задачам
        user_tasks = tasks
        title = "📊 ОБЩАЯ СТАТИСТИКА:\n\n"
    elif user["role"] == "chief":
        # Статистика по отделу
        user_tasks = [t for t in tasks if t["chief_id"] == tg_id or 
                     is_user_subordinate(t["assignee_id"], tg_id, users)]
        title = "📊 СТАТИСТИКА ОТДЕЛА:\n\n"
    else:
        await update.message.reply_text("❌ Эта функция доступна только директору и начальникам.")
        return
    
    if not user_tasks:
        await update.message.reply_text(f"{title}Нет задач")
        return
    
    total_tasks = len(user_tasks)
    completed_tasks = len([t for t in user_tasks if t["status"] == "done"])
    pending_tasks = total_tasks - completed_tasks
    
    completion_rate = round((completed_tasks / total_tasks) * 100) if total_tasks > 0 else 0
    
    message = title
    message += f"📋 Всего задач: {total_tasks}\n"
    message += f"✅ Выполнено: {completed_tasks}\n"
    message += f"⏳ В процессе: {pending_tasks}\n"
    message += f"📈 Процент выполнения: {completion_rate}%"
    
    await update.message.reply_text(message)

# ---------- Help function ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if user and user["role"] == "director":
        help_text = (
            "👑 Команды ДИРЕКТОРА:\n"
            "📝 Создать задачу - назначить задачу любому сотруднику\n"
            "📋 Все задачи - просмотреть все задачи в системе\n"
            "👥 Все сотрудники - список всех сотрудников\n"
            "🔄 Изменить роли - изменить роль любого сотрудника\n"
            "📊 Статистика - общая статистика по задачам\n"
            "🏢 Отделы - управление отделами"
        )
    elif user and user["role"] == "chief":
        help_text = (
            "👤 Команды НАЧАЛЬНИКА:\n"
            "📝 Создать задачу - назначить задачу своим менеджерам\n"
            "📋 Задачи отдела - просмотреть задачи отдела\n"
            "👥 Мои сотрудники - список менеджеров отдела\n"
            "📊 Статистика отдела - статистика по отделу"
        )
    else:
        help_text = (
            "💼 Команды МЕНЕДЖЕРА:\n"
            "📋 Мои задачи - просмотреть текущие задачи\n"
            "✅ Выполненные - просмотреть выполненные задачи\n"
            "❓ Помощь - показать эту справку"
        )
    
    await update.message.reply_text(help_text)

# ---------- Text message handler ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    tg_id = str(update.effective_user.id)
    users = load_users()
    user = next((u for u in users if u["tg_id"] == tg_id), None)
    
    if not user:
        await update.message.reply_text("❌ Сначала /start")
        return
    
    # Обработка отмены
    if text.lower() in ['отмена', 'cancel']:
        return await cancel(update, context)
    
    # Всегда проверяем роль из базы данных, а не из кэша
    if user["role"] == "director":
        if text == "📝 Создать задачу":
            await task(update, context)
        elif text == "📋 Все задачи":
            await show_tasks(update, context)
        elif text == "👥 Все сотрудники":
            await show_employees(update, context)
        elif text == "🔄 Изменить роли":
            await change_role(update, context)
        elif text == "📊 Статистика":
            await show_statistics(update, context)
        elif text == "❓ Помощь":
            await help_command(update, context)
        else:
            await update.message.reply_text("❌ Неизвестная команда")
            
    elif user["role"] == "chief":
        if text == "📝 Создать задаче":
            await task(update, context)
        elif text == "📋 Задачи отдела":
            await show_tasks(update, context)
        elif text == "👥 Мои сотрудники":
            await show_employees(update, context)
        elif text == "📊 Статистика отдела":
            await show_statistics(update, context)
        elif text == "❓ Помощь":
            await help_command(update, context)
        else:
            await update.message.reply_text("❌ Неизвестная команда")
            
    else:  # manager
        if text == "📋 Мои задачи":
            await show_tasks(update, context)
        elif text == "✅ Выполненные":
            await show_completed_tasks(update, context)
        elif text == "❓ Помощь":
            await help_command(update, context)
        else:
            await update.message.reply_text("❌ Неизвестная команда")
            
def reload_all_reminders(application: Application):
    """Восстановление всех напоминаний при запуске"""
    global temp_tasks  # ДОБАВЬТЕ ЭТО
    try:
        tasks = load_tasks()
        if not tasks:  # Если БД недоступна, используем временное хранилище
            tasks = temp_tasks
            
        now = datetime.datetime.now()
        count = 0
        
        for task in tasks:
            if task.get("status") == "new" and task.get("deadline"):
                try:
                    deadline_str = task["deadline"]
                    if isinstance(deadline_str, str):
                        deadline_dt = datetime.datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
                    else:
                        deadline_dt = deadline_str
                    
                    if deadline_dt > now:
                        schedule_deadline_reminders(
                            application,
                            str(task["id"]),
                            int(task["chief_id"]),
                            int(task["assignee_id"]),
                            task["text"],
                            deadline_dt
                        )
                        count += 1
                        
                except Exception as e:
                    logger.error(f"Ошибка восстановления напоминания задачи {task['id']}: {e}")
        
        logger.info(f"Восстановлено {count} напоминаний")
        
    except Exception as e:
        logger.error(f"Ошибка восстановления напоминаний: {e}")
        
# ---------- MAIN ----------
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN not found in environment variables.")
        return
    
    # Проверяем переменные окружения
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.warning("DATABASE_URL not found. Using temporary storage.")
    
    # Проверяем подключение к базе данных
    db_available = False
    if database_url:
        for attempt in range(3):
            try:
                db_available = init_database()
                if db_available:
                    logger.info("Database connection successful")
                    break
                else:
                    logger.warning(f"Database connection failed, attempt {attempt + 1}/3")
                    time.sleep(2)
            except Exception as e:
                logger.error(f"Database initialization error: {e}")
                time.sleep(2)
    
    if not db_available:
        logger.warning("Database is not available. Using temporary storage.")
        global temp_users, temp_tasks
        temp_users = []
        temp_tasks = []
    
    # Загружаем существующие данные из БД или используем временное хранилище
    if db_available:
        users = load_users()
        tasks = load_tasks()
        logger.info(f"Loaded {len(users)} users and {len(tasks)} tasks from database")
    else:
        users = temp_users
        tasks = temp_tasks
        logger.info("Using temporary storage for users and tasks")
    
    app = Application.builder().token(token).build()

    # Регистрация - ИСПРАВЛЕННЫЙ СИНТАКСИС
    register_conv = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
        REGISTER_SURNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_surname)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Создание задачи - ИСПРАВЛЕННЫЙ СИНТАКСИС
    task_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^📝 Создать задачу$") & filters.TEXT, task)],
        states={
            TASK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, task_text_handler)],
            CHOOSE_USER: [CallbackQueryHandler(assign_task, pattern=r"^assign:")],
            DEADLINE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_date_handler)],
            DEADLINE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deadline_time_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(r"^(отмена|cancel)$") & filters.TEXT, cancel)],
    )

    # Изменение ролей - ИСПРАВЛЕННЫЙ СИНТАКСИС
    role_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🔄 Изменить роли$") & filters.TEXT, change_role)],
        states={
            CHOOSE_USER_FOR_ROLE: [CallbackQueryHandler(choose_user_for_role, pattern=r"^role_user:")],
            CHOOSE_NEW_ROLE: [CallbackQueryHandler(choose_new_role, pattern=r"^choose_role:")],
            CONFIRM_ROLE_CHANGE: [CallbackQueryHandler(confirm_role_change, pattern=r"^confirm_role:")],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(r"^(отмена|cancel)$") & filters.TEXT, cancel)],
    )

    # Добавляем обработчики
    app.add_handler(register_conv)
    app.add_handler(task_conv)
    app.add_handler(role_conv)
    app.add_handler(CallbackQueryHandler(mark_done, pattern=r"^done:"))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CommandHandler("refresh", refresh))

    # Восстанавливаем напоминания
    reload_all_reminders(app)

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
