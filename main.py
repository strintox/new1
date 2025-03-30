import os
import json
from io import BytesIO
import logging
import base64
import requests
import time
import asyncio
import tempfile
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackContext, filters, CallbackQueryHandler
from telegram.constants import ParseMode

# Импорты для работы с файлами
import PyPDF2
from docx import Document
import io
import magic  # для определения MIME-типа файла
import filetype  # резервная библиотека для определения типа файла

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
TELEGRAM_BOT_TOKEN = "7639285272:AAH-vhuRyoVDMNjqyvkDgfsZw7_d5GEc77Q"
ADMIN_ID = 8199808170
ADMIN_USERNAME = "aunex"  # Имя пользователя администратора
ANTHROPIC_API_KEY = "sk-Rr88gyoBb4RD9ipDp4vHqXa9W0CkA8piOCN8swUfvqsCiuOf2j5Eg-aNqRwgUKyHw6n2qvtlIb1uSV385QUfpA"
ANTHROPIC_API_URL = "https://api.langdock.com/anthropic/eu/v1/messages"
DEFAULT_CREDITS = 10
MAX_MEMORY_MESSAGES = 10
MAX_MESSAGE_LENGTH = 4000  # Максимальная длина сообщения в Telegram
CREDIT_RESET_HOURS = 10   # Период сброса кредитов (в часах)
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 МБ максимальный размер файла
# Поддерживаемые типы файлов
SUPPORTED_FILE_TYPES = {
    'application/pdf': 'PDF',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
    'text/plain': 'TXT',
    'text/csv': 'CSV',
    'application/json': 'JSON',
    'text/markdown': 'Markdown',
    'text/html': 'HTML',
    'application/xml': 'XML',
    'application/rtf': 'RTF',
    'application/msword': 'DOC',
    'application/vnd.ms-excel': 'XLS',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
    'application/vnd.ms-powerpoint': 'PPT',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PPTX',
    'image/jpeg': 'JPEG',
    'image/png': 'PNG',
    'image/gif': 'GIF',
    'image/webp': 'WEBP',
    'image/svg+xml': 'SVG'
}

# Хранение данных пользователей
users_data = {}
# Память для хранения истории сообщений пользователей
user_memory = {}
# Для отслеживания процессов восстановления кредитов
credit_reset_tasks = {}
# Для хранения обрабатываемых файлов
processing_files = set()

# Константы для клавиатур
USER_KEYBOARD_COMMANDS = ["💰 Баланс", "🔄 Сбросить историю", "ℹ️ Помощь"]
ADMIN_KEYBOARD_COMMANDS = ["📊 Список пользователей", "➕ Добавить кредиты", "➖ Снять кредиты", 
                          "🌟 Дать безлимит", "⭐ Убрать безлимит", "💰 Мой баланс", 
                          "🔄 Сбросить историю", "ℹ️ Помощь"]

# Создание клавиатуры для пользователя
def get_user_keyboard():
    keyboard = [USER_KEYBOARD_COMMANDS]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Создание клавиатуры для администратора
def get_admin_keyboard():
    keyboard = [ADMIN_KEYBOARD_COMMANDS[:4]]
    keyboard.append(ADMIN_KEYBOARD_COMMANDS[4:])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Загрузка данных пользователей из файла, если он существует
def load_users_data():
    global users_data
    try:
        if os.path.exists('users_data.json'):
            with open('users_data.json', 'r', encoding='utf-8') as file:
                users_data = json.load(file)
                # Добавление полей для времени сброса кредитов, если их нет
                for user_id in users_data:
                    if "next_reset_time" not in users_data[user_id]:
                        users_data[user_id]["next_reset_time"] = (datetime.now() + timedelta(hours=CREDIT_RESET_HOURS)).isoformat()
                    if "unlimited" not in users_data[user_id]:
                        users_data[user_id]["unlimited"] = False
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных пользователей: {e}")
        users_data = {}

# Сохранение данных пользователей в файл
def save_users_data():
    try:
        with open('users_data.json', 'w', encoding='utf-8') as file:
            json.dump(users_data, file, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных пользователей: {e}")

# Инициализация нового пользователя
def init_user(user_id):
    if str(user_id) not in users_data:
        users_data[str(user_id)] = {
            "credits": DEFAULT_CREDITS if user_id != ADMIN_ID else float('inf'),
            "unlimited": user_id == ADMIN_ID,
            "name": "",
            "username": "",
            "next_reset_time": (datetime.now() + timedelta(hours=CREDIT_RESET_HOURS)).isoformat()
        }
        save_users_data()

# Инициализация памяти пользователя
def init_user_memory(user_id):
    if user_id not in user_memory:
        user_memory[user_id] = []

# Отправка запроса к API Anthropic
async def query_anthropic(messages):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY
    }
    
    payload = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": messages,
        "max_tokens": 4000
    }
    
    try:
        response = requests.post(ANTHROPIC_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса к Anthropic API: {e}")
        return {"error": str(e)}

# Функция для разбивки длинного текста на части
def split_text(text, max_length=MAX_MESSAGE_LENGTH):
    if len(text) <= max_length:
        return [text]
    
    parts = []
    current_part = ""
    sentences = text.split(". ")
    
    for sentence in sentences:
        if len(current_part) + len(sentence) + 2 <= max_length:
            if current_part:
                current_part += ". " + sentence
            else:
                current_part = sentence
        else:
            if current_part:
                parts.append(current_part + ".")
                current_part = sentence
            else:
                # Если одно предложение слишком длинное, разбиваем его по словам
                words = sentence.split(" ")
                current_part = ""
                for word in words:
                    if len(current_part) + len(word) + 1 <= max_length:
                        if current_part:
                            current_part += " " + word
                        else:
                            current_part = word
                    else:
                        parts.append(current_part)
                        current_part = word
                if current_part:
                    parts.append(current_part)
                current_part = ""
    
    if current_part:
        parts.append(current_part)
    
    return parts

# Отправка длинного сообщения частями
async def send_long_message(update, text, reply_markup=None):
    parts = split_text(text)
    # Добавляем нумерацию частей если частей больше одной
    if len(parts) > 1:
        for i, part in enumerate(parts):
            # Последняя часть получает клавиатуру
            if i == len(parts) - 1 and reply_markup:
                await update.message.reply_text(f"Часть {i+1}/{len(parts)}:\n\n{part}", reply_markup=reply_markup)
            else:
                await update.message.reply_text(f"Часть {i+1}/{len(parts)}:\n\n{part}")
            # Небольшая задержка между сообщениями чтобы избежать ограничений Telegram
            await asyncio.sleep(0.5)
    else:
        await update.message.reply_text(parts[0], reply_markup=reply_markup)

# Функция для сброса кредитов
async def reset_credits(user_id, context):
    if str(user_id) in users_data and not users_data[str(user_id)].get("unlimited", False):
        users_data[str(user_id)]["credits"] = DEFAULT_CREDITS
        users_data[str(user_id)]["next_reset_time"] = (datetime.now() + timedelta(hours=CREDIT_RESET_HOURS)).isoformat()
        save_users_data()
        
        # Уведомление пользователя
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Ваши кредиты были обновлены! У вас теперь {DEFAULT_CREDITS} кредитов."
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления о сбросе кредитов: {e}")

# Запуск задачи сброса кредитов
async def schedule_credit_reset(user_id, context):
    if str(user_id) in users_data:
        next_reset_time = datetime.fromisoformat(users_data[str(user_id)]["next_reset_time"])
        now = datetime.now()
        
        if now >= next_reset_time:
            # Если время уже прошло, сбрасываем сейчас
            await reset_credits(user_id, context)
        else:
            # Иначе планируем сброс на нужное время
            delay = (next_reset_time - now).total_seconds()
            
            if user_id in credit_reset_tasks:
                credit_reset_tasks[user_id].cancel()
            
            credit_reset_tasks[user_id] = asyncio.create_task(
                delayed_credit_reset(user_id, delay, context)
            )

# Отложенный сброс кредитов
async def delayed_credit_reset(user_id, delay, context):
    await asyncio.sleep(delay)
    await reset_credits(user_id, context)
    # После сброса планируем следующий
    await schedule_credit_reset(user_id, context)

# Обработка команды /start
async def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    username = update.effective_user.username
    
    init_user(user_id)
    init_user_memory(user_id)
    
    # Обновляем имя и username пользователя
    users_data[str(user_id)]["name"] = user_name
    users_data[str(user_id)]["username"] = username or ""
    save_users_data()
    
    # Запускаем планировщик сброса кредитов
    await schedule_credit_reset(user_id, context)
    
    welcome_message = (
        "🌟 *Добро пожаловать в бот Claude 3.7 Sonnet!* 🌟\n\n"
        "Я - ваш персональный ИИ-ассистент на базе Claude 3.7 Sonnet от Anthropic. "
        "Я могу отвечать на вопросы, анализировать изображения и файлы, помогать с текстами и многое другое.\n\n"
        "📱 *Основные функции:*\n"
        "• Ответы на любые вопросы\n"
        "• Анализ изображений и документов\n"
        "• Помощь в написании текстов\n"
        "• Работа с различными типами файлов\n"
        "• Последние 10 сообщений сохраняются в памяти\n\n"
        "📄 *Поддерживаемые форматы:*\n"
        "• Документы: PDF, DOCX, DOC, TXT и другие\n"
        "• Таблицы, презентации и изображения\n"
        "• Максимальный размер файла: 20 МБ\n\n"
    )
    
    # Информация о кредитах
    if users_data[str(user_id)].get("unlimited", False):
        welcome_message += "💰 *У вас безлимитный доступ!*\n\n"
    else:
        welcome_message += f"💰 *У вас {users_data[str(user_id)]['credits']} кредитов*\n"
        welcome_message += f"💎 Для приобретения дополнительных кредитов или безлимитного доступа свяжитесь с @{ADMIN_USERNAME}\n\n"
    
    welcome_message += (
        "📋 *Кнопки управления:*\n"
        "💰 Баланс - проверить баланс кредитов\n"
        "🔄 Сбросить историю - сбросить историю диалога\n"
        "ℹ️ Помощь - показать справку\n\n"
        "Просто начните общение, отправив сообщение, изображение или файл! 🚀"
    )
    
    # Отправляем сообщение с соответствующей клавиатурой
    if user_id == ADMIN_ID:
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN, reply_markup=get_user_keyboard())

# Обработка команды "💰 Баланс"
async def balance_command(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    init_user(int(user_id))
    
    if users_data[user_id].get("unlimited", False):
        balance_text = "У вас безлимитный доступ к боту! 🌟"
    else:
        credits = users_data[user_id]["credits"]
        next_reset = datetime.fromisoformat(users_data[user_id]["next_reset_time"])
        
        balance_text = (
            f"Ваш текущий баланс: {credits} кредитов.\n"
            f"Следующее обновление кредитов: {next_reset.strftime('%d.%m.%Y %H:%M')}"
        )
    
    await update.message.reply_text(balance_text)

# Обработка команды "🔄 Сбросить историю"
async def reset_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    init_user_memory(user_id)
    user_memory[user_id] = []
    
    await update.message.reply_text("История диалога очищена! 🧹")

# Обработка команды "ℹ️ Помощь"
async def help_command(update: Update, context: CallbackContext):
    help_text = (
        "🔍 *Справка по использованию бота:*\n\n"
        "📝 *Основные возможности:*\n"
        "• Задавайте любые вопросы\n"
        "• Отправляйте изображения для анализа\n"
        "• Получайте большие тексты разбитыми на удобные части\n"
        "• Отправляйте файлы различных форматов для анализа\n"
        "• Бот помнит последние 10 сообщений диалога\n\n"
        "📑 *Поддерживаемые типы файлов:*\n"
        "• 📄 Документы: PDF, DOCX, DOC, TXT, RTF, JSON, CSV, XML, HTML\n"
        "• 📊 Таблицы: XLSX, XLS\n"
        "• 📽 Презентации: PPTX, PPT\n"
        "• 🖼 Изображения: JPG, PNG, GIF, WebP, SVG\n"
        "• Максимальный размер файла: 20 МБ\n"
        "• Вы можете добавить комментарий к файлу в подписи\n\n"
        "⚠️ *Ограничения:*\n"
        "• Видео, аудио и голосовые сообщения не поддерживаются и будут удалены\n\n"
        "⌨️ *Доступные кнопки:*\n"
        "💰 Баланс - проверить количество оставшихся кредитов\n"
        "🔄 Сбросить историю - очистить историю диалога\n"
        "ℹ️ Помощь - показать это сообщение\n\n"
    )
    
    if users_data[str(update.effective_user.id)].get("unlimited", False):
        help_text += "💰 У вас безлимитный доступ к боту!\n"
    else:
        help_text += (
            "💡 Каждый запрос использует 1 кредит\n"
            f"💫 Для приобретения дополнительных кредитов или безлимитного доступа свяжитесь с @{ADMIN_USERNAME}\n"
            "💎 Стоимость кредитов и безлимитного доступа очень доступная!\n"
        )
    
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# Админские функции

# Обработка команды "📊 Список пользователей"
async def list_users_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    
    if not users_data:
        await update.message.reply_text("Список пользователей пуст.")
        return
    
    users_list = "📊 *Список пользователей:*\n\n"
    for uid, data in users_data.items():
        name = data.get("name", "Неизвестно")
        username = data.get("username", "")
        credits = data.get("credits", 0)
        unlimited = data.get("unlimited", False)
        
        user_info = f"👤 ID: `{uid}`\n"
        user_info += f"📝 Имя: {name}\n"
        if username:
            user_info += f"🔗 Username: @{username}\n"
        
        if unlimited:
            user_info += f"💰 Статус: Безлимитный доступ\n\n"
        else:
            user_info += f"💰 Кредиты: {credits}\n"
            next_reset = datetime.fromisoformat(data.get("next_reset_time", datetime.now().isoformat()))
            user_info += f"⏱ Сброс: {next_reset.strftime('%d.%m.%Y %H:%M')}\n\n"
        
        users_list += user_info
    
    # Разбиваем список на части если он длинный
    parts = split_text(users_list)
    for part in parts:
        await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN)

# Обработка команды "➕ Добавить кредиты"
async def add_credits_command(update: Update, context: CallbackContext, target_user_id=None, amount=None):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    
    if target_user_id is None or amount is None:
        # Сохраняем текущую команду
        context.user_data['last_admin_command'] = 'add_credits'
        await update.message.reply_text("Отправьте ID пользователя и количество кредитов в формате:\n`ID количество`", parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Количество должно быть положительным числом")
    except ValueError:
        await update.message.reply_text("Количество кредитов должно быть положительным числом.")
        return
    
    if str(target_user_id) not in users_data:
        init_user(int(target_user_id))
    
    # Если у пользователя безлимит, предупреждаем админа
    if users_data[str(target_user_id)].get("unlimited", False):
        await update.message.reply_text(f"Пользователь {target_user_id} имеет безлимитный доступ, кредиты не добавлены.")
        return
    
    users_data[str(target_user_id)]["credits"] += amount
    save_users_data()
    
    await update.message.reply_text(f"Добавлено {amount} кредитов пользователю {target_user_id}.")
    
    # Уведомление пользователя
    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text=f"Администратор добавил вам {amount} кредитов. Ваш текущий баланс: {users_data[str(target_user_id)]['credits']} кредитов."
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления о пополнении кредитов: {e}")

# Обработка команды "➖ Снять кредиты"
async def remove_credits_command(update: Update, context: CallbackContext, target_user_id=None, amount=None):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    
    if target_user_id is None or amount is None:
        # Сохраняем текущую команду
        context.user_data['last_admin_command'] = 'remove_credits'
        await update.message.reply_text("Отправьте ID пользователя и количество кредитов для снятия в формате:\n`ID количество`", parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        amount = int(amount)
        if amount <= 0:
            raise ValueError("Количество должно быть положительным числом")
    except ValueError:
        await update.message.reply_text("Количество кредитов должно быть положительным числом.")
        return
    
    if str(target_user_id) not in users_data:
        await update.message.reply_text(f"Пользователь {target_user_id} не найден.")
        return
    
    # Если у пользователя безлимит, предупреждаем админа
    if users_data[str(target_user_id)].get("unlimited", False):
        await update.message.reply_text(f"Пользователь {target_user_id} имеет безлимитный доступ, кредиты не сняты.")
        return
    
    prev_amount = users_data[str(target_user_id)]["credits"]
    users_data[str(target_user_id)]["credits"] = max(0, prev_amount - amount)
    save_users_data()
    
    actual_removed = prev_amount - users_data[str(target_user_id)]["credits"]
    
    await update.message.reply_text(f"Снято {actual_removed} кредитов у пользователя {target_user_id}.")
    
    # Уведомление пользователя
    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text=f"Администратор снял {actual_removed} кредитов с вашего счета. Ваш текущий баланс: {users_data[str(target_user_id)]['credits']} кредитов."
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления о снятии кредитов: {e}")

# Обработка команды "🌟 Дать безлимит"
async def set_unlimited_command(update: Update, context: CallbackContext, target_user_id=None):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    
    if target_user_id is None:
        # Сохраняем текущую команду
        context.user_data['last_admin_command'] = 'set_unlimited'
        await update.message.reply_text("Отправьте ID пользователя для предоставления безлимитного доступа в формате:\n`ID`", parse_mode=ParseMode.MARKDOWN)
        return
    
    if str(target_user_id) not in users_data:
        init_user(int(target_user_id))
    
    if users_data[str(target_user_id)].get("unlimited", False):
        await update.message.reply_text(f"Пользователь {target_user_id} уже имеет безлимитный доступ.")
        return
    
    users_data[str(target_user_id)]["unlimited"] = True
    save_users_data()
    
    await update.message.reply_text(f"Пользователю {target_user_id} установлен безлимитный доступ.")
    
    # Уведомление пользователя
    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text="🎉 Поздравляем! Администратор предоставил вам безлимитный доступ к боту! Теперь вы можете использовать бота без ограничений."
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления о безлимитном доступе: {e}")

# Обработка команды "⭐ Убрать безлимит"
async def unset_unlimited_command(update: Update, context: CallbackContext, target_user_id=None, new_amount=None):
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    
    if target_user_id is None or new_amount is None:
        # Сохраняем текущую команду
        context.user_data['last_admin_command'] = 'unset_unlimited'
        await update.message.reply_text("Отправьте ID пользователя и начальное количество кредитов в формате:\n`ID количество`", parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        new_amount = int(new_amount)
        if new_amount < 0:
            raise ValueError("Количество должно быть неотрицательным числом")
    except ValueError:
        await update.message.reply_text("Количество кредитов должно быть неотрицательным числом.")
        return
    
    if str(target_user_id) not in users_data:
        await update.message.reply_text(f"Пользователь {target_user_id} не найден.")
        return
    
    if not users_data[str(target_user_id)].get("unlimited", False):
        await update.message.reply_text(f"Пользователь {target_user_id} не имеет безлимитного доступа.")
        return
    
    users_data[str(target_user_id)]["unlimited"] = False
    users_data[str(target_user_id)]["credits"] = new_amount
    users_data[str(target_user_id)]["next_reset_time"] = (datetime.now() + timedelta(hours=CREDIT_RESET_HOURS)).isoformat()
    save_users_data()
    
    await update.message.reply_text(f"Безлимитный доступ у пользователя {target_user_id} отменен. Установлено {new_amount} кредитов.")
    
    # Уведомление пользователя
    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text=f"Ваш безлимитный доступ был отменен администратором. Вам начислено {new_amount} кредитов."
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления об отмене безлимитного доступа: {e}")

# Функции для обработки файлов

# Определение типа файла
def get_file_type(file_bytes):
    try:
        # Сначала пробуем определить тип с помощью filetype (более надежно работает в Windows)
        kind = filetype.guess(file_bytes)
        if kind is not None:
            mime_type = kind.mime
            logger.info(f"Тип файла определен через filetype: {mime_type}")
            
            # Проверяем, соответствует ли определенный тип поддерживаемым типам
            # PDF
            if mime_type == 'application/pdf' or 'pdf' in mime_type:
                return 'application/pdf'
            # MS Word (DOCX, DOC)
            elif 'officedocument.wordprocessingml.document' in mime_type or 'docx' in mime_type:
                return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            elif 'application/msword' in mime_type or 'doc' in mime_type:
                return 'application/msword'
            # MS Excel (XLSX, XLS)
            elif 'officedocument.spreadsheetml.sheet' in mime_type or 'xlsx' in mime_type:
                return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            elif 'application/vnd.ms-excel' in mime_type or 'xls' in mime_type:
                return 'application/vnd.ms-excel'
            # MS Powerpoint (PPTX, PPT)
            elif 'officedocument.presentationml.presentation' in mime_type or 'pptx' in mime_type:
                return 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
            elif 'application/vnd.ms-powerpoint' in mime_type or 'ppt' in mime_type:
                return 'application/vnd.ms-powerpoint'
            # Текстовые форматы
            elif 'text/plain' in mime_type or 'txt' in mime_type:
                return 'text/plain'
            elif 'text/csv' in mime_type or 'csv' in mime_type:
                return 'text/csv'
            elif 'application/json' in mime_type or 'json' in mime_type:
                return 'application/json'
            elif 'text/markdown' in mime_type or 'md' in mime_type:
                return 'text/markdown'
            elif 'text/html' in mime_type or 'html' in mime_type:
                return 'text/html'
            elif 'application/xml' in mime_type or 'xml' in mime_type:
                return 'application/xml'
            elif 'application/rtf' in mime_type or 'rtf' in mime_type:
                return 'application/rtf'
            # Изображения
            elif 'image/jpeg' in mime_type or 'jpg' in mime_type or 'jpeg' in mime_type:
                return 'image/jpeg'
            elif 'image/png' in mime_type or 'png' in mime_type:
                return 'image/png'
            elif 'image/gif' in mime_type or 'gif' in mime_type:
                return 'image/gif'
            elif 'image/webp' in mime_type or 'webp' in mime_type:
                return 'image/webp'
            elif 'image/svg+xml' in mime_type or 'svg' in mime_type:
                return 'image/svg+xml'
        
        # Если filetype не смог определить или тип не поддерживается, пробуем python-magic
        try:
            mime = magic.Magic(mime=True)
            # Сначала сохраняем bytearray во временный файл, чтобы избежать проблем с указателями в Windows
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(file_bytes)
                temp_file_path = temp_file.name
            
            # Затем определяем MIME-тип из файла
            file_type = mime.from_file(temp_file_path)
            
            # Удаляем временный файл
            os.unlink(temp_file_path)
            
            logger.info(f"Тип файла определен через magic: {file_type}")
            
            # Если тип файла в списке поддерживаемых, возвращаем его
            if file_type in SUPPORTED_FILE_TYPES:
                return file_type
                
            # Базовая проверка для основных групп типов
            if 'text/' in file_type:
                return 'text/plain'
            elif 'image/' in file_type:
                # Для изображений пытаемся определить тип изображения или возвращаем JPEG как дефолт
                if 'jpeg' in file_type or 'jpg' in file_type:
                    return 'image/jpeg'
                elif 'png' in file_type:
                    return 'image/png'
                elif 'gif' in file_type:
                    return 'image/gif'
                elif 'webp' in file_type:
                    return 'image/webp'
                elif 'svg' in file_type:
                    return 'image/svg+xml'
                else:
                    return 'image/jpeg'  # Дефолтный тип для изображений
            elif 'application/pdf' in file_type:
                return 'application/pdf'
            elif 'wordprocessingml' in file_type or 'docx' in file_type:
                return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            elif 'msword' in file_type:
                return 'application/msword'
        except Exception as e:
            logger.error(f"Ошибка при определении типа файла с magic: {e}")
        
        # Если и magic не сработал, определяем по сигнатуре файла вручную или расширению
        if file_bytes[:4] == b'%PDF':
            return 'application/pdf'
        elif file_bytes[:2] == b'PK':
            # DOCX и другие Office-файлы используют формат ZIP (начинаются с 'PK')
            return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        elif file_bytes[:5] == b'<?xml' or file_bytes[:5] == b'<html' or file_bytes[:9] == b'<!DOCTYPE':
            # XML, HTML и подобные форматы
            return 'text/html'
        
        # По умолчанию считаем текстовым файлом
        try:
            # Проверяем, можно ли декодировать как текст
            file_bytes.decode('utf-8')
            return 'text/plain'
        except:
            try:
                file_bytes.decode('latin-1')
                return 'text/plain'
            except:
                # Если не можем определить тип, возвращаем бинарный поток,
                # который не в списке поддерживаемых типов и будет отклонен
                return 'application/octet-stream'
    except Exception as e:
        # Если все методы определения типа файла не сработали, выбрасываем исключение
        logger.error(f"Все методы определения типа файла не сработали: {e}")
        raise ValueError(f"Не удалось определить тип файла: {e}")

# Извлечение текста из PDF
def extract_text_from_pdf(file_bytes):
    try:
        with io.BytesIO(file_bytes) as pdf_file:
            reader = PyPDF2.PdfReader(pdf_file)
            text = ""
            for page_num in range(len(reader.pages)):
                page = reader.pages[page_num]
                text += page.extract_text() + "\n"
            return text
    except Exception as e:
        logger.error(f"Ошибка при извлечении текста из PDF: {e}")
        return f"Не удалось извлечь текст из PDF файла: {str(e)}"

# Извлечение текста из DOCX
def extract_text_from_docx(file_bytes):
    try:
        with io.BytesIO(file_bytes) as docx_file:
            doc = Document(docx_file)
            text = ""
            for para in doc.paragraphs:
                text += para.text + "\n"
            return text
    except Exception as e:
        logger.error(f"Ошибка при извлечении текста из DOCX: {e}")
        return f"Не удалось извлечь текст из DOCX файла: {str(e)}"

# Извлечение текста из TXT
def extract_text_from_txt(file_bytes):
    try:
        # Попытка декодировать с разными кодировками
        encodings = ['utf-8', 'cp1251', 'latin-1']
        for encoding in encodings:
            try:
                return file_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise UnicodeDecodeError("Не удалось определить кодировку файла")
    except Exception as e:
        logger.error(f"Ошибка при извлечении текста из TXT: {e}")
        return f"Не удалось прочитать текстовый файл: {str(e)}"

# Основная функция для извлечения текста из файла
def extract_text_from_file(file_bytes, file_type):
    if file_type == 'application/pdf':
        return extract_text_from_pdf(file_bytes)
    elif file_type in ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/msword']:
        return extract_text_from_docx(file_bytes)
    elif file_type in ['text/plain', 'text/csv', 'application/json', 'text/markdown', 'text/html', 'application/xml', 'application/rtf']:
        return extract_text_from_txt(file_bytes)
    elif file_type in ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.ms-excel']:
        return "[Таблица Excel] Файл успешно загружен. Задайте вопрос о его содержимом."
    elif file_type in ['application/vnd.openxmlformats-officedocument.presentationml.presentation', 'application/vnd.ms-powerpoint']:
        return "[Презентация PowerPoint] Файл успешно загружен. Задайте вопрос о его содержимом."
    elif file_type.startswith('image/'):
        return "[Изображение] Файл успешно загружен. Задайте вопрос о его содержимом."
    else:
        return f"Неподдерживаемый тип файла: {file_type}"

# Ограничение размера текста для API
def limit_text(text, max_length=15000):
    if len(text) <= max_length:
        return text
    
    return text[:max_length] + f"\n\n... [Текст обрезан, превышен максимальный размер. Показано {max_length} из {len(text)} символов]"

# Обработка файлов
async def handle_document(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    init_user(user_id)
    init_user_memory(user_id)
    file_id = update.message.document.file_id
    
    # Проверка на наличие кредитов, пропускаем если безлимит
    if not users_data[str(user_id)].get("unlimited", False) and users_data[str(user_id)]["credits"] <= 0:
        keyboard = [[InlineKeyboardButton("Связаться с администратором", url=f"https://t.me/{ADMIN_USERNAME}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "У вас закончились кредиты! Свяжитесь с администратором @" + ADMIN_USERNAME + 
            " для пополнения баланса. Вы можете приобрести дополнительные кредиты или полный безлимитный доступ за небольшую сумму.",
            reply_markup=reply_markup
        )
        return
    
    # Получаем информацию о файле
    file_name = update.message.document.file_name or "документ"
    file_size = update.message.document.file_size
    
    # Проверка размера файла
    if file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"Файл слишком большой! Максимальный размер - {MAX_FILE_SIZE // (1024 * 1024)} МБ."
        )
        return
    
    # Проверяем, не обрабатывается ли уже файл для этого пользователя
    file_key = f"{user_id}_{file_id}"
    if file_key in processing_files:
        await update.message.reply_text("Этот файл уже обрабатывается, пожалуйста, подождите.")
        return
    
    # Отмечаем файл как обрабатываемый
    processing_files.add(file_key)
    status_message = None
    
    try:
        # Информируем пользователя о начале обработки
        status_message = await update.message.reply_text(f"⏳ Начинаю обработку файла '{file_name}'...")
        
        # Скачиваем файл
        try:
            file = await context.bot.get_file(file_id)
            file_bytes = await file.download_as_bytearray()
        except Exception as e:
            logger.error(f"Ошибка при скачивании файла: {e}")
            await status_message.edit_text(f"❌ Не удалось скачать файл: {str(e)}")
            processing_files.remove(file_key)
            return
        
        # Определяем тип файла
        file_type = None
        try:
            file_type = get_file_type(file_bytes)
            logger.info(f"Определен тип файла: {file_type} для файла {file_name}")
        except Exception as e:
            logger.error(f"Ошибка при определении типа файла программно: {e}")
            # Попробуем определить тип файла по расширению
            if file_name:
                ext = os.path.splitext(file_name)[1].lower()
                if ext == '.pdf':
                    file_type = 'application/pdf'
                    logger.info(f"Тип файла определен по расширению: {file_type}")
                elif ext in ['.docx', '.doc']:
                    file_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                    logger.info(f"Тип файла определен по расширению: {file_type}")
                elif ext in ['.xlsx', '.xls']:
                    file_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                    logger.info(f"Тип файла определен по расширению: {file_type}")
                elif ext in ['.pptx', '.ppt']:
                    file_type = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
                    logger.info(f"Тип файла определен по расширению: {file_type}")
                elif ext in ['.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm', '.rtf']:
                    file_type = 'text/plain'
                    logger.info(f"Тип файла определен по расширению: {file_type}")
                elif ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']:
                    if ext in ['.jpg', '.jpeg']:
                        file_type = 'image/jpeg'
                    elif ext == '.png':
                        file_type = 'image/png'
                    elif ext == '.gif':
                        file_type = 'image/gif'
                    elif ext == '.webp':
                        file_type = 'image/webp'
                    elif ext == '.svg':
                        file_type = 'image/svg+xml'
                    logger.info(f"Тип файла определен по расширению: {file_type}")
            
            if not file_type:
                await status_message.edit_text(f"❌ Не удалось определить тип файла.")
                processing_files.remove(file_key)
                return
        
        # Обрабатываем изображения отдельно
        if file_type.startswith('image/'):
            # Для изображений используем base64 кодирование
            image_base64 = base64.b64encode(file_bytes).decode('utf-8')
            
            # Создаем сообщение с изображением
            caption = update.message.caption or "Опишите этот файл изображения"
            
            # Определяем MIME-тип для изображения
            image_mime = file_type
            
            message_with_image = {
                "role": "user",
                "content": [
                    {"type": "text", "text": caption},
                    {"type": "image", "source": {"type": "base64", "media_type": image_mime, "data": image_base64}}
                ]
            }
            
            # Получаем историю диалога без изображений (чтобы не перегружать API)
            text_history = []
            for msg in user_memory[user_id]:
                if isinstance(msg["content"], str):
                    text_history.append({"role": msg["role"], "content": msg["content"]})
            
            # Добавляем текущее сообщение с изображением
            messages_to_send = text_history + [message_with_image]
            
            # Сообщаем пользователю
            await status_message.edit_text(f"🔍 Анализирую изображение '{file_name}'...")
            
            # Определяем, если это запрос на большой текст
            is_long_content_request = any(keyword in caption.lower() for keyword in 
                                    ["реферат", "эссе", "сочинение", "статья", "доклад", "текст на", 
                                    "напиши большой", "3000 слов", "2000 слов", "много текста",
                                    "развернутый ответ", "подробно опиши", "подробный анализ"])
            
            # Отправка запроса к Anthropic
            try:
                response = await query_anthropic(messages_to_send)
            except Exception as e:
                logger.error(f"Ошибка при запросе к API Anthropic: {e}")
                await status_message.edit_text(f"❌ Ошибка при отправке запроса к Claude: {str(e)}")
                processing_files.remove(file_key)
                return
            
            if "error" in response:
                await status_message.edit_text(f"❌ Произошла ошибка: {response['error']}")
                processing_files.remove(file_key)
                return
            
            # Обработка ответа от API
            if "content" in response and len(response["content"]) > 0:
                assistant_response = response["content"][0]["text"]
                
                # Добавление запроса пользователя и ответа помощника в память
                user_memory[user_id].append({"role": "user", "content": caption + " [изображение]"})
                user_memory[user_id].append({"role": "assistant", "content": assistant_response})
                
                # Ограничение памяти
                if len(user_memory[user_id]) > MAX_MEMORY_MESSAGES:
                    user_memory[user_id] = user_memory[user_id][-MAX_MEMORY_MESSAGES:]
                
                # Списание кредита, только если не безлимит
                if not users_data[str(user_id)].get("unlimited", False):
                    users_data[str(user_id)]["credits"] -= 1
                    save_users_data()
                
                # Если кредиты закончились после этого запроса, уведомляем пользователя
                if users_data[str(user_id)]["credits"] == 0:
                    await update.message.reply_text(
                        "⚠️ Это был ваш последний кредит! Для продолжения использования бота свяжитесь с "
                        f"@{ADMIN_USERNAME} для приобретения дополнительных кредитов или безлимитного доступа."
                    )
                
                # Удаляем статусное сообщение
                try:
                    await status_message.delete()
                except Exception:
                    pass
                
                # Получаем соответствующую клавиатуру для пользователя
                reply_markup = get_admin_keyboard() if user_id == ADMIN_ID else get_user_keyboard()
                
                # Отправка ответа пользователю
                if is_long_content_request or len(assistant_response) > MAX_MESSAGE_LENGTH:
                    await send_long_message(update, assistant_response, reply_markup)
                else:
                    await update.message.reply_text(assistant_response, reply_markup=reply_markup)
            else:
                await status_message.edit_text("❌ Не удалось получить ответ от Claude. Попробуйте еще раз.")
        else:
            # Для не-изображений продолжаем обычную обработку файла
            # Обновляем статус
            await status_message.edit_text(f"🔍 Извлекаю информацию из файла '{file_name}'...")
            
            # Извлекаем текст из файла
            try:
                extracted_text = extract_text_from_file(file_bytes, file_type)
            except Exception as e:
                logger.error(f"Ошибка при извлечении текста из файла: {e}")
                await status_message.edit_text(f"❌ Ошибка при извлечении текста: {str(e)}")
                processing_files.remove(file_key)
                return
            
            # Если текст не удалось извлечь
            if not extracted_text or extracted_text.startswith("Не удалось"):
                await status_message.edit_text(f"❌ {extracted_text}")
                processing_files.remove(file_key)
                return
            
            # Ограничиваем размер текста, чтобы не перегружать API
            limited_text = limit_text(extracted_text)
            
            # Подготавливаем запрос к Claude
            query = update.message.caption or f"Проанализируй этот {SUPPORTED_FILE_TYPES.get(file_type, 'файл')}: {file_name}"
            
            # Сообщаем пользователю
            await status_message.edit_text(f"💭 Отправляю запрос к Claude с данными из файла ({len(limited_text)} символов)...")
            
            # Добавляем сообщение пользователя в память
            file_message = f"{query}\n\nСодержимое файла:\n\n{limited_text}"
            user_memory[user_id].append({"role": "user", "content": file_message})
            
            # Ограничение памяти до последних MAX_MEMORY_MESSAGES сообщений
            if len(user_memory[user_id]) > MAX_MEMORY_MESSAGES:
                user_memory[user_id] = user_memory[user_id][-MAX_MEMORY_MESSAGES:]
            
            # Определяем, если это запрос на большой текст
            is_long_content_request = len(limited_text) > 5000 or any(keyword in query.lower() for keyword in 
                                    ["реферат", "эссе", "сочинение", "статья", "доклад", "текст на", 
                                    "напиши большой", "3000 слов", "2000 слов", "много текста",
                                    "развернутый ответ", "подробно опиши", "подробный анализ"])
            
            # Отправка запроса к Anthropic
            try:
                response = await query_anthropic(user_memory[user_id])
            except Exception as e:
                logger.error(f"Ошибка при запросе к API Anthropic: {e}")
                await status_message.edit_text(f"❌ Ошибка при отправке запроса к Claude: {str(e)}")
                processing_files.remove(file_key)
                return
            
            if "error" in response:
                await status_message.edit_text(f"❌ Произошла ошибка: {response['error']}")
                processing_files.remove(file_key)
                return
            
            # Обработка ответа от API
            if "content" in response and len(response["content"]) > 0:
                assistant_response = response["content"][0]["text"]
                
                # Добавление ответа помощника в память
                user_memory[user_id].append({"role": "assistant", "content": assistant_response})
                
                # Списание кредита, только если не безлимит
                if not users_data[str(user_id)].get("unlimited", False):
                    users_data[str(user_id)]["credits"] -= 1
                    save_users_data()
                
                # Если кредиты закончились после этого запроса, уведомляем пользователя
                if users_data[str(user_id)]["credits"] == 0:
                    await update.message.reply_text(
                        "⚠️ Это был ваш последний кредит! Для продолжения использования бота свяжитесь с "
                        f"@{ADMIN_USERNAME} для приобретения дополнительных кредитов или безлимитного доступа."
                    )
                
                # Удаляем статусное сообщение
                try:
                    await status_message.delete()
                except Exception:
                    pass  # Игнорируем ошибки при удалении сообщения
                
                # Получаем соответствующую клавиатуру для пользователя
                reply_markup = get_admin_keyboard() if user_id == ADMIN_ID else get_user_keyboard()
                
                # Отправка ответа пользователю: разбиваем на части если это большой ответ
                if is_long_content_request or len(assistant_response) > MAX_MESSAGE_LENGTH:
                    await send_long_message(update, assistant_response, reply_markup)
                else:
                    await update.message.reply_text(assistant_response, reply_markup=reply_markup)
            else:
                await status_message.edit_text("❌ Не удалось получить ответ от Claude. Попробуйте еще раз.")
    except Exception as e:
        logger.error(f"Ошибка при обработке файла: {e}")
        error_message = f"❌ Произошла ошибка при обработке файла: {str(e)}"
        if status_message:
            try:
                await status_message.edit_text(error_message)
            except Exception:
                await update.message.reply_text(error_message)
        else:
            await update.message.reply_text(error_message)
    finally:
        # Удаляем файл из списка обрабатываемых в любом случае
        if file_key in processing_files:
            processing_files.remove(file_key)

# Обработка текстовых сообщений
async def handle_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    init_user(user_id)
    init_user_memory(user_id)
    
    user_message = update.message.text
    
    # Обработка команд клавиатуры
    if user_message in USER_KEYBOARD_COMMANDS or user_message in ADMIN_KEYBOARD_COMMANDS:
        if user_message == "💰 Баланс" or user_message == "💰 Мой баланс":
            await balance_command(update, context)
            return
        elif user_message == "🔄 Сбросить историю":
            await reset_command(update, context)
            return
        elif user_message == "ℹ️ Помощь":
            await help_command(update, context)
            return
        
        # Команды только для администратора
        if user_id == ADMIN_ID:
            if user_message == "📊 Список пользователей":
                await list_users_command(update, context)
                return
            elif user_message == "➕ Добавить кредиты":
                context.user_data['last_admin_command'] = 'add_credits'
                await update.message.reply_text("Отправьте ID пользователя и количество кредитов в формате:\n`ID количество`", parse_mode=ParseMode.MARKDOWN)
                return
            elif user_message == "➖ Снять кредиты":
                context.user_data['last_admin_command'] = 'remove_credits'
                await update.message.reply_text("Отправьте ID пользователя и количество кредитов для снятия в формате:\n`ID количество`", parse_mode=ParseMode.MARKDOWN)
                return
            elif user_message == "🌟 Дать безлимит":
                context.user_data['last_admin_command'] = 'set_unlimited'
                await update.message.reply_text("Отправьте ID пользователя для предоставления безлимитного доступа в формате:\n`ID`", parse_mode=ParseMode.MARKDOWN)
                return
            elif user_message == "⭐ Убрать безлимит":
                context.user_data['last_admin_command'] = 'unset_unlimited'
                await update.message.reply_text("Отправьте ID пользователя и начальное количество кредитов в формате:\n`ID количество`", parse_mode=ParseMode.MARKDOWN)
                return
        else:
            if user_message in ADMIN_KEYBOARD_COMMANDS and user_message not in USER_KEYBOARD_COMMANDS:
                await update.message.reply_text("У вас нет прав для выполнения этой команды.")
                return
    
    # Проверка на формат ввода для админских команд
    if user_id == ADMIN_ID:
        parts = user_message.strip().split()
        if len(parts) == 2 and parts[0].isdigit():
            # Это может быть команда добавления/снятия кредитов или отмены безлимита
            target_user_id = parts[0]
            try:
                amount = int(parts[1])
                # Определяем последнюю команду
                last_command = context.user_data.get('last_admin_command')
                if last_command == "add_credits":
                    await add_credits_command(update, context, target_user_id, amount)
                    context.user_data['last_admin_command'] = None
                    return
                elif last_command == "remove_credits":
                    await remove_credits_command(update, context, target_user_id, amount)
                    context.user_data['last_admin_command'] = None
                    return
                elif last_command == "unset_unlimited":
                    await unset_unlimited_command(update, context, target_user_id, amount)
                    context.user_data['last_admin_command'] = None
                    return
            except ValueError:
                pass
        elif len(parts) == 1 and parts[0].isdigit():
            # Это может быть команда установки безлимита
            target_user_id = parts[0]
            last_command = context.user_data.get('last_admin_command')
            if last_command == "set_unlimited":
                await set_unlimited_command(update, context, target_user_id)
                context.user_data['last_admin_command'] = None
                return
    
    # Проверка на наличие кредитов, пропускаем если безлимит
    if not users_data[str(user_id)].get("unlimited", False) and users_data[str(user_id)]["credits"] <= 0:
        keyboard = [[InlineKeyboardButton("Связаться с администратором", url=f"https://t.me/{ADMIN_USERNAME}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "У вас закончились кредиты! Свяжитесь с администратором @" + ADMIN_USERNAME + 
            " для пополнения баланса. Вы можете приобрести дополнительные кредиты или полный безлимитный доступ за небольшую сумму.",
            reply_markup=reply_markup
        )
        return
    
    # Добавление сообщения пользователя в память
    user_memory[user_id].append({"role": "user", "content": user_message})
    
    # Ограничение памяти до последних MAX_MEMORY_MESSAGES сообщений
    if len(user_memory[user_id]) > MAX_MEMORY_MESSAGES:
        user_memory[user_id] = user_memory[user_id][-MAX_MEMORY_MESSAGES:]
    
    # Определяем, если это запрос на большой текст (реферат, эссе и т.д.)
    is_long_content_request = any(keyword in user_message.lower() for keyword in 
                               ["реферат", "эссе", "сочинение", "статья", "доклад", "текст на", 
                                "напиши большой", "3000 слов", "2000 слов", "много текста",
                                "развернутый ответ", "подробно опиши", "подробный анализ"])
    
    # Отправляем статус набора текста
    await update.message.chat.send_action("typing")
    
    # Отправка запроса к Anthropic
    response = await query_anthropic(user_memory[user_id])
    
    if "error" in response:
        await update.message.reply_text(f"Произошла ошибка: {response['error']}")
        return
    
    # Обработка ответа от API
    if "content" in response and len(response["content"]) > 0:
        assistant_response = response["content"][0]["text"]
        
        # Добавление ответа помощника в память
        user_memory[user_id].append({"role": "assistant", "content": assistant_response})
        
        # Списание кредита, только если не безлимит
        if not users_data[str(user_id)].get("unlimited", False):
            users_data[str(user_id)]["credits"] -= 1
            save_users_data()
            
            # Если кредиты закончились после этого запроса, уведомляем пользователя
            if users_data[str(user_id)]["credits"] == 0:
                await update.message.reply_text(
                    "⚠️ Это был ваш последний кредит! Для продолжения использования бота свяжитесь с "
                    f"@{ADMIN_USERNAME} для приобретения дополнительных кредитов или безлимитного доступа."
                )
        
        # Получаем соответствующую клавиатуру для пользователя
        reply_markup = get_admin_keyboard() if user_id == ADMIN_ID else get_user_keyboard()
        
        # Отправка ответа пользователю: разбиваем на части если это большой ответ
        if is_long_content_request or len(assistant_response) > MAX_MESSAGE_LENGTH:
            await send_long_message(update, assistant_response, reply_markup)
        else:
            await update.message.reply_text(assistant_response, reply_markup=reply_markup)
    else:
        # В случае ошибки тоже показываем клавиатуру
        reply_markup = get_admin_keyboard() if user_id == ADMIN_ID else get_user_keyboard()
        await update.message.reply_text("Не удалось получить ответ от Claude. Попробуйте еще раз.", reply_markup=reply_markup)

# Обработка фотографий
async def handle_photo(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    init_user(user_id)
    init_user_memory(user_id)
    
    # Проверка на наличие кредитов, пропускаем если безлимит
    if not users_data[str(user_id)].get("unlimited", False) and users_data[str(user_id)]["credits"] <= 0:
        keyboard = [[InlineKeyboardButton("Связаться с администратором", url=f"https://t.me/{ADMIN_USERNAME}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "У вас закончились кредиты! Свяжитесь с администратором @" + ADMIN_USERNAME + 
            " для пополнения баланса. Вы можете приобрести дополнительные кредиты или полный безлимитный доступ за небольшую сумму.",
            reply_markup=reply_markup
        )
        return
    
    # Получение фотографии с наилучшим качеством
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    
    # Преобразование изображения в base64
    image_base64 = base64.b64encode(photo_bytes).decode('utf-8')
    
    # Создаем сообщение с изображением
    caption = update.message.caption or "Опишите эту фотографию"
    
    # Определяем, если это запрос на большой текст (реферат, эссе и т.д.)
    is_long_content_request = any(keyword in caption.lower() for keyword in 
                               ["реферат", "эссе", "сочинение", "статья", "доклад", "текст на", 
                                "напиши большой", "3000 слов", "2000 слов", "много текста",
                                "развернутый ответ", "подробно опиши", "подробный анализ"])
    
    message_with_image = {
        "role": "user",
        "content": [
            {"type": "text", "text": caption},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_base64}}
        ]
    }
    
    # Получаем историю диалога без изображений (чтобы не перегружать API)
    text_history = []
    for msg in user_memory[user_id]:
        if isinstance(msg["content"], str):
            text_history.append({"role": msg["role"], "content": msg["content"]})
        # Пропускаем сообщения с изображениями в истории
    
    # Добавляем текущее сообщение с изображением
    messages_to_send = text_history + [message_with_image]
    
    # Отправка запроса к Anthropic
    await update.message.chat.send_action("typing")
    response = await query_anthropic(messages_to_send)
    
    if "error" in response:
        await update.message.reply_text(f"Произошла ошибка: {response['error']}")
        return
    
    # Обработка ответа от API
    if "content" in response and len(response["content"]) > 0:
        assistant_response = response["content"][0]["text"]
        
        # Добавление запроса пользователя (только текст) и ответа помощника в память
        user_memory[user_id].append({"role": "user", "content": caption + " [фотография]"})
        user_memory[user_id].append({"role": "assistant", "content": assistant_response})
        
        # Ограничение памяти до последних MAX_MEMORY_MESSAGES сообщений
        if len(user_memory[user_id]) > MAX_MEMORY_MESSAGES:
            user_memory[user_id] = user_memory[user_id][-MAX_MEMORY_MESSAGES:]
        
        # Списание кредита, только если не безлимит
        if not users_data[str(user_id)].get("unlimited", False):
            users_data[str(user_id)]["credits"] -= 1
            save_users_data()
            
            # Если кредиты закончились после этого запроса, уведомляем пользователя
            if users_data[str(user_id)]["credits"] == 0:
                await update.message.reply_text(
                    "⚠️ Это был ваш последний кредит! Для продолжения использования бота свяжитесь с "
                    f"@{ADMIN_USERNAME} для приобретения дополнительных кредитов или безлимитного доступа."
                )
        
        # Получаем соответствующую клавиатуру для пользователя
        reply_markup = get_admin_keyboard() if user_id == ADMIN_ID else get_user_keyboard()
        
        # Отправка ответа пользователю: разбиваем на части если это большой ответ
        if is_long_content_request or len(assistant_response) > MAX_MESSAGE_LENGTH:
            await send_long_message(update, assistant_response, reply_markup)
        else:
            await update.message.reply_text(assistant_response, reply_markup=reply_markup)
    else:
        # В случае ошибки тоже показываем клавиатуру
        reply_markup = get_admin_keyboard() if user_id == ADMIN_ID else get_user_keyboard()
        await update.message.reply_text("Не удалось получить ответ от Claude. Попробуйте еще раз.", reply_markup=reply_markup)

# Удаление нежелательных типов сообщений
async def delete_unsupported_message(update: Update, context: CallbackContext):
    await update.message.delete()

def main():
    # Загрузка данных пользователей
    load_users_data()
    
    # Создание приложения без функции post_init
    builder = Application.builder()
    builder.token(TELEGRAM_BOT_TOKEN)
    # Отключаем job_queue для предотвращения ошибки weak reference
    builder.job_queue(None)
    application = builder.build()
    
    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start))
    
    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Обработчик документов (файлов)
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Обработчик фото
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Удаление аудио, видео и голосовых сообщений
    application.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.AUDIO | filters.VIDEO_NOTE, 
        delete_unsupported_message
    ))
    
    # Все остальные типы сообщений обрабатываются основной функцией handle_message
    application.add_handler(MessageHandler(
        ~filters.TEXT & ~filters.PHOTO & ~filters.COMMAND & ~filters.Document.ALL & 
        ~filters.VOICE & ~filters.VIDEO & ~filters.AUDIO & ~filters.VIDEO_NOTE, 
        handle_message
    ))
    
    # Настраиваем веб-хук для работы на хостингах
    PORT = int(os.environ.get('PORT', '8443'))
    
    # Определяем метод запуска в зависимости от наличия переменной окружения
    if os.environ.get('USE_WEBHOOK', 'False').lower() == 'true':
        # Для запуска на хостинге с веб-хуком
        APP_URL = os.environ.get('APP_URL', '')
        if APP_URL:
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TELEGRAM_BOT_TOKEN,
                webhook_url=f"{APP_URL}/{TELEGRAM_BOT_TOKEN}"
            )
            print(f"Бот запущен в режиме webhook на порту {PORT}!")
        else:
            print("Ошибка: не указан APP_URL для webhook режима")
            application.run_polling()
    else:
        # Для локального запуска с polling
        print("Бот запущен в режиме polling! Нажмите Ctrl+C для остановки.")
        application.run_polling()

if __name__ == "__main__":
    main() 