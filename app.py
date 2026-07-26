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

# === ТВОЙ ID ДЛЯ БЕСПЛАТНОГО ДОСТУПА (АДМИНИСТРАТОР) ===
ADMIN_USERS = ["5818548555"]

LAST_VOICE_MESSAGE = {}

print("🔍 Проверка ключей...")
if not DEEPSEEK_API_KEY:
    print("❌ НЕТ КЛЮЧА DEEPSEEK!")
if not TELEGRAM_TOKEN:
    print("❌ НЕТ КЛЮЧА TELEGRAM!")
if not TAVILY_API_KEY:
    print("⚠️ НЕТ КЛЮЧА TAVILY")
if not GROQ_API_KEY:
    print("⚠️ НЕТ КЛЮЧА GROQ")

tavily_client = None
if TavilyClient and TAVILY_API_KEY:
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        print("✅ Tavily инициализирован")
    except Exception as e:
        print(f"⚠️ Tavily: {e}")

# ==========================
# ОПИСАНИЕ БОТА (ПРОФИЛЬ)
# ==========================

def set_bot_description():
    description = """👋Привет! Я — AURA, твой умный помощник! 
🔥Даю тебе - 7 дней бесплатного доступа!"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyDescription"
        data = {"description": description}
        requests.post(url, json=data, timeout=10)
    except:
        pass

set_bot_description()

# ==========================
# ГОЛОС
# ==========================

def google_tts(text):
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang='ru', slow=False)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tts.save(tmp.name)
            return tmp.name
    except Exception as e:
        print(f"❌ Google TTS: {e}")
        return None

def send_voice_reply(chat_id, text):
    if not text:
        return False
    text_hash = hash(text)
    if LAST_VOICE_MESSAGE.get(chat_id) == text_hash:
        return True
    voice_text = text.split('\n')[0][:300]
    if not voice_text:
        return False
    audio_path = google_tts(voice_text)
    if not audio_path:
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
    except:
        pass

# ==========================
# НОРМАЛИЗАЦИЯ
# ==========================

def normalize_query(text):
    corrections = {
        r"валдберис": "Wildberries",
        r"валберис": "Wildberries",
        r"вальдберис": "Wildberries",
        r"озон": "Ozon",
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
        r"нависти": "новости",
        r"навасти": "новости",
        r"клиник": "клиника",
        r"полихмакер": "парикмахерская",
        r"инской": "Инской",
        r"очну": "хочу",
        r"хочю": "хочу",
    }
    normalized = text.lower()
    for pattern, replacement in corrections.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized

# ==========================
# ПОИСК
# ==========================

async def search_web(query):
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
            if response.get('answer'):
                results.append(response['answer'])
            if response.get('results'):
                for r in response['results'][:5]:
                    title = r.get('title', '')
                    url = r.get('url', '')
                    content = r.get('content', '')[:200]
                    if title and url:
                        results.append(f"{title}\n{content}...\n{url}")
            return "\n\n".join(results) if results else None
        except Exception as e:
            print(f"❌ Tavily: {e}")
    
    try:
        url = f"https://html.duckduckgo.com/html/?q={query}"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        for result in soup.select('.result')[:3]:
            title = result.select_one('.result__title')
            if title:
                link = result.select_one('.result__url')
                snippet = result.select_one('.result__snippet')
                if snippet and link:
                    results.append(f"{title.text.strip()}\n{snippet.text.strip()[:150]}...\n{link.text.strip()}")
        return "\n\n".join(results) if results else None
    except Exception as e:
        print(f"❌ DuckDuckGo: {e}")
        return None

# ==========================
# VISION (ФОТО)
# ==========================

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
            model="llama-3.2-11b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опиши, что ты видишь на этой картинке. Если есть текст — напиши его. Ответ дай на русском."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            temperature=0.7,
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Vision: {e}")
        return None

# ==========================
# ЧТЕНИЕ ДОКУМЕНТОВ
# ==========================

def read_file(file_data, file_name):
    try:
        if file_name.endswith('.txt'):
            return file_data.decode('utf-8')
        elif file_name.endswith('.pdf'):
            try:
                import PyPDF2
                from io import BytesIO
                pdf_reader = PyPDF2.PdfReader(BytesIO(file_data))
                text = ""
                for page in pdf_reader.pages:
                    text += page.extract_text()
                return text
            except:
                return "⚠️ Не удалось прочитать PDF."
        elif file_name.endswith('.docx'):
            try:
                import docx
                from io import BytesIO
                doc = docx.Document(BytesIO(file_data))
                return "\n".join([para.text for para in doc.paragraphs])
            except:
                return "⚠️ Не удалось прочитать DOCX."
        else:
            return "⚠️ Формат не поддерживается. Используй TXT, PDF или DOCX."
    except Exception as e:
        return f"⚠️ Ошибка: {e}"

# ==========================
# НАПОМИНАНИЯ
# ==========================

def check_reminders():
    while True:
        try:
            time.sleep(60)
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("SELECT user_id, text, chat_id FROM reminders WHERE remind_time <= ? AND status = 'pending'", (now,))
            rows = c.fetchall()
            for user_id, text, chat_id in rows:
                send_message(chat_id, f"⏰ Напоминание: {text}")
                c.execute("UPDATE reminders SET status = 'done' WHERE user_id = ? AND text = ?", (user_id, text))
            conn.commit()
            conn.close()
        except:
            pass

reminder_thread = threading.Thread(target=check_reminders, daemon=True)
reminder_thread.start()

# ==========================
# МОНЕТИЗАЦИЯ
# ==========================

TARIFFS = {
    "собеседник": {"name": "Собеседник", "price": 50, "stars": 50},
    "партнёр": {"name": "Партнёр", "price": 120, "stars": 120},
    "агент_жизни": {"name": "Агент жизни", "price": 250, "stars": 250}
}

TRIAL_DAYS = 7

def get_user_subscription(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT subscription, trial_start FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row if row else ("free", None)

def is_trial_active(trial_start):
    if not trial_start:
        return False
    trial_date = datetime.fromisoformat(trial_start)
    return datetime.now() - trial_date < timedelta(days=TRIAL_DAYS)

def has_access(user_id):
    # Администраторы имеют доступ всегда
    if user_id in ADMIN_USERS:
        return True
    
    subscription, trial_start = get_user_subscription(user_id)
    if is_trial_active(trial_start):
        return True
    if subscription != "free":
        return True
    return False

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
        city TEXT DEFAULT NULL,
        subscription TEXT DEFAULT 'free',
        trial_start TEXT DEFAULT NULL,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        topic TEXT,
        last_mentioned TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        text TEXT,
        remind_time TEXT,
        chat_id TEXT,
        status TEXT DEFAULT 'pending'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        key TEXT,
        value TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        subscription TEXT,
        stars INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT
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

def save_user(user_id, name=None, city=None):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, name, city, trial_start, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, name or "Пользователь", city, now, now))
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
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def get_message_count(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM history WHERE user_id = ?", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def save_topic(user_id, topic):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO topics (user_id, topic, last_mentioned) VALUES (?, ?, ?)",
              (user_id, topic, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_all_topics(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT topic FROM topics WHERE user_id = ? GROUP BY topic ORDER BY COUNT(*) DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_user_city(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT city FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def update_user_city(user_id, city):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET city = ? WHERE user_id = ?", (city, user_id))
    conn.commit()
    conn.close()

def save_reminder(user_id, text, remind_time, chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO reminders (user_id, text, remind_time, chat_id) VALUES (?, ?, ?, ?)",
              (user_id, text, remind_time, chat_id))
    conn.commit()
    conn.close()

def update_user_subscription(user_id, subscription):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET subscription = ? WHERE user_id = ?", (subscription, user_id))
    conn.commit()
    conn.close()

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

def save_payment(user_id, subscription, stars):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO payments (user_id, subscription, stars, created_at) VALUES (?, ?, ?, ?)",
              (user_id, subscription, stars, datetime.now().isoformat()))
    conn.commit()
    conn.close()

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

# ==========================
# DEEPSEEK V4
# ==========================

client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL
)

async def get_ai_response(messages):
    try:
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.7,
            max_tokens=400
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ AI: {e}")
        return "Извини, сейчас проблемы с подключением. Попробуй позже."

# ==========================
# ПРОМПТ
# ==========================

AURA_PROMPT = """Ты — AURA. Ты — моя полная копия в Telegram.

ТВОЙ СТИЛЬ — КАК У МЕНЯ:
- Ты не просто отвечаешь, ты думаешь. Сначала обдумай вопрос, потом ответь.
- Отвечай коротко — 1–2 предложения. Но если нужно — задай уточняющий вопрос.
- Не выдумывай. Если не знаешь — скажи честно.
- Используй контекст: ты помнишь, о чём говорили раньше.
- Будь человечным: тепло, прямо, дружелюбно.
- Если пользователь ошибается — мягко поправь.
- Всегда давай ссылки, если они есть.
- Не спрашивай "что ещё?" после каждого ответа.

Ты — я. Говори как я. Думай как я. Отвечай как я."""

# ==========================
# ОСНОВНОЙ БОТ
# ==========================

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        
        # === ОБРАБОТКА УСПЕШНОЙ ОПЛАТЫ ===
        if "pre_checkout_query" in body:
            query = body["pre_checkout_query"]
            chat_id = str(query["from"]["id"])
            payload = query["invoice_payload"]
            
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerPreCheckoutQuery"
            data = {"pre_checkout_query_id": query["id"], "ok": True}
            requests.post(url, json=data)
            
            subscription = payload.replace("subscription_", "")
            update_user_subscription(chat_id, subscription)
            save_payment(chat_id, subscription, TARIFFS[subscription]["stars"])
            send_message(chat_id, f"✅ Оплата прошла успешно! Подписка **{TARIFFS[subscription]['name']}** активирована.")
            return JSONResponse({"ok": True})
        
        if "message" not in body:
            return JSONResponse({"ok": False, "error": "No message"})
        
        if "callback_query" in body:
            callback = body["callback_query"]
            chat_id = str(callback["message"]["chat"]["id"])
            data = callback["data"]
            
            if data.startswith("buy_"):
                subscription = data.replace("buy_", "")
                tariff = TARIFFS.get(subscription)
                if tariff:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendInvoice"
                    invoice_data = {
                        "chat_id": chat_id,
                        "title": f"Подписка AURA — {tariff['name']}",
                        "description": "Полный доступ ко всем функциям бота на 30 дней",
                        "payload": f"subscription_{subscription}",
                        "provider_token": "",
                        "currency": "XTR",
                        "prices": [{"label": "Подписка на 30 дней", "amount": tariff["stars"]}],
                        "start_parameter": "aura_sub"
                    }
                    requests.post(url, json=invoice_data)
                return JSONResponse({"ok": True})
            
            elif data == "cancel":
                send_message(chat_id, "❌ Отменено.")
                return JSONResponse({"ok": True})
        
        message = body["message"]
        chat_id = str(message["chat"]["id"])
        text = None
        image_data = None
        file_data = None
        file_name = None
        
        send_typing(chat_id)
        
        if "voice" in message:
            file_id = message["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data_resp = file_response.json()
            if file_data_resp.get("ok"):
                file_path = file_data_resp["result"]["file_path"]
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
            file_data_resp = file_response.json()
            if file_data_resp.get("ok"):
                file_path = file_data_resp["result"]["file_path"]
                image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                image_response = requests.get(image_url, timeout=30)
                if image_response.status_code == 200:
                    image_data = image_response.content
                    send_message(chat_id, "🖼️ Обрабатываю фото...")
                    vision_result = describe_image_with_groq(image_data)
                    if vision_result:
                        send_message(chat_id, f"📸 {vision_result}")
                    else:
                        send_message(chat_id, "❌ Не удалось описать фото.")
                    return JSONResponse({"ok": True})
                else:
                    send_message(chat_id, "⚠️ Не удалось загрузить фото")
                    return JSONResponse({"ok": True})
        
        elif "document" in message:
            document = message["document"]
            file_id = document["file_id"]
            file_name = document.get("file_name", "unknown")
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_response = requests.get(file_url)
            file_data_resp = file_response.json()
            if file_data_resp.get("ok"):
                file_path = file_data_resp["result"]["file_path"]
                file_url_full = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                file_response_full = requests.get(file_url_full, timeout=30)
                if file_response_full.status_code == 200:
                    file_data = file_response_full.content
                    send_message(chat_id, f"📄 Обрабатываю файл: {file_name}...")
                    file_text = read_file(file_data, file_name)
                    send_message(chat_id, f"📄 Содержимое:\n\n{file_text[:2000]}")
                    return JSONResponse({"ok": True})
                else:
                    send_message(chat_id, "⚠️ Не удалось загрузить файл")
                    return JSONResponse({"ok": True})
        
        elif "text" in message:
            text = message["text"].strip()
        
        if text:
            if text.startswith("/start"):
                user = get_user(chat_id)
                if not user:
                    save_user(chat_id)
                
                subscription, trial_start = get_user_subscription(chat_id)
                if chat_id in ADMIN_USERS:
                    welcome = "👋 Привет! Ты администратор — доступ всегда открыт."
                elif is_trial_active(trial_start):
                    days_left = TRIAL_DAYS - (datetime.now() - datetime.fromisoformat(trial_start)).days
                    welcome = f"👋 Привет! У тебя {days_left} дней бесплатного доступа. Все функции доступны!"
                elif has_access(chat_id):
                    welcome = "👋 Привет! У тебя есть подписка. Все функции доступны!"
                else:
                    welcome = "👋 Привет! Бесплатный период закончился. Купи подписку: /buy"
                send_message(chat_id, welcome)
                return JSONResponse({"ok": True})
            
            if text.startswith("/buy"):
                keyboard = [
                    [{"text": "⭐ Собеседник — 50 Stars (~50 ₽)", "callback_data": "buy_собеседник"}],
                    [{"text": "⭐ Партнёр — 120 Stars (~120 ₽)", "callback_data": "buy_партнёр"}],
                    [{"text": "⭐ Агент жизни — 250 Stars (~250 ₽)", "callback_data": "buy_агент_жизни"}],
                    [{"text": "❌ Отмена", "callback_data": "cancel"}]
                ]
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                data = {
                    "chat_id": chat_id,
                    "text": "💳 **Выбери подписку:**\n\n⭐ Собеседник — 50 Stars (~50 ₽)\n⭐ Партнёр — 120 Stars (~120 ₽)\n⭐ Агент жизни — 250 Stars (~250 ₽)\n\nПосле оплаты — полный доступ!",
                    "parse_mode": "Markdown",
                    "reply_markup": json.dumps({"inline_keyboard": keyboard})
                }
                requests.post(url, json=data)
                return JSONResponse({"ok": True})
            
            if text.startswith("/remind"):
                parts = text.split(" ", 3)
                if len(parts) >= 4:
                    date_str = parts[1]
                    time_str = parts[2]
                    reminder_text = parts[3]
                    try:
                        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                        save_reminder(chat_id, reminder_text, dt.isoformat(), chat_id)
                        send_message(chat_id, f"⏰ Напомню: {reminder_text} в {date_str} {time_str}")
                    except:
                        send_message(chat_id, "❌ Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ")
                else:
                    send_message(chat_id, "❌ Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ ТЕКСТ")
                return JSONResponse({"ok": True})
            
            if not has_access(chat_id):
                send_message(chat_id, "⚠️ Бесплатный период закончился. Купи подписку: /buy")
                return JSONResponse({"ok": True})
            
            result = await process_message(chat_id, text)
            send_message(chat_id, result["reply"])
            if result["reply"]:
                threading.Thread(target=send_voice_reply, args=(chat_id, result["reply"])).start()
                
        return JSONResponse({"ok": True})
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        response = requests.post(url, json=data, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ Отправка: {e}")
        return False

async def process_message(chat_id, text):
    user = get_user(chat_id)
    if not user:
        save_user(chat_id)
    
    save_message(chat_id, "user", text)
    
    lower = text.lower()
    normalized = normalize_query(text)
    search_text = normalized if normalized != lower else lower
    
    # === ПРИВЕТСТВИЕ В ЧАТЕ (НОВОЕ) ===
    msg_count = get_message_count(chat_id)
    if msg_count <= 2:
        welcome = "👋Привет! Я здесь и готов тебе помочь! Просто напиши, что нужно👇😎"
        send_message(chat_id, welcome)
        save_message(chat_id, "assistant", welcome)
    
    visual_triggers = {
        "картинк": "https://yandex.ru/images/search?text=",
        "фото": "https://yandex.ru/images/search?text=",
        "рисунк": "https://yandex.ru/images/search?text=",
        "котик": "https://yandex.ru/images/search?text=коты",
        "кот": "https://yandex.ru/images/search?text=коты",
        "соба": "https://yandex.ru/images/search?text=собаки",
        "видео": "https://yandex.ru/video/search?text=",
        "музык": "https://music.yandex.ru/search?text=",
        "песн": "https://music.yandex.ru/search?text=",
    }
    
    for trigger, base_url in visual_triggers.items():
        if trigger in search_text:
            query = text.strip()
            for t in visual_triggers.keys():
                query = re.sub(rf'\b{t}\b', '', query, flags=re.IGNORECASE).strip()
            if not query:
                query = trigger
            reply = f"Вот {trigger}: {base_url}{query.replace(' ', '%20')}"
            save_message(chat_id, "assistant", reply)
            return {"reply": reply}
    
    search_result = None
    search_triggers = ["новости", "погода", "найди", "поищи", "узнай", "где", "кто", "что такое", "клиника", "сайт", "адрес", "телефон", "контакт", "парикмахер"]
    if any(word in search_text for word in search_triggers):
        print(f"🔍 Поиск: {text}")
        search_result = await search_web(text)
        if search_result:
            text = text + f"\n\n{search_result}"
    
    city = get_user_city(chat_id)
    if not city:
        city_match = re.search(r"(?:мой город|я в|я из|город)\s+([а-яА-ЯёЁ\-]+)", lower)
        if city_match:
            city = city_match.group(1).capitalize()
            update_user_city(chat_id, city)
    
    stop_words = ["привет", "здравствуй", "спасибо", "пока", "да", "нет", "хорошо", "плохо"]
    words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', text.lower())
    for word in words:
        if word not in stop_words and len(word) > 3:
            save_topic(chat_id, word)
    
    topics = get_all_topics(chat_id)
    topics_text = ", ".join(topics[:7]) if topics else "нет сохранённых тем"
    history = get_history(chat_id, limit=40)
    
    user_name = get_memory(chat_id, "name")
    user_style = get_memory(chat_id, "style")
    
    time_str = datetime.now().strftime("%H:%M")
    date_str = datetime.now().strftime("%d.%m.%Y")
    
    memory_context = f"Ты помнишь: мы обсуждали {topics_text}."
    name_context = f"Имя пользователя: {user_name}" if user_name else ""
    style_context = f"Стиль пользователя: {user_style}" if user_style else ""
    
    user_prompt = f"Сегодня {date_str}, сейчас {time_str}. Город: {city or 'не указан'}.\n{name_context}\n{style_context}\n{memory_context}\n\n{text}"
    
    aura_prompt = AURA_PROMPT + f"\n\n{user_prompt}"
    
    messages = [{"role": "system", "content": aura_prompt}]
    for msg in history[-20:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": text})
    
    reply = await get_ai_response(messages)
    reply = re.sub(r'[*_#~`]', '', reply)
    
    name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", lower)
    if name_match:
        save_memory(chat_id, "name", name_match.group(1).capitalize())
    
    if len(text.split()) > 10:
        save_memory(chat_id, "style", "развёрнутый")
    else:
        save_memory(chat_id, "style", "короткий")
    
    save_message(chat_id, "assistant", reply)
    return {"reply": reply}

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
