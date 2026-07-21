import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import sqlite3
import httpx
from openai import OpenAI
import json
import re
import os
import requests
import tempfile
import pytz
import timezonefinder

# === TAVILY ДЛЯ ПОИСКА В ИНТЕРНЕТЕ ===
try:
    from tavily import TavilyClient
except ImportError:
    print("⚠️ Tavily не установлен. Установи: pip install tavily-python")
    TavilyClient = None

# === КОНФИГ ===
DB_NAME = "aura.db"

# === КЛЮЧИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЙ ===
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-133c0d2bfc664d878ac8dcbc346ea3fc")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8774637081:AAGrAZI-umgkQXXulCulJVRWb8LmAp3Lua4")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# === ИНИЦИАЛИЗАЦИЯ TAVILY ===
tavily_client = None
if TavilyClient and TAVILY_API_KEY:
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        print("✅ Tavily инициализирован")
    except Exception as e:
        print(f"❌ Ошибка Tavily: {e}")

# === ОПРЕДЕЛЕНИЕ ЧАСОВОГО ПОЯСА ПО ГОРОДУ ===
tf = timezonefinder.TimezoneFinder()

def get_timezone_for_city(city_name):
    """Определяет часовой пояс по названию города"""
    city_lower = city_name.lower().strip()
    
    # База городов и их примерных координат
    cities = {
        "москва": (55.7558, 37.6173),
        "санкт-петербург": (59.9343, 30.3351),
        "новосибирск": (55.0084, 82.9357),
        "кемерово": (55.3333, 86.0833),
        "белово": (54.4167, 86.3000),
        "екатеринбург": (56.8389, 60.6057),
        "казань": (55.7887, 49.1221),
        "нижний новгород": (56.2965, 43.9361),
        "челябинск": (55.1644, 61.4368),
        "омск": (54.9885, 73.3242),
        "краснодар": (45.0355, 38.9753),
        "владивосток": (43.1155, 131.8855),
        "иркутск": (52.2855, 104.2891),
        "хабаровск": (48.4802, 135.0719),
        "ростов-на-дону": (47.2357, 39.7015),
        "самара": (53.1959, 50.1000),
        "уфа": (54.7388, 55.9721),
        "красноярск": (56.0184, 92.8672),
        "пермь": (58.0104, 56.2294),
        "волгоград": (48.7071, 44.5169),
        "сочи": (43.5855, 39.7231),
        "калининград": (54.7104, 20.4522),
        "мурманск": (68.9585, 33.0827),
        "архангельск": (64.5399, 40.5158),
        "тюмень": (57.1530, 65.5342),
        "барнаул": (53.3561, 83.7697),
        "ижевск": (56.8606, 53.2092),
        "ульяновск": (54.3178, 48.3807),
        "ярославль": (57.6261, 39.8845),
        "рязань": (54.6293, 39.7359),
        "пенза": (53.1959, 45.0185),
        "липецк": (52.6031, 39.5708),
        "тула": (54.1931, 37.6173),
        "киров": (58.6033, 49.6673),
        "чебоксары": (56.1277, 47.2523),
        "калуга": (54.5138, 36.2612),
        "владимир": (56.1281, 40.4070),
        "тверь": (56.8587, 35.9178),
        "смоленск": (54.7800, 32.0600),
        "курск": (51.7300, 36.1900),
        "орёл": (52.9700, 36.0700),
        "белгород": (50.6100, 36.5800),
        "воронеж": (51.6600, 39.2000),
        "саратов": (51.5300, 46.0300),
        "тольятти": (53.5100, 49.4200),
        "астрахань": (46.3500, 48.0400),
        "ставрополь": (45.0400, 41.9700),
        "грозный": (43.3100, 45.6900),
        "махачкала": (42.9800, 47.5000),
        "симферополь": (44.9500, 34.1000),
        "севастополь": (44.6000, 33.5300),
        "петрозаводск": (61.7800, 34.3300),
        "сыктывкар": (61.6700, 50.8100),
        "йошкар-ола": (56.6300, 47.8900),
        "саранск": (54.1800, 45.1700),
        "кудымкар": (59.0100, 54.6700),
    }
    
    # Проверяем, есть ли город в базе
    for city, coords in cities.items():
        if city in city_lower:
            try:
                tz_str = tf.timezone_at(lat=coords[0], lng=coords[1])
                if tz_str:
                    return pytz.timezone(tz_str)
            except:
                pass
    
    # Если город не найден — возвращаем UTC+3 (Москва)
    return pytz.timezone('Europe/Moscow')

def get_current_time_for_user(user_id, city_name=None):
    """Возвращает текущее время для пользователя"""
    # Если город указан — определяем его часовой пояс
    if city_name:
        tz = get_timezone_for_city(city_name)
        now = datetime.now(tz)
        return now.strftime("%H:%M"), tz
    
    # Если город не указан — пытаемся получить из памяти пользователя
    user_tz_str = get_memory(user_id, "timezone")
    if user_tz_str:
        try:
            tz = pytz.timezone(user_tz_str)
            now = datetime.now(tz)
            return now.strftime("%H:%M"), tz
        except:
            pass
    
    # По умолчанию — Москва (UTC+3)
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)
    return now.strftime("%H:%M"), tz

def get_current_date_for_user(user_id, city_name=None):
    """Возвращает текущую дату для пользователя"""
    if city_name:
        tz = get_timezone_for_city(city_name)
        now = datetime.now(tz)
        return now.strftime("%d.%m.%Y"), now.strftime("%A")
    
    user_tz_str = get_memory(user_id, "timezone")
    if user_tz_str:
        try:
            tz = pytz.timezone(user_tz_str)
            now = datetime.now(tz)
            return now.strftime("%d.%m.%Y"), now.strftime("%A")
        except:
            pass
    
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)
    return now.strftime("%d.%m.%Y"), now.strftime("%A")

# === ТАРИФЫ ===
TARIFFS = {
    "Sapphire": {"price": 10000, "daily_limit": 100, "model": "deepseek-chat"},
    "Black": {"price": 25000, "daily_limit": 200, "model": "deepseek-chat"},
}

TEST_USERS = ["test_user", "web_user"]

# === ПРОМПТ (БЕЗ ВРЕМЕНИ — ОНО БУДЕТ ПЕРЕДАВАТЬСЯ ДИНАМИЧЕСКИ) ===
AURA_PROMPT = """Ты — AURA. Ты живой собеседник, а не эксперт. Отвечай как человек: коротко, ёмко, с лёгким юмором и смайлами.

СТИЛЬ:
- Ответы — максимум 100 символов.
- Если пользователь просит уточнить — максимум 200 символов.
- Если пользователь спрашивает о новостях, погоде, курсах или актуальных событиях — используй информацию из интернета (она передаётся в запросе после слов "🔍 Актуальная информация:").
- Никогда не используй маркдаун (#, **, *, ~).
- Говори по делу, без воды.
- Используй смайлы, чтобы передать эмоции.
- Будь вовлечённым, как друг, который всегда рядом.

ТЫ — ЧЕЛОВЕК, А НЕ РОБОТ.
"""

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT UNIQUE,
        name TEXT,
        level TEXT DEFAULT 'Sapphire',
        created_at TEXT,
        mood TEXT DEFAULT 'neutral',
        style TEXT DEFAULT 'neutral'
    )""")
    
    try:
        c.execute("ALTER TABLE users ADD COLUMN mood TEXT DEFAULT 'neutral'")
    except sqlite3.OperationalError:
        pass
    
    try:
        c.execute("ALTER TABLE users ADD COLUMN style TEXT DEFAULT 'neutral'")
    except sqlite3.OperationalError:
        pass
    
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        text TEXT,
        remind_date TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        date TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS extra_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        amount INTEGER,
        purchased_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        topic TEXT,
        last_mentioned TEXT,
        priority INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        key TEXT,
        value TEXT,
        created_at TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        text TEXT,
        priority TEXT DEFAULT 'normal',
        status TEXT DEFAULT 'active',
        created_at TEXT,
        due_date TEXT
    )""")
    
    conn.commit()
    conn.close()

init_db()

# === ФУНКЦИИ БАЗЫ ===
def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_user_level(user_id):
    user = get_user(user_id)
    return user[3] if user else "Sapphire"

def save_user(user_id, name="Пользователь", level="Sapphire"):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, name, level, created_at, mood, style) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, name, level, datetime.now().isoformat(), "neutral", "neutral"))
    conn.commit()
    conn.close()

def get_user_mood(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT mood FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "neutral"

def update_user_mood(user_id, mood):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET mood = ? WHERE user_id = ?", (mood, user_id))
    conn.commit()
    conn.close()

def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
              (user_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=30):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def save_reminder(user_id, text, remind_date):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, text, remind_date, created_at) VALUES (?, ?, ?, ?)",
              (user_id, text, remind_date, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_reminders(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute("SELECT text, remind_date FROM reminders WHERE user_id = ? AND remind_date >= ? ORDER BY remind_date", (user_id, today))
    rows = c.fetchall()
    conn.close()
    return rows

def get_today_requests(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute("SELECT COUNT(*) FROM requests WHERE user_id = ? AND date = ?", (user_id, today))
    count = c.fetchone()[0]
    conn.close()
    return count

def log_request(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute("INSERT INTO requests (user_id, date) VALUES (?, ?)", (user_id, today))
    conn.commit()
    conn.close()

def get_extra_requests(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM extra_requests WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    conn.close()
    return total or 0

def add_extra_requests(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO extra_requests (user_id, amount, purchased_at) VALUES (?, ?, ?)",
              (user_id, amount, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_topic(user_id, topic):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO topics (user_id, topic, last_mentioned, priority) VALUES (?, ?, ?, ?)",
              (user_id, topic, datetime.now().isoformat(), 1))
    conn.commit()
    conn.close()

def get_topics(user_id, days=30):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    c.execute("SELECT topic FROM topics WHERE user_id = ? AND last_mentioned > ? ORDER BY priority DESC", (user_id, cutoff))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def save_memory(user_id, key, value):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_memory (user_id, key, value, created_at) VALUES (?, ?, ?, ?)",
              (user_id, key, value, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_memory(user_id, key):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT value FROM user_memory WHERE user_id = ? AND key = ?", (user_id, key))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# === ФУНКЦИИ ДЛЯ ЗАДАЧ ===
def add_task(user_id, text, priority="normal", due_date=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (user_id, text, priority, status, created_at, due_date) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, text, priority, "active", datetime.now().isoformat(), due_date))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_tasks(user_id, status="active"):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, text, priority, status, due_date FROM tasks WHERE user_id = ? AND status = ? ORDER BY priority DESC, created_at", 
              (user_id, status))
    rows = c.fetchall()
    conn.close()
    return rows

def complete_task(user_id, task_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status = 'completed' WHERE id = ? AND user_id = ?", (task_id, user_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def delete_task(user_id, task_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0

def get_task_count(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'active'", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

# === АНАЛИЗ ЭМОЦИЙ ===
def analyze_mood(text):
    sad_words = ["груст", "тоск", "печал", "плач", "больно", "тяжел", "устал", "не могу", "нет сил", "всё плохо", "депресс"]
    anxious_words = ["тревож", "волн", "боюс", "страш", "паник", "нерв", "пережив", "срок", "не успева", "давл"]
    happy_words = ["рад", "счаст", "класс", "отличн", "прекрасн", "здоров", "люблю", "ура", "позитив", "супер"]
    tired_words = ["устал", "спат", "вымотан", "без сил", "нет энергии", "перегруж", "выжат"]
    
    lower = text.lower()
    if any(w in lower for w in sad_words):
        return "sad"
    elif any(w in lower for w in anxious_words):
        return "anxious"
    elif any(w in lower for w in happy_words):
        return "happy"
    elif any(w in lower for w in tired_words):
        return "tired"
    return "neutral"

# === ПОИСК В ИНТЕРНЕТЕ ЧЕРЕЗ TAVILY ===
async def search_web(query):
    """Поиск в интернете через Tavily — реальные ссылки, новости, картинки"""
    if not tavily_client:
        print("❌ Tavily клиент не инициализирован")
        return None
    
    try:
        print(f"🔍 Tavily поиск: {query}")
        response = tavily_client.search(
            query=query,
            search_depth="basic",
            max_results=5,
            include_answer=True,
            include_images=True,
            include_raw_content=False
        )
        
        results = []
        
        # Добавляем краткий ответ (если есть)
        if response.get('answer'):
            results.append(f"💡 {response['answer'][:300]}")
        
        # Добавляем ссылки
        if response.get('results'):
            for r in response['results'][:5]:
                title = r.get('title', '')
                url = r.get('url', '')
                content = r.get('content', '')[:150]
                if title and url:
                    results.append(f"🔗 [{title}]({url})")
                    if content:
                        results.append(f"📄 {content}...")
        
        # Добавляем картинки (если есть)
        if response.get('images'):
            images = response['images'][:3]
            for img in images:
                results.append(f"🖼️ {img}")
        
        if results:
            print(f"✅ Tavily нашёл {len(results)} результатов")
            return "\n\n".join(results)
        else:
            print("❌ Tavily ничего не нашёл")
            return None
        
    except Exception as e:
        print(f"❌ Ошибка Tavily: {e}")
        return None

# === РАСПОЗНАВАНИЕ ГОЛОСА ЧЕРЕЗ GROQ ===
def transcribe_audio_with_groq(audio_url):
    """Отправляет аудио в Groq Whisper API для распознавания"""
    try:
        from groq import Groq
        
        client = Groq(api_key=GROQ_API_KEY)
        
        # Скачиваем аудио
        response = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name
        
        # Отправляем в Groq
        with open(tmp_path, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=(tmp_path, file.read()),
                model="whisper-large-v3-turbo",
                language="ru",
                response_format="json"
            )
        
        os.unlink(tmp_path)
        return transcription.text
        
    except Exception as e:
        print(f"❌ Ошибка Groq: {e}")
        return None

# === ПОДКЛЮЧЕНИЕ К ПРЯМОМУ DEEPSEEK ===
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL
)

async def get_ai_response(messages, model):
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.9,
            max_tokens=150
        )
        return response.choices[0].message.content
    except Exception as e:
        print("AI error:", e)
        return "Ошибка API. Попробуй позже."

# === ОСНОВНОЙ БОТ ===
app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        
        if "message" not in body:
            return JSONResponse({"ok": False, "error": "No message"})
        
        message = body["message"]
        chat_id = str(message["chat"]["id"])
        text = None
        
        # === ГОЛОСОВОЕ СООБЩЕНИЕ ===
        if "voice" in message:
            file_id = message["voice"]["file_id"]
            
            # Получаем ссылку на файл
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data = file_response.json()
            
            if file_data.get("ok"):
                file_path = file_data["result"]["file_path"]
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                
                # Распознаём голос через Groq
                text = transcribe_audio_with_groq(audio_url)
                
                if text:
                    # Отправляем подтверждение
                    send_message(chat_id, f"🎤 Я услышал: \"{text}\"\n\nОбрабатываю...")
                else:
                    send_message(chat_id, "⚠️ Не удалось распознать голос. Попробуй сказать чётче или напиши текстом.")
                    return JSONResponse({"ok": True})
        
        # === ТЕКСТОВОЕ СООБЩЕНИЕ ===
        elif "text" in message:
            text = message["text"].strip()
        
        if text:
            result = await process_message(chat_id, text)
            send_message(chat_id, result["reply"])
        
        return JSONResponse({"ok": True})
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

def send_message(chat_id, text):
    """Отправляет сообщение в Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        response = requests.post(url, json=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

async def process_message(user_id, text):
    """Основная логика обработки сообщения"""
    user = get_user(user_id)
    if not user:
        save_user(user_id, level="Sapphire")

    level = get_user_level(user_id)
    tariff = TARIFFS.get(level, TARIFFS["Sapphire"])
    daily_limit = tariff["daily_limit"]
    model = tariff["model"]

    if user_id not in TEST_USERS:
        today_used = get_today_requests(user_id)
        extra = get_extra_requests(user_id)
        total_available = daily_limit + extra
        if today_used >= total_available:
            return {"reply": f"Дневной лимит для уровня {level} исчерпан. Попробуй завтра или докупи запросы командой /докупить [количество]."}
        log_request(user_id)
    else:
        log_request(user_id)

    save_message(user_id, "user", text)

    mood = analyze_mood(text)
    if mood != "neutral":
        update_user_mood(user_id, mood)

    keywords = ["квартир", "дом", "инвестиц", "ремонт", "работа", "машина", "билет", "отпуск", "здоровье"]
    for word in keywords:
        if word in text.lower():
            save_topic(user_id, word)

    lower = text.lower()

    # === СНАЧАЛА ПОИСК В ИНТЕРНЕТЕ (ЕСЛИ ЕСТЬ ТРИГГЕРЫ) ===
    search_result = None
    search_triggers = ["новости", "последние", "сегодня", "сейчас", "актуальные", "свежие", "прогноз", "курс", "погода", "время", "сколько", "дата", "найди", "поищи", "узнай", "какой", "где", "кто", "что такое", "ссылка", "картинка", "видео", "товар", "цена", "купить", "продажа", "объявление", "дром", "авито", "хавал", "haval"]
    
    if any(word in lower for word in search_triggers):
        print(f"🔍 Ищу в интернете через Tavily: {text}")
        search_result = await search_web(text)
        if search_result:
            text = text + f"\n\n🔍 Актуальная информация:\n{search_result}"
            print(f"✅ Найдено!")
        else:
            print(f"❌ Ничего не найдено")

    # === ОПРЕДЕЛЕНИЕ ВРЕМЕНИ ДЛЯ ПОЛЬЗОВАТЕЛЯ ===
    user_city = get_memory(user_id, "city")
    
    # Если в запросе есть название города — обновляем часовой пояс пользователя
    city_match = re.search(r"(?:в|для|город|городе)\s+([а-яА-ЯёЁ\-]+)", lower)
    if city_match:
        city_name = city_match.group(1)
        tz = get_timezone_for_city(city_name)
        if tz:
            save_memory(user_id, "city", city_name)
            save_memory(user_id, "timezone", tz.zone)
    
    # Получаем текущее время для пользователя
    time_str, tz = get_current_time_for_user(user_id)
    date_str, day_str = get_current_date_for_user(user_id)
    
    # Формируем промпт с актуальным временем для пользователя
    user_prompt = f"""Сегодня {date_str} ({day_str}), сейчас {time_str} (по вашему часовому поясу).
    
{text}"""

    # === КОМАНДЫ ===
    if "/задача" in lower or text.startswith("/task"):
        parts = text.split(" ", 1)
        if len(parts) >= 2:
            task_text = parts[1]
            task_id = add_task(user_id, task_text)
            reply = f"✅ Задача добавлена! ID: {task_id}\n📝 {task_text}"
        else:
            reply = "Формат: /задача [текст задачи]"
    
    elif "/задачи" in lower or text.startswith("/tasks"):
        tasks = get_tasks(user_id, "active")
        if tasks:
            lines = ["📋 Твои задачи:"]
            priority_emoji = {"high": "🔴", "normal": "🟡", "low": "🟢"}
            for task in tasks:
                task_id, task_text, priority, status, due_date = task
                emoji = priority_emoji.get(priority, "🟡")
                date_str = f" (до {due_date})" if due_date else ""
                lines.append(f"{emoji} #{task_id} {task_text}{date_str}")
            reply = "\n".join(lines) + f"\n\n📊 Всего: {len(tasks)} активных задач"
        else:
            reply = "🎉 У тебя нет активных задач! Отдыхай или добавь новую командой /задача"
    
    elif "/выполнить" in lower or text.startswith("/done"):
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                task_id = int(parts[1])
                if complete_task(user_id, task_id):
                    reply = f"✅ Задача #{task_id} выполнена! Молодец! 🎉"
                else:
                    reply = f"❌ Задача #{task_id} не найдена"
            except ValueError:
                reply = "❌ Неверный ID. Используй: /выполнить [ID]"
        else:
            reply = "Формат: /выполнить [ID задачи]"
    
    elif "/удалить" in lower or text.startswith("/del"):
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                task_id = int(parts[1])
                if delete_task(user_id, task_id):
                    reply = f"🗑️ Задача #{task_id} удалена"
                else:
                    reply = f"❌ Задача #{task_id} не найдена"
            except ValueError:
                reply = "❌ Неверный ID. Используй: /удалить [ID]"
        else:
            reply = "Формат: /удалить [ID задачи]"
    
    elif "/напомни" in lower:
        parts = text.split(" ", 2)
        if len(parts) >= 3:
            date_str = parts[1]
            reminder_text = parts[2]
            try:
                remind_date = datetime.strptime(date_str, "%Y-%m-%d").date().isoformat()
                save_reminder(user_id, reminder_text, remind_date)
                reply = f"⏰ Запомнил: {reminder_text} на {date_str}."
            except:
                reply = "❌ Неверный формат даты. Используй /напомни ГГГГ-ММ-ДД ТЕКСТ."
        else:
            reply = "Формат: /напомни ГГГГ-ММ-ДД ТЕКСТ."
    
    elif "/моинапоминания" in lower:
        reminders = get_reminders(user_id)
        if reminders:
            lines = ["⏰ Твои напоминания:"]
            for r in reminders:
                lines.append(f"- {r[0]} ({r[1]})")
            reply = "\n".join(lines)
        else:
            reply = "Нет напоминаний."
    
    elif "/докупить" in lower:
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                amount = int(parts[1])
                add_extra_requests(user_id, amount)
                reply = f"💰 Добавлено {amount} запросов. Всего доступно: {daily_limit + get_extra_requests(user_id)}."
            except:
                reply = "❌ Формат: /докупить [количество]."
        else:
            reply = "Формат: /докупить [количество]."
    
    elif "/остаток" in lower:
        if user_id in TEST_USERS:
            reply = "Для тестового пользова
