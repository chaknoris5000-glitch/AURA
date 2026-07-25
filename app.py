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
import shutil
import threading
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.mime.text import MIMEText
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import base64
import io
from PIL import Image
import urllib.parse

load_dotenv()

try:
    from tavily import TavilyClient
except ImportError:
    print("⚠️ Tavily не установлен")
    TavilyClient = None

DB_NAME = "aura.db"
BACKUP_NAME = "aura_backup.db"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")

# === ЗАЩИТА ОТ ДУБЛЯЖА ГОЛОСА ===
LAST_VOICE_MESSAGE = {}

print("🔍 Проверка ключей...")
if not DEEPSEEK_API_KEY:
    print("❌ НЕТ КЛЮЧА DEEPSEEK!")
if not TELEGRAM_TOKEN:
    print("❌ НЕТ КЛЮЧА TELEGRAM!")
if not TAVILY_API_KEY:
    print("⚠️ НЕТ КЛЮЧА TAVILY")
if not YANDEX_API_KEY:
    print("⚠️ НЕТ КЛЮЧА YANDEX (поиск по .ru будет работать через SearXNG)")

tavily_client = None
if TavilyClient and TAVILY_API_KEY:
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        print("✅ Tavily инициализирован")
    except Exception as e:
        print(f"⚠️ Tavily: {e}")

# ==========================
# ОПИСАНИЕ БОТА (В ПРОФИЛЕ TELEGRAM)
# ==========================

def set_bot_description():
    description = """Привет! Я — AURA, твой помощник.

Вот что я могу для тебя сделать:
- Запомню всё и напомню вовремя.
- Найду нужную информацию за секунды.
- Помогу спланировать день и решить задачи.
- Запомню контекст — не нужно объяснять дважды.
- Работаю с текстом, фото, документами и голосом.

Напиши, что нужно — я рядом."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyDescription"
        data = {"description": description}
        response = requests.post(url, json=data, timeout=10)
        if response.status_code == 200:
            print("✅ Описание бота установлено!")
        else:
            print(f"❌ Ошибка установки описания: {response.status_code}")
    except Exception as e:
        print(f"❌ Ошибка: {e}")

set_bot_description()

# ==========================
# ГОЛОС (Google TTS)
# ==========================

def google_tts(text):
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang='ru', slow=False, tld='ru')
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tts.save(tmp.name)
            return tmp.name
    except Exception as e:
        print(f"❌ Google TTS ошибка: {e}")
        return None

def send_voice_reply(chat_id, text):
    if not text:
        return False
    
    # Защита от дубляжа
    text_hash = hash(text)
    if LAST_VOICE_MESSAGE.get(chat_id) == text_hash:
        print(f"⏭️ Пропускаем дубляж голоса для {chat_id}")
        return True
    
    # Берём только ПЕРВОЕ предложение (основную мысль)
    voice_text = text.split('\n')[0] if '\n' in text else text
    
    # Убираем ссылки, номера телефонов, эмодзи
    voice_text = re.sub(r'https?://\S+', '', voice_text)                           # ссылки
    voice_text = re.sub(r'\+7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', '', voice_text)  # +7 XXX XXX XX XX
    voice_text = re.sub(r'8\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', '', voice_text)   # 8 XXX XXX XX XX
    voice_text = re.sub(r'[#*_~`]', '', voice_text)                               # маркдаун
    voice_text = re.sub(r'[✅❌👉📌⚡🔮🚀😊🔥💬📸🎤🌍]', '', voice_text)             # эмодзи
    voice_text = re.sub(r'\s+', ' ', voice_text).strip()                          # лишние пробелы
    
    if len(voice_text) < 10:
        sentences = text.split('.')
        for s in sentences[1:3]:
            clean = re.sub(r'https?://\S+', '', s)
            clean = re.sub(r'\+7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', '', clean)
            clean = re.sub(r'8\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', '', clean)
            clean = re.sub(r'[#*_~`]', '', clean)
            clean = re.sub(r'[✅❌👉📌⚡🔮🚀😊🔥💬📸🎤🌍]', '', clean).strip()
            if len(clean) > 10:
                voice_text = clean
                break
    
    if len(voice_text) > 300:
        voice_text = voice_text[:300] + "..."
    
    if not voice_text or len(voice_text) < 5:
        print("⚠️ Текст слишком короткий для голоса, пропускаю")
        return False
    
    print(f"🎤 Озвучиваю: {voice_text[:50]}...")
    
    audio_path = google_tts(voice_text)
    if not audio_path:
        print("❌ Голос не синтезирован")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendAudio"
        with open(audio_path, 'rb') as f:
            files = {'audio': f}
            data = {'chat_id': chat_id}
            response = requests.post(url, files=files, data=data, timeout=30)
        os.unlink(audio_path)
        if response.status_code == 200:
            LAST_VOICE_MESSAGE[chat_id] = text_hash
            print("✅ Голосовое сообщение отправлено!")
            return True
        return False
    except Exception as e:
        print(f"❌ Отправка голоса: {e}")
        return False

# ==========================
# СТАТУС "ПЕЧАТАЕТ..."
# ==========================

def send_typing(chat_id):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
        data = {"chat_id": chat_id, "action": "typing"}
        requests.post(url, json=data, timeout=3)
    except Exception as e:
        print(f"❌ Ошибка typing: {e}")

# ==========================
# НОРМАЛИЗАЦИЯ
# ==========================

def normalize_query(text):
    corrections = {
        r"валдберис": "Wildberries",
        r"валберис": "Wildberries",
        r"вальдберис": "Wildberries",
        r"wildberris": "Wildberries",
        r"wildberies": "Wildberries",
        r"озон": "Ozon",
        r"ozon": "Ozon",
        r"котик": "кот",
        r"котики": "коты",
        r"картинк": "картинки",
        r"фотограф": "фото",
        r"изображен": "изображения",
        r"рисунк": "рисунки",
        r"сколька": "сколько",
        r"скольк": "сколько",
        r"который час": "сколько время",
        r"времян": "время",
        r"пагода": "погода",
        r"пагоду": "погоду",
        r"темпертур": "температура",
        r"нависти": "новости",
        r"навасти": "новости",
        r"свежи": "свежие",
        r"актуальн": "актуальные",
        r"клиник": "клиника",
        r"полихмакер": "парикмахерская",
        r"поліхмакер": "парикмахерская",
        r"палихмакер": "парикмахерская",
        r"инской": "Инской",
        r"очну": "хочу",
        r"хочю": "хочу",
    }
    normalized = text.lower()
    for pattern, replacement in corrections.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    if normalized != text.lower():
        print(f"🔧 Нормализация: '{text}' → '{normalized}'")
    return normalized

# ==========================
# ПАРСИНГ САЙТОВ
# ==========================

def parse_site_for_info(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "ru-RU,ru;q=0.9"}
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.decompose()
        text = soup.get_text(separator="\n", strip=True)
        result = {}
        
        phone_patterns = [r'\+7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', r'8\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', r'7\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}']
        phones = []
        for pattern in phone_patterns:
            phones.extend(re.findall(pattern, text))
        phones = [re.sub(r'\s+', ' ', p).strip() for p in phones]
        phones = list(set(phones))[:5]
        if phones:
            result["phones"] = phones
        
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        emails = list(set(re.findall(email_pattern, text)))[:3]
        if emails:
            result["emails"] = emails
        
        address_pattern = r'(?:ул\.|улица|проспект|пр\.|переулок|пер\.|площадь|пл\.|шоссе|бульвар)\s+[А-Яа-я0-9\-\.\s,]+'
        addresses = list(set(re.findall(address_pattern, text)))[:3]
        if addresses:
            result["addresses"] = addresses
        
        result["snippet"] = text[:500].replace("\n", " ")
        return result
    except Exception as e:
        print(f"❌ Ошибка парсинга: {e}")
        return None

# ==========================
# ПОИСК ПО .RU (Яндекс + SearXNG)
# ==========================

async def search_yandex(query):
    if not YANDEX_API_KEY:
        return None
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://yandex.ru/search/xml?text={encoded_query}&l10n=ru&sortby=rlv&filter=strict"
        headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'xml')
            results = []
            for doc in soup.find_all('doc')[:5]:
                title = doc.find('title')
                url_elem = doc.find('url')
                snippet = doc.find('snippet')
                if title and url_elem:
                    results.append({
                        "title": title.text.strip(),
                        "url": url_elem.text.strip(),
                        "snippet": snippet.text.strip()[:300] if snippet else ""
                    })
            return results if results else None
        else:
            return None
    except Exception as e:
        print(f"❌ Yandex поиск ошибка: {e}")
        return None

async def search_searxng(query):
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://searx.be/search?q={encoded_query}&format=json&categories=general&language=ru"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            results = []
            for r in data.get("results", [])[:5]:
                title = r.get("title", "")
                url_elem = r.get("url", "")
                snippet = r.get("content", "")[:300]
                if title and url_elem:
                    results.append({
                        "title": title.strip(),
                        "url": url_elem.strip(),
                        "snippet": snippet.strip()
                    })
            return results if results else None
        return None
    except Exception as e:
        print(f"❌ SearXNG ошибка: {e}")
        return None

async def search_ru_deep(query):
    print(f"🔍 Поиск по .ru: {query}")
    results = await search_yandex(query)
    if not results:
        print("🔄 Яндекс не дал результатов, пробую SearXNG...")
        results = await search_searxng(query)
    if not results:
        print("🔄 SearXNG не дал результатов, пробую Tavily...")
        if tavily_client:
            try:
                response = tavily_client.search(
                    query=query,
                    search_depth="advanced",
                    max_results=5,
                    include_answer=True,
                    include_images=False
                )
                if response.get('results'):
                    results = []
                    for r in response['results'][:5]:
                        results.append({
                            "title": r.get('title', ''),
                            "url": r.get('url', ''),
                            "snippet": r.get('content', '')[:300]
                        })
            except Exception as e:
                print(f"❌ Tavily ошибка: {e}")
    return results

async def search_web(query, need_links=False, is_image_search=False):
    if is_image_search:
        encoded_query = query.replace(" ", "%20")
        return f"https://yandex.ru/images/search?text={encoded_query}"
    results = await search_ru_deep(query)
    if not results:
        return "❌ Ничего не найдено."
    formatted = []
    for r in results:
        title = r.get('title', '')
        url = r.get('url', '')
        snippet = r.get('snippet', '')[:250]
        formatted.append(f"**{title}**\n{snippet}...")
        if need_links:
            formatted.append(f"🔗 {url}")
        formatted.append("")
    return "\n".join(formatted).strip()

# ==========================
# VISION (ОТКЛЮЧЕНА)
# ==========================

def describe_image_with_groq(image_data):
    return None

def ocr_yandex(image_data):
    return None

# ==========================
# БЭКАП
# ==========================

def send_backup_email():
    try:
        if not os.path.exists(DB_NAME):
            return False
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        msg['Subject'] = f"💾 Бэкап AURA {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        body = f"🧠 Бэкап базы данных AURA\n📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        with open(DB_NAME, "rb") as attachment:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename=aura_backup_{datetime.now().strftime("%Y%m%d_%H%M")}.db')
            msg.attach(part)
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        server.quit()
        print("✅ Бэкап отправлен на почту")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки бэкапа: {e}")
        return False

def backup_database():
    try:
        if os.path.exists(DB_NAME):
            shutil.copy2(DB_NAME, BACKUP_NAME)
            return True
        return False
    except Exception as e:
        print(f"❌ Ошибка бэкапа: {e}")
        return False

def restore_database():
    try:
        if os.path.exists(BACKUP_NAME):
            shutil.copy2(BACKUP_NAME, DB_NAME)
            return True
        return False
    except Exception as e:
        print(f"❌ Ошибка восстановления: {e}")
        return False

def backup_scheduler():
    hour_counter = 0
    while True:
        time.sleep(3600)
        if backup_database():
            hour_counter += 1
            if hour_counter >= 24:
                send_backup_email()
                hour_counter = 0

print("🔄 Проверка базы данных...")
if not os.path.exists(DB_NAME):
    if restore_database():
        print("✅ База восстановлена")
    else:
        print("📦 Создаю новую базу")
else:
    print("✅ База данных найдена")
    backup_database()

backup_thread = threading.Thread(target=backup_scheduler, daemon=True)
backup_thread.start()
print("🔄 Планировщик бэкапа запущен")

# ==========================
# БАЗА ДАННЫХ
# ==========================

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
        style TEXT DEFAULT 'neutral',
        city TEXT DEFAULT NULL,
        city_asked BOOLEAN DEFAULT 0
    )""")
    try:
        c.execute("ALTER TABLE users ADD COLUMN city TEXT DEFAULT NULL")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN city_asked BOOLEAN DEFAULT 0")
    except:
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
        remind_time TEXT,
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

# ==========================
# ФУНКЦИИ БАЗЫ
# ==========================

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

def save_user(user_id, name="Пользователь", level="Sapphire", city=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, name, level, created_at, mood, style, city, city_asked) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (user_id, name, level, datetime.now().isoformat(), "neutral", "neutral", city, 1 if city else 0))
    conn.commit()
    conn.close()

def get_user_city(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT city, city_asked FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, 0)

def update_user_city(user_id, city):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET city = ?, city_asked = 1 WHERE user_id = ?", (city, user_id))
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

def get_history(user_id, limit=200):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content, created_at FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1], "time": r[2]} for r in reversed(rows)]

def get_message_count(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM history WHERE user_id = ?", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def save_reminder(user_id, text, remind_date, remind_time):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, text, remind_date, remind_time, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, text, remind_date, remind_time, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_reminders(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute("SELECT text, remind_date, remind_time FROM reminders WHERE user_id = ? AND remind_date >= ? ORDER BY remind_date, remind_time", (user_id, today))
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

def get_all_topics(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT topic, COUNT(*) as cnt FROM topics WHERE user_id = ? GROUP BY topic ORDER BY cnt DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

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

# ==========================
# ЗАДАЧИ
# ==========================

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

# ==========================
# АНАЛИЗ НАСТРОЕНИЯ
# ==========================

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

# ==========================
# ОПРЕДЕЛЕНИЕ ГОРОДА
# ==========================

def get_city_by_ip(ip):
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,timezone,offset", timeout=3)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success":
                return {
                    "city": data.get("city", ""),
                    "region": data.get("regionName", ""),
                    "country": data.get("country", ""),
                    "timezone": data.get("timezone", ""),
                    "offset": data.get("offset", 0) // 3600
                }
    except:
        pass
    return None

def get_timezone_offset(city_name):
    timezones = {
        "белово": 7, "кемерово": 7, "новокузнецк": 7, "прокопьевск": 7,
        "киселёвск": 7, "междуреченск": 7, "инской": 7,
        "москва": 3, "санкт-петербург": 3, "екатеринбург": 5, "новосибирск": 7,
        "омск": 6, "красноярск": 7, "иркутск": 8, "владивосток": 10,
        "хабаровск": 10, "алматы": 5, "астана": 5, "минск": 3,
        "киев": 2, "рига": 2, "лондон": 0, "берлин": 1,
        "париж": 1, "нью-йорк": -4, "лос-анджелес": -7
    }
    for city, offset in timezones.items():
        if city in city_name.lower():
            return offset
    return 3

# ==========================
# ГОЛОС (ВХОД)
# ==========================

def transcribe_audio_with_groq(audio_url):
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        response = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name
        with open(tmp_path, "rb") as file:
            transcription = client.
