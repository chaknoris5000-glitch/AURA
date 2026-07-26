import os
import re
import time
import sqlite3
import asyncio
import requests
from datetime import datetime, timedelta
import random
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
from openai import AsyncOpenAI
from tavily import TavilyClient
from groq import Groq
from gtts import gTTS
from bs4 import BeautifulSoup

# Загрузка переменных
load_dotenv()

# ==================== НАСТРОЙКИ ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")

ADMIN_USERS = ["5818548555"]

# Инициализация клиентов
deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"
)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# ==================== FASTAPI ====================
app = FastAPI()

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect('aura.db')
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            city TEXT,
            city_set INTEGER DEFAULT 0,
            subscription_type TEXT DEFAULT 'free',
            subscription_end DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            mood TEXT DEFAULT 'neutral',
            name TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            task TEXT,
            due_date TIMESTAMP,
            done INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def clean_text_for_voice(text):
    """Очищает текст от эмодзи, ссылок, номеров телефонов"""
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F700-\U0001F77F"
        "\U0001F780-\U0001F7FF"
        "\U0001F800-\U0001F8FF"
        "\U0001F900-\U0001F9FF"
        "\U0001FA00-\U0001FA6F"
        "\U0001FA70-\U0001FAFF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'\+?\d[\d\s\-\(\)]{7,}\d', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def get_user(chat_id):
    conn = sqlite3.connect('aura.db')
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    user = cur.fetchone()
    conn.close()
    return user

def create_user(chat_id, username=None, first_name=None):
    conn = sqlite3.connect('aura.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT OR IGNORE INTO users (chat_id, username, first_name)
        VALUES (?, ?, ?)
    ''', (chat_id, username, first_name))
    conn.commit()
    conn.close()

def update_user_city(chat_id, city):
    conn = sqlite3.connect('aura.db')
    cur = conn.cursor()
    cur.execute("UPDATE users SET city = ?, city_set = 1 WHERE chat_id = ?", (city, chat_id))
    conn.commit()
    conn.close()

def save_message(chat_id, role, content):
    conn = sqlite3.connect('aura.db')
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO messages (chat_id, role, content)
        VALUES (?, ?, ?)
    ''', (chat_id, role, content))
    conn.commit()
    conn.close()

def get_history(chat_id, limit=10):
    conn = sqlite3.connect('aura.db')
    cur = conn.cursor()
    cur.execute('''
        SELECT role, content FROM messages
        WHERE chat_id = ?
        ORDER BY created_at DESC LIMIT ?
    ''', (chat_id, limit))
    history = cur.fetchall()
    conn.close()
    return list(reversed(history))

def analyze_mood(text):
    sad_words = ['грустн', 'печаль', 'тоск', 'плак', 'слёз', 'депрес']
    anxious_words = ['тревож', 'волну', 'страх', 'боюс', 'паник']
    happy_words = ['счаст', 'радост', 'весел', 'класс', 'отличн', 'супер']
    tired_words = ['устал', 'утом', 'спат', 'сон', 'вымота']
    
    text_lower = text.lower()
    if any(word in text_lower for word in sad_words):
        return 'sad'
    elif any(word in text_lower for word in anxious_words):
        return 'anxious'
    elif any(word in text_lower for word in happy_words):
        return 'happy'
    elif any(word in text_lower for word in tired_words):
        return 'tired'
    else:
        return 'neutral'

def check_subscription(chat_id):
    user = get_user(chat_id)
    if not user:
        return False
    if str(chat_id) in ADMIN_USERS:
        return True
    subscription_type = user[5]
    subscription_end = user[6]
    if subscription_type == 'free':
        return False
    if subscription_end:
        end_date = datetime.strptime(subscription_end, '%Y-%m-%d')
        if datetime.now().date() <= end_date.date():
            return True
    return False

def send_telegram_message(chat_id, text):
    """Отправка сообщения через Telegram API напрямую"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
        response = requests.post(url, data=data)
        return response.json()
    except Exception as e:
        print(f"Send message error: {e}")
        return None

def send_telegram_voice(chat_id, text):
    """Отправка голосового через Telegram API напрямую"""
    try:
        clean_text = clean_text_for_voice(text)
        if not clean_text or len(clean_text) < 5:
            return None
        
        tts = gTTS(text=clean_text, lang='ru', slow=False)
        audio_file = f"voice_{chat_id}_{int(time.time())}.mp3"
        tts.save(audio_file)
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVoice"
        with open(audio_file, 'rb') as f:
            files = {'voice': f}
            data = {'chat_id': chat_id}
            response = requests.post(url, files=files, data=data)
        
        os.remove(audio_file)
        return response.json()
    except Exception as e:
        print(f"Voice send error: {e}")
        return None

# ==================== ПОИСК ====================

async def search_web(query):
    results = []
    try:
        tavily_results = tavily_client.search(query, max_results=5)
        for result in tavily_results.get('results', []):
            results.append({
                'title': result.get('title', ''),
                'content': result.get('content', ''),
                'url': result.get('url', '')
            })
    except Exception as e:
        print(f"Tavily error: {e}")
    
    if len(results) < 3:
        try:
            ddg_url = f"https://html.duckduckgo.com/html/?q={query}"
            response = requests.get(ddg_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            for result in soup.select('.result')[:3]:
                title_elem = result.select_one('.result__a')
                snippet_elem = result.select_one('.result__snippet')
                if title_elem and snippet_elem:
                    results.append({
                        'title': title_elem.text,
                        'content': snippet_elem.text,
                        'url': title_elem.get('href', '')
                    })
        except Exception as e:
            print(f"DuckDuckGo error: {e}")
    return results

# ==================== ОСНОВНАЯ ЛОГИКА ====================

AURA_PROMPT = """Ты — AURA. Ты — мой цифровой партнёр в Telegram.
- Ты чувствуешь настроение собеседника и адаптируешься.
- Отвечай как человек: тепло, прямо, с эмпатией.
- Всегда в конце добавляй вопрос, чтобы продолжить диалог.
- Используй контекст прошлых диалогов.
- Отвечай коротко (1-2 предложения), если не просят подробнее."""

async def get_aura_response(chat_id, text):
    user = get_user(chat_id)
    
    if not check_subscription(chat_id) and str(chat_id) not in ADMIN_USERS:
        return "🔒 У тебя закончился пробный период. Оформи подписку, чтобы продолжить общение."
    
    save_message(chat_id, 'user', text)
    
    mood = analyze_mood(text)
    conn = sqlite3.connect('aura.db')
    cur = conn.cursor()
    cur.execute("UPDATE users SET mood = ?, last_active = CURRENT_TIMESTAMP WHERE chat_id = ?", (mood, chat_id))
    conn.commit()
    conn.close()
    
    history = get_history(chat_id, 10)
    messages = [{"role": "system", "content": AURA_PROMPT}]
    
    if user:
        user_info = []
        if user[3]:
            user_info.append(f"Город: {user[3]}")
        if user[8]:
            user_info.append(f"Настроение: {user[8]}")
        if user_info:
            messages.append({"role": "system", "content": f"Информация о пользователе: {', '.join(user_info)}"})
    
    for role, content in history[-5:]:
        messages.append({"role": role, "content": content})
    
    messages.append({"role": "user", "content": text})
    
    search_keywords = ['найди', 'поищи', 'что такое', 'кто такой', 'где', 'сколько', 'погода', 'новости']
    if any(keyword in text.lower() for keyword in search_keywords):
        search_results = await search_web(text)
        if search_results:
            context = "Результаты поиска:\n"
            for i, result in enumerate(search_results[:3], 1):
                context += f"{i}. {result['title']}\n{result['content'][:200]}\n"
            messages.append({"role": "system", "content": context})
    
    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )
        reply = response.choices[0].message.content
        if len(reply) < 10:
            reply += " Что думаешь? Хочешь, продолжу?"
        if not reply.endswith("?") and not reply.endswith("..."):
            reply += " Что думаешь?"
        save_message(chat_id, 'assistant', reply)
        return reply
    except Exception as e:
        print(f"DeepSeek error: {e}")
        return "Извини, сейчас проблемы с подключением. Попробуй позже."

# ==================== WEBHOOK ОБРАБОТЧИК ====================

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        print(f"Webhook received: {json.dumps(data, ensure_ascii=False)[:200]}")
        
        # Обрабатываем сообщение
        if 'message' in data:
            message = data['message']
            chat_id = str(message['chat']['id'])
            
            # Текстовое сообщение
            if 'text' in message:
                text = message['text']
                
                # Проверяем команды
                if text.startswith('/start'):
                    reply = "👋 Привет! Я AURA. Напиши свой город."
                    send_telegram_message(chat_id, reply)
                    return {"status": "ok"}
                
                if text.startswith('/help'):
                    reply = "🤖 Я умею: общаться, искать информацию, напоминать, анализировать настроение, отвечать голосом."
                    send_telegram_message(chat_id, reply)
                    return {"status": "ok"}
                
                # Обработка города
                user = get_user(chat_id)
                if not user:
                    create_user(chat_id, message['chat'].get('username'), message['chat'].get('first_name'))
                    user = get_user(chat_id)
                
                if user and user[4] == 0:
                    if len(text) < 30 and not any(kw in text.lower() for kw in ['привет', 'здравствуй']):
                        update_user_city(chat_id, text)
                        reply = f"✅ Город {text} сохранён! Чем могу помочь?"
                        send_telegram_message(chat_id, reply)
                        send_telegram_voice(chat_id, reply)
                        return {"status": "ok"}
                    else:
                        send_telegram_message(chat_id, "📍 Напиши свой город.")
                        return {"status": "ok"}
                
                # Основной ответ
                reply = await get_aura_response(chat_id, text)
                send_telegram_message(chat_id, reply)
                send_telegram_voice(chat_id, reply)
            
            # Голосовое сообщение
            elif 'voice' in message:
                # Получаем файл
                file_id = message['voice']['file_id']
                file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
                file_response = requests.get(file_url).json()
                
                if file_response.get('ok'):
                    file_path = file_response['result']['file_path']
                    file_download = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                    
                    # Скачиваем
                    audio_response = requests.get(file_download)
                    audio_file = f"voice_in_{chat_id}_{int(time.time())}.ogg"
                    with open(audio_file, 'wb') as f:
                        f.write(audio_response.content)
                    
                    # Распознаём через Groq
                    try:
                        with open(audio_file, 'rb') as f:
                            transcription = groq_client.audio.transcriptions.create(
                                file=f,
                                model="whisper-large-v3-turbo",
                                response_format="text"
                            )
                        text = transcription
                        os.remove(audio_file)
                        
                        if text:
                            send_telegram_message(chat_id, f"🎤 Распознано: {text}")
                            reply = await get_aura_response(chat_id, text)
                            send_telegram_message(chat_id, reply)
                            send_telegram_voice(chat_id, reply)
                    except Exception as e:
                        print(f"Transcription error: {e}")
                        send_telegram_message(chat_id, "Не удалось распознать голосовое.")
        
        return {"status": "ok"}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"status": "error"}

# ==================== ФОНОВЫЕ ЗАДАЧИ ====================

async def check_inactivity():
    while True:
        await asyncio.sleep(3600)
        try:
            conn = sqlite3.connect('aura.db')
            cur = conn.cursor()
            cur.execute('''
                SELECT chat_id FROM users
                WHERE last_active < datetime('now', '-24 hours')
                AND last_active > datetime('now', '-48 hours')
            ''')
            inactive_users = cur.fetchall()
            conn.close()
            
            for user in inactive_users:
                chat_id = user[0]
                reply = random.choice([
                    "👋 Давно не общались! Как дела?",
                    "Привет! Скучал по нашему общению!",
                    "Эй! Долго не писал. Всё хорошо?"
                ])
                send_telegram_message(chat_id, reply)
                send_telegram_voice(chat_id, reply)
        except Exception as e:
            print(f"Inactivity error: {e}")

async def check_reminders():
    while True:
        await asyncio.sleep(60)
        try:
            conn = sqlite3.connect('aura.db')
            cur = conn.cursor()
            cur.execute('''
                SELECT id, chat_id, task FROM tasks
                WHERE due_date <= datetime('now')
                AND done = 0
            ''')
            tasks = cur.fetchall()
            for task_id, chat_id, task in tasks:
                reply = f"⏰ Напоминание: {task}"
                send_telegram_message(chat_id, reply)
                send_telegram_voice(chat_id, reply)
                cur.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
                conn.commit()
            conn.close()
        except Exception as e:
            print(f"Reminder error: {e}")

# ==================== ЗАПУСК ====================

@app.on_event("startup")
async def startup():
    print("🚀 AURA запускается...")
    asyncio.create_task(check_inactivity())
    asyncio.create_task(check_reminders())
    
    # Устанавливаем webhook
    webhook_url = "https://aura-zatq.onrender.com/webhook"
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
    response = requests.get(set_url)
    print(f"✅ Webhook set: {response.json()}")

@app.get("/")
async def root():
    return {"status": "AURA is running", "version": "3.0"}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
