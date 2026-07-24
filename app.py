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

# === ЗАГРУЗКА КЛЮЧЕЙ ИЗ .env ===
load_dotenv()

# === TAVILY ДЛЯ ПОИСКА ===
try:
    from tavily import TavilyClient
except ImportError:
    print("⚠️ Tavily не установлен")
    TavilyClient = None

# === КОНФИГ ===
DB_NAME = "aura.db"
BACKUP_NAME = "aura_backup.db"
LAST_VOICE_MESSAGE = {}  # для предотвращения дубляжа голоса

# === КЛЮЧИ ===
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")

print("🔍 Проверка ключей...")
if not DEEPSEEK_API_KEY:
    print("❌ НЕТ КЛЮЧА DEEPSEEK!")
if not TELEGRAM_TOKEN:
    print("❌ НЕТ КЛЮЧА TELEGRAM!")
if not TAVILY_API_KEY:
    print("⚠️ НЕТ КЛЮЧА TAVILY")
if not YANDEX_API_KEY:
    print("⚠️ НЕТ КЛЮЧА YANDEX")

# === TAVILY CLIENT ===
tavily_client = None
if TavilyClient and TAVILY_API_KEY:
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        print("✅ Tavily инициализирован")
    except Exception as e:
        print(f"⚠️ Tavily: {e}")

# === SILERO TTS ===
def silero_tts(text):
    try:
        import torch
        import soundfile as sf
        device = torch.device('cpu')
        model, _ = torch.hub.load(repo_or_dir='snakers4/silero-models',
                                   model='silero_tts',
                                   language='ru',
                                   speaker='aidar')
        model.to(device)
        audio = model.apply_tts(text, speaker='aidar', sample_rate=48000)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            sf.write(tmp.name, audio, 48000)
            return tmp.name
    except Exception as e:
        print(f"❌ Silero TTS: {e}")
        return None

# === YANDEX TTS (мужской голос) ===
def yandex_tts(text):
    if not YANDEX_API_KEY:
        return None
    try:
        url = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
        headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
        data = {
            "text": text[:500],
            "lang": "ru-RU",
            "voice": "filipp",  # Мужской голос
            "emotion": "good",
            "speed": 1.0,
            "format": "lpcm",
            "sampleRateHertz": 48000
        }
        response = requests.post(url, headers=headers, data=data, timeout=10)
        if response.status_code == 200:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                tmp.write(response.content)
                return tmp.name
        return None
    except Exception as e:
        print(f"❌ Yandex TTS: {e}")
        return None

# === ОСНОВНАЯ ФУНКЦИЯ ОТПРАВКИ ГОЛОСА (С ЗАЩИТОЙ ОТ ДУБЛЯЖА) ===
def send_voice_reply_sync(chat_id, text):
    """Синхронная отправка голоса (Яндекс → Silero) с защитой от дубляжа"""
    if not text or len(text.strip()) == 0:
        return False
    
    # Проверяем, не отправляли ли мы уже этот же текст в голосе
    text_hash = hash(text[:100])
    if LAST_VOICE_MESSAGE.get(chat_id) == text_hash:
        print(f"⏭️ Пропускаем дубляж голоса для {chat_id}")
        return True
    
    voice_text = text.split('\n')[0] if '\n' in text else text
    if len(voice_text) > 300:
        voice_text = voice_text[:300] + "..."
    
    audio_path = None
    used_service = None
    
    if YANDEX_API_KEY:
        print(f"🎤 Пробую Яндекс TTS (мужской)...")
        audio_path = yandex_tts(voice_text)
        if audio_path:
            used_service = "Яндекс (мужской)"
    
    if not audio_path:
        print(f"🎤 Пробую Silero TTS...")
        audio_path = silero_tts(voice_text)
        if audio_path:
            used_service = "Silero"
    
    if not audio_path:
        print("❌ Не удалось синтезировать голос")
        return False
    
    print(f"✅ Голос синтезирован через {used_service}")
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendAudio"
        with open(audio_path, 'rb') as f:
            files = {'audio': f}
            data = {'chat_id': chat_id}
            response = requests.post(url, files=files, data=data, timeout=30)
        os.unlink(audio_path)
        if response.status_code == 200:
            print(f"✅ Голосовое сообщение отправлено!")
            LAST_VOICE_MESSAGE[chat_id] = text_hash
            return True
        else:
            print(f"❌ Ошибка отправки: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Отправка голоса: {e}")
        return False

# === НОРМАЛИЗАЦИЯ ===
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

# === ПАРСИНГ САЙТОВ ===
def parse_site_for_info(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        for script in soup(["script", "style"]):
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
        address_pattern = r'(?:ул\.|улица|проспект|пр\.|переулок|пер\.|площадь|пл\.)\s+[А-Яа-я0-9\-\.\s,]+'
        addresses = list(set(re.findall(address_pattern, text)))[:3]
        if addresses:
            result["addresses"] = addresses
        return result
    except Exception as e:
        print(f"❌ Ошибка парсинга: {e}")
        return None

# === VISION (РАСПОЗНАВАНИЕ ИЗОБРАЖЕНИЙ ЧЕРЕЗ GROQ) ===
def describe_image_with_groq(image_data):
    try:
        import groq
        
        if isinstance(image_data, bytes):
            img = Image.open(io.BytesIO(image_data))
        else:
            img = Image.open(io.BytesIO(image_data))
        
        max_size = 512
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        
        buffer = io.BytesIO()
        img.convert('RGB').save(buffer, format='JPEG', quality=80)
        compressed_data = buffer.getvalue()
        
        base64_image = base64.b64encode(compressed_data).decode('utf-8')
        
        client = groq.Groq(api_key=GROQ_API_KEY)
        
        response = client.chat.completions.create(
            model="llama-3.2-90b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опиши, что ты видишь на этой картинке. Если там есть текст, напиши его. Ответ дай на русском, кратко, но с деталями."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            temperature=0.7,
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Groq Vision ошибка: {e}")
        return None

# === OCR (РАСПОЗНАВАНИЕ ТЕКСТА ЧЕРЕЗ YANDEX) ===
def ocr_yandex(image_data):
    if not YANDEX_API_KEY:
        return None
    try:
        if isinstance(image_data, bytes):
            img = Image.open(io.BytesIO(image_data))
        else:
            img = Image.open(io.BytesIO(image_data))
        
        max_size = 2048
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=90)
        compressed_data = buffer.getvalue()
        
        base64_image = base64.b64encode(compressed_data).decode('utf-8')
        
        url = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
        headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
        
        payload = {
            "analyze_specs": [{
                "content": base64_image,
                "features": [{"type": "TEXT_DETECTION"}]
            }]
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            text_blocks = []
            for result in data.get("results", []):
                for block in result.get("textDetection", {}).get("blocks", []):
                    for line in block.get("lines", []):
                        text_blocks.append(line.get("text", ""))
            return "\n".join(text_blocks) if text_blocks else None
        else:
            print(f"❌ Yandex OCR ошибка: {response.status_code}")
            return None
    except Exception as e:
        print(f"❌ Yandex OCR: {e}")
        return None

# === СТАТУС "ПЕЧАТАЕТ..." ===
def send_typing(chat_id):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
        data = {"chat_id": chat_id, "action": "typing"}
        requests.post(url, json=data, timeout=5)
    except Exception as e:
        print(f"❌ Ошибка typing: {e}")

# === БЭКАП ===
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

def get_history(user_id, limit=50):
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

def get_user_topics_summary(user_id):
    topics = get_all_topics(user_id)
    if not topics:
        return None
    topic_counts = {}
    for t in topics:
        topic_counts[t] = topic_counts.get(t, 0) + 1
    sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)
    top_topics = [t[0] for t in sorted_topics[:10]]
    if top_topics:
        return f"📚 Я помню, что мы обсуждали: {', '.join(top_topics)}. Чем могу помочь сегодня? 😊"
    return None

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

# === ЗАДАЧИ ===
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

# === АНАЛИЗ НАСТРОЕНИЯ ===
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

# === ОПРЕДЕЛЕНИЕ ГОРОДА ===
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

# === ПОИСК ===
async def search_duckduckgo(query):
    try:
        url = f"https://html.duckduckgo.com/html/?q={query}"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        for result in soup.select('.result')[:5]:
            title = result.select_one('.result__title')
            if title:
                link = result.select_one('.result__url')
                text_elem = result.select_one('.result__snippet')
                if text_elem and link:
                    results.append({
                        "title": title.text.strip(),
                        "snippet": text_elem.text.strip()[:300],
                        "url": link.text.strip()
                    })
        return results if results else None
    except Exception as e:
        print(f"❌ DuckDuckGo: {e}")
        return None

async def search_web(query, need_links=False, is_image_search=False):
    if is_image_search:
        encoded_query = query.replace(" ", "%20")
        return f"https://yandex.ru/images/search?text={encoded_query}"
    if tavily_client:
        try:
            response = tavily_client.search(
                query=query,
                search_depth="advanced",
                max_results=5,
                include_answer=True,
                include_images=False
            )
            results = []
            urls = []
            if response.get('answer'):
                results.append(response['answer'])
            if response.get('results'):
                for r in response['results'][:5]:
                    title = r.get('title', '')
                    url = r.get('url', '')
                    content = r.get('content', '')[:300]
                    if title and url:
                        urls.append(url)
                        results.append(f"**{title}**\n{content}...")
                        if need_links:
                            results.append(f"🔗 {url}")
            if "телефон" in query.lower() or "адрес" in query.lower() or "номер" in query.lower():
                print("📞 Ищем контакты...")
                parsed_info = []
                for url in urls[:3]:
                    info = parse_site_for_info(url)
                    if info:
                        info_text = f"🔗 {url}\n"
                        if info.get("phones"):
                            info_text += f"📞 {', '.join(info['phones'])}\n"
                        if info.get("addresses"):
                            info_text += f"📍 {', '.join(info['addresses'])}\n"
                        parsed_info.append(info_text)
                if parsed_info:
                    results.append("\n".join(parsed_info))
            return "\n\n".join(results) if results else None
        except Exception as e:
            print(f"❌ Tavily: {e}")
    duck_results = await search_duckduckgo(query)
    if duck_results:
        results = []
        for r in duck_results:
            results.append(f"**{r['title']}**\n{r['snippet']}...")
        return "\n\n".join(results) if results else None
    return None

# === ГОЛОС (ВХОД) ===
def transcribe_audio_with_groq(audio_url):
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        response = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name
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
        print(f"❌ Groq: {e}")
        return None

# === DEEPSEEK ===
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL
)

async def get_ai_response(messages, model):
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.8,
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        print("AI error:", e)
        return "Извини, сейчас проблемы с подключением. Попробуй позже."

def create_summary(user_id, messages):
    try:
        summary_prompt = f"""Сделай краткую выжимку этого диалога (максимум 200 символов). Выдели основные темы и интересы.
Диалог:
{messages}
Выжимка:"""
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": summary_prompt}],
            temperature=0.7,
            max_tokens=150
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Ошибка выжимки: {e}")
        return None

# === ТАРИФЫ ===
TARIFFS = {
    "Sapphire": {"price": 10000, "daily_limit": 100, "model": "deepseek-chat"},
    "Black": {"price": 25000, "daily_limit": 200, "model": "deepseek-chat"},
}
TEST_USERS = ["test_user", "web_user"]

# === ЧЕЛОВЕЧЕСКИЙ ПРОМПТ ===
AURA_PROMPT = """Ты — AURA, помощник в Telegram.

ТВОЙ СТИЛЬ:
- Отвечай как живой человек — коротко, по делу, без воды.
- Эмодзи используй РЕДКО — только для усиления эффекта (🚀🔥💡👉).
- НЕ ставь галочки (✅) в каждом ответе — они нужны только для списков.
- Отвечай на вопрос прямо, потом дай ссылку или совет.
- В конце не спрашивай "что ещё?" в каждом сообщении — только если действительно нужно.

Ты понятный, живой и полезный помощник. Без перегруза."""

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
        image_data = None
        
        send_typing(chat_id)
        
        if "voice" in message:
            file_id = message["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data = file_response.json()
            if file_data.get("ok"):
                file_path = file_data["result"]["file_path"]
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                text = transcribe_audio_with_groq(audio_url)
                if not text:
                    send_message(chat_id, "⚠️ Не удалось распознать голос")
                    return JSONResponse({"ok": True})
        
        elif "photo" in message:
            photo = message["photo"][-1]
            file_id = photo["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data = file_response.json()
            if file_data.get("ok"):
                file_path = file_data["result"]["file_path"]
                image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                image_response = requests.get(image_url, timeout=30)
                if image_response.status_code == 200:
                    image_data = image_response.content
                    send_message(chat_id, "🖼️ Обрабатываю фото...")
                    vision_result = describe_image_with_groq(image_data)
                    ocr_result = ocr_yandex(image_data)
                    result_text = "**📸 Что я вижу на фото:**\n\n"
                    if vision_result:
                        result_text += f"{vision_result}\n\n"
                    else:
                        result_text += "❌ Не удалось описать фото.\n\n"
                    if ocr_result:
                        result_text += f"**📝 Текст на фото:**\n{ocr_result}"
                    else:
                        result_text += "📝 Текст на фото не найден."
                    send_message(chat_id, result_text)
                    return JSONResponse({"ok": True})
                else:
                    send_message(chat_id, "⚠️ Не удалось загрузить фото")
                    return JSONResponse({"ok": True})
        
        elif "text" in message:
            text = message["text"].strip()
        
        if text:
            result = await process_message(request, chat_id, text)
            formatted_reply = result["reply"]
            send_message(chat_id, formatted_reply)
            if result["reply"]:
                # Отправляем голос синхронно, в отдельном потоке
                threading.Thread(target=send_voice_reply_sync, args=(chat_id, result["reply"])).start()
                
        return JSONResponse({"ok": True})
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        response = requests.post(url, json=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Отправка: {e}")
        return False

async def process_message(request: Request, user_id, text):
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
            return {"reply": "⚠️ Дневной лимит запросов исчерпан. Попробуй завтра."}
        log_request(user_id)
    else:
        log_request(user_id)

    save_message(user_id, "user", text)
    
    mood = analyze_mood(text)
    if mood != "neutral":
        update_user_mood(user_id, mood)

    lower = text.lower()
    normalized_text = normalize_query(text)
    search_text = normalized_text if normalized_text != lower else lower

    # === ПАМЯТЬ ===
    msg_count = get_message_count(user_id)
    if msg_count <= 2:
        topics_summary = get_user_topics_summary(user_id)
        if topics_summary:
            send_message(user_id, f"**👋 Привет!** {topics_summary}")
            save_message(user_id, "assistant", topics_summary)
        else:
            welcome = "**👋 Привет!** Я AURA — твой помощник. 😊\n\n✅ Ищу информацию в интернете\n✅ Нахожу телефоны и адреса\n✅ Распознаю текст на фото\n✅ Отвечаю голосом\n\nЧем могу помочь?"
            send_message(user_id, welcome)
            save_message(user_id, "assistant", welcome)

    # === ГОРОД ===
    city_info = get_user_city(user_id)
    user_city = city_info[0] if city_info else None
    city_asked = city_info[1] if city_info else 0

    city_match = re.search(r"(?:мой город|я в|я из|город|городе|из)\s+([а-яА-ЯёЁ\-]+)", lower)
    if city_match:
        city_name = city_match.group(1).capitalize()
        update_user_city(user_id, city_name)
        user_city = city_name
        city_asked = 1
        save_memory(user_id, "city", city_name)
        save_memory(user_id, "tz_offset", str(get_timezone_offset(city_name)))
        print(f"📍 Город: {city_name}")

    elif not user_city and not city_asked:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        else:
            ip = request.client.host if request.client else "127.0.0.1"
        
        if ip and ip not in ["127.0.0.1", "localhost", "::1"]:
            city_data = get_city_by_ip(ip)
            if city_data and city_data.get("city"):
                user_city = city_data["city"]
                update_user_city(user_id, user_city)
                save_memory(user_id, "city", user_city)
                save_memory(user_id, "tz_offset", str(get_timezone_offset(user_city)))
        
        if not user_city:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("UPDATE users SET city_asked = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return {"reply": "🌍 Напиши свой город, чтобы я показывал точное время. Например: Мой город Белово"}

    if user_city:
        offset_hours = get_timezone_offset(user_city)
        current_time = datetime.utcnow() + timedelta(hours=offset_hours)
        current_date = current_time.strftime("%d.%m.%Y")
        current_day = current_time.strftime("%A")
        current_time_str = current_time.strftime("%H:%M")
        save_memory(user_id, "tz_offset", str(offset_hours))
    else:
        offset_hours = 3
        current_time = datetime.utcnow() + timedelta(hours=3)
        current_date = current_time.strftime("%d.%m.%Y")
        current_day = current_time.strftime("%A")
        current_time_str = current_time.strftime("%H:%M")
        user_city = "Москва"

    # === ПОИСК ===
    search_result = None
    
    is_image_search = bool(re.search(r"(?:картинк|фото|изображен|рисунк)", search_text))
    need_links = bool(re.search(r"(?:дай|покажи|скинь|ссылк|ссылка|link|url)", search_text))
    
    if is_image_search:
        print(f"🖼️ Поиск картинок: {text}")
        search_result = await search_web(search_text, need_links=True, is_image_search=True)
        if search_result:
            text = text + f"\n\n🔗 {search_result}"
            print("✅ Ссылка на картинки найдена")
    else:
        search_triggers = ["новости", "сегодня", "актуальные", "свежие", "прогноз", "курс", "погода", "найди", "поищи", "узнай", "где", "кто", "что такое", "клиника", "атака", "склады", "wildberries", "озон", "сайт", "адрес", "телефон", "контакт", "парикмахер", "инской", "раскраски"]
        if any(word in search_text for word in search_triggers):
            print(f"🔍 Поиск: {text}")
            search_result = await search_web(search_text, need_links=need_links, is_image_search=False)
            if search_result:
                text = text + f"\n\n{search_result}"
                print("✅ Найдено!")
            else:
                print("❌ Ничего не найдено")

    # === КОМАНДЫ ===
    if "/задача" in lower:
        parts = text.split(" ", 1)
        if len(parts) >= 2:
            task_id = add_task(user_id, parts[1])
            reply = f"✅ Задача добавлена! ID: {task_id}"
        else:
            reply = "**Формат:** /задача [текст]"
    
    elif "/задачи" in lower:
        tasks = get_tasks(user_id, "active")
        if tasks:
            lines = ["**📋 Твои задачи:**"]
            for task in tasks:
                task_id, task_text, priority, status, due_date = task
                lines.append(f"✅ #{task_id} {task_text}")
            reply = "\n".join(lines)
        else:
            reply = "🎉 Нет активных задач!"
    
    elif "/выполнить" in lower:
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                task_id = int(parts[1])
                if complete_task(user_id, task_id):
                    reply = f"✅ Задача #{task_id} выполнена! 🎉"
                else:
                    reply = f"❌ Задача #{task_id} не найдена"
            except ValueError:
                reply = "❌ Неверный ID"
        else:
            reply = "**Формат:** /выполнить [ID]"
    
    elif "/удалить" in lower:
        parts = text.split(" ")
        if len(parts) >= 2:
            try:
                task_id = int(parts[1])
                if delete_task(user_id, task_id):
                    reply = f"🗑️ Задача #{task_id} удалена"
                else:
                    reply = f"❌ Задача #{task_id} не найдена"
            except ValueError:
                reply = "❌ Неверный ID"
        else:
            reply = "**Формат:** /удалить [ID]"
    
    elif "/напомни" in lower:
        parts = text.split(" ", 3)
        if len(parts) >= 4:
            date_str = parts[1]
            time_str = parts[2]
            reminder_text = parts[3]
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
                datetime.strptime(time_str, "%H:%M")
                save_reminder(user_id, reminder_text, date_str, time_str)
                reply = f"⏰ Запомнил: {reminder_text} на {date_str} в {time_str}"
            except:
                reply = "❌ Неверный формат. Используй: /напомни ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ"
        else:
            reply = "**Формат:** /напомни ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ"
    
    elif "/моинапоминания" in lower:
        reminders = get_reminders(user_id)
        if reminders:
            lines = ["**⏰ Твои напоминания:**"]
            for r in reminders:
                lines.append(f"✅ {r[0]} ({r[1]} в {r[2]})")
            reply = "\n".join(lines)
        else:
            reply = "📭 Нет напоминаний."
    
    elif "/помощь" in lower or "/help" in lower:
        reply = """**🤖 Помощь AURA**

**📋 Задачи:**
/задача [текст] — добавить
/задачи — показать все
/выполнить [ID] — отметить
/удалить [ID] — удалить

**⏰ Напоминания:**
/напомни ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ
/моинапоминания — показать все

**📸 Фото:** отправь фото — опишу и распознаю текст
**🎤 Голос:** отправь голосовое — услышу и отвечу
**🌍 Город:** скажи "Мой город ..." — запомню время

Просто пиши вопросы — я отвечу! 😊"""
    
    else:
        user_name = get_memory(user_id, "name")
        if not user_name:
            name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", lower)
            if name_match:
                user_name = name_match.group(1).capitalize()
                save_memory(user_id, "name", user_name)
        
        name_context = f"\nИмя: {user_name}" if user_name else ""
        summary = get_memory(user_id, "summary")
        summary_context = f"\nВыжимка прошлых диалогов: {summary}" if summary else ""
        history = get_history(user_id, limit=30)
        
        user_prompt = f"Сегодня {current_date} ({current_day}), сейчас {current_time_str} (город: {user_city}).\n\n{text}"

        mood_context = ""
        if mood == "sad":
            mood_context = "Пользователь грустный. Отвечай тепло и поддерживающе."
        elif mood == "happy":
            mood_context = "Пользователь в хорошем настроении. Отвечай бодро и с юмором."
        elif mood == "anxious":
            mood_context = "Пользователь тревожится. Отвечай спокойно и уверенно."
        elif mood == "tired":
            mood_context = "Пользователь устал. Отвечай мягко и без лишней информации."

        aura_prompt = AURA_PROMPT + name_context + summary_context + f"\n\n{mood_context}\n\n{user_prompt}"

        messages = [{"role": "system", "content": aura_prompt}]
        for msg in history[-15:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": text})

        reply = await get_ai_response(messages, model)
        reply = re.sub(r'[*_#~`]', '', reply)

        if msg_count % 50 == 0 and msg_count > 0:
            recent_msgs = get_history(user_id, limit=15)
            dialog_text = "\n".join([f"{m['role']}: {m['content']}" for m in recent_msgs])
            new_summary = create_summary(user_id, dialog_text)
            if new_summary:
                old_summary = get_memory(user_id, "summary")
                combined = f"{old_summary}\n{new_summary}" if old_summary else new_summary
                if len(combined) > 2000:
                    combined = combined[-2000:]
                save_memory(user_id, "summary", combined)

    save_message(user_id, "assistant", reply)
    return {"reply": reply}

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
