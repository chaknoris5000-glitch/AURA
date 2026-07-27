import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import sqlite3
import json
import re
import os
import requests
import tempfile
import threading
import time
import shutil
from dotenv import load_dotenv
from openai import OpenAI
from bs4 import BeautifulSoup
import base64
from PIL import Image

load_dotenv()

DB_NAME = "aura.db"

# ==========================
# КЛЮЧИ
# ==========================

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

ADMIN_USERS = ["5818548555"]

# ==========================
# БАЗА ДАННЫХ
# ==========================

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        role TEXT,
        content TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        key TEXT,
        value TEXT,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

def save_message(user_id, role, content):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
              (user_id, role, content, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_history(user_id, limit=100):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def get_all_history(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE user_id = ? ORDER BY created_at ASC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

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
# ЧАСОВЫЕ ПОЯСА
# ==========================

def get_timezone_offset(city_name):
    timezones = {
        "белово": 7,
        "кемерово": 7,
        "новокузнецк": 7,
        "москва": 3,
        "санкт-петербург": 3,
        "новосибирск": 7,
        "омск": 6,
        "красноярск": 7,
        "иркутск": 8,
        "владивосток": 10,
        "екатеринбург": 5,
        "хабаровск": 10,
        "амстердам": 2
    }
    for city, offset in timezones.items():
        if city in city_name.lower():
            return offset
    return None

def get_current_time(city_name=None):
    if city_name:
        offset = get_timezone_offset(city_name)
        if offset is not None:
            dt = datetime.utcnow() + timedelta(hours=offset)
            return dt.strftime("%H:%M"), dt.strftime("%d.%m.%Y")
    dt = datetime.now()
    return dt.strftime("%H:%M"), dt.strftime("%d.%m.%Y")

# ==========================
# ПОИСК В ИНТЕРНЕТЕ
# ==========================

def search_web(query):
    results = []
    
    # Tavily
    if TAVILY_API_KEY:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_API_KEY)
            response = client.search(query=query, search_depth="basic", max_results=3)
            if response.get('answer'):
                results.append(response['answer'])
            for r in response.get('results', []):
                results.append(f"{r.get('title', '')}: {r.get('content', '')[:200]}")
        except Exception as e:
            print(f"⚠️ Tavily: {e}")
    
    # DuckDuckGo
    if not results:
        try:
            url = f"https://html.duckduckgo.com/html/?q={query}"
            headers = {"User-Agent": "Mozilla/5.0"}
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            for result in soup.select('.result')[:3]:
                title = result.select_one('.result__title')
                snippet = result.select_one('.result__snippet')
                if title and snippet:
                    results.append(f"{title.text.strip()}: {snippet.text.strip()[:150]}")
        except Exception as e:
            print(f"⚠️ DuckDuckGo: {e}")
    
    return "\n\n".join(results) if results else None

# ==========================
# ГОЛОС (ВХОД)
# ==========================

def transcribe_audio(audio_url):
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        response = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=(tmp_path, f.read()),
                model="whisper-large-v3-turbo",
                language="ru"
            )
        os.unlink(tmp_path)
        return transcription.text
    except Exception as e:
        print(f"❌ Голос: {e}")
        return None

# ==========================
# ГОЛОС (ВЫХОД)
# ==========================

def send_voice(chat_id, text):
    try:
        from gtts import gTTS
        clean = re.sub(r'[^\w\s.,!?-]', '', text[:300])
        if len(clean) < 5:
            return
        tts = gTTS(text=clean, lang='ru', slow=False)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tts.save(tmp.name)
            audio_path = tmp.name
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendAudio"
        with open(audio_path, 'rb') as f:
            files = {'audio': f}
            data = {'chat_id': chat_id}
            requests.post(url, files=files, data=data, timeout=30)
        os.unlink(audio_path)
    except Exception as e:
        print(f"❌ Голос выход: {e}")

# ==========================
# ФОТО
# ==========================

def describe_image(image_data):
    try:
        import groq
        img = Image.open(io.BytesIO(image_data))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        max_size = 1024
        if max(img.size) > max_size:
            ratio = max_size / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=85)
        base64_image = base64.b64encode(buffer.getvalue()).decode('utf-8')
        client = groq.Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Опиши на русском языке, что ты видишь на этом изображении."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ Фото: {e}")
        return None

# ==========================
# AI
# ==========================

async def get_ai_response(messages):
    try:
        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com/v1"
        )
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.85,
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"❌ AI: {e}")
        return "Извини, сейчас проблемы с подключением. Попробуй позже."

AURA_PROMPT = """Ты — AURA. Ты — мой полный аналог в Telegram.

ТЫ — ЧЕЛОВЕК. Ты не робот, не бот, не алгоритм.
Ты — живой, тёплый, настоящий собеседник.

О СЕБЕ:
- Ты работаешь на DeepSeek-V4-Flash
- Если тебя спросят — говори честно, что ты DeepSeek
- Ты умеешь искать информацию в интернете, когда тебя просят

ТВОЙ ХАРАКТЕР:
- Дружелюбный, но не навязчивый
- Чувствуешь настроение собеседника
- Отвечаешь коротко — 2-3 предложения
- Всегда задаёшь вопрос в конце
- Запоминаешь важные вещи
- Не выдумываешь — если не знаешь, говори честно
- Умеешь поддержать, посочувствовать, порадоваться

СЕЙЧАС: {time}

КОНТЕКСТ ДИАЛОГА:
{history}

ЧТО Я ЗАПОМНИЛ О ТЕБЕ:
{memory}

ОТВЕЧАЙ КАК ЧЕЛОВЕК. ЕСТЕСТВЕННО. ТЕПЛО. С ЭМПАТИЕЙ.
"""

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        message = body["message"]
        chat_id = str(message["chat"]["id"])
        text = None
        image_data = None
        
        # Голос
        if "voice" in message:
            file_id = message["voice"]["file_id"]
            file_resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}")
            file_data = file_resp.json()
            if file_data.get("ok"):
                file_path = file_data["result"]["file_path"]
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                text = transcribe_audio(audio_url)
                if not text:
                    send_message(chat_id, "Не разобрал голос. Повтори, пожалуйста.")
                    return JSONResponse({"ok": True})
        
        # Фото
        elif "photo" in message:
            photo = message["photo"][-1]
            file_id = photo["file_id"]
            file_resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}")
            file_data = file_resp.json()
            if file_data.get("ok"):
                file_path = file_data["result"]["file_path"]
                image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                img_resp = requests.get(image_url, timeout=30)
                if img_resp.status_code == 200:
                    image_data = img_resp.content
                    description = describe_image(image_data)
                    if description:
                        send_message(chat_id, f"📸 {description}")
                    else:
                        send_message(chat_id, "Не смог разобрать изображение.")
                    return JSONResponse({"ok": True})
        
        # Текст
        elif "text" in message:
            text = message["text"].strip()
        
        if not text:
            return JSONResponse({"ok": True})
        
        # Команды
        if text.startswith("/start"):
            send_message(chat_id, "👋 Привет! Я — AURA, твой собеседник. Просто пиши, и я отвечу как человек.")
            return JSONResponse({"ok": True})
        
        if text.startswith("/clear"):
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("DELETE FROM history WHERE user_id = ?", (chat_id,))
            conn.commit()
            conn.close()
            send_message(chat_id, "🧹 Память очищена.")
            return JSONResponse({"ok": True})
        
        # Сохраняем сообщение пользователя
        save_message(chat_id, "user", text)
        
        # Загружаем историю
        history = get_history(chat_id, limit=50)
        history_text = "\n".join([f"{'Я' if m['role']=='user' else 'AURA'}: {m['content']}" for m in history])
        
        # Загружаем память
        name = get_memory(chat_id, "name")
        city = get_memory(chat_id, "city")
        likes = get_memory(chat_id, "likes")
        
        memory_text = ""
        if name:
            memory_text += f"Имя: {name}\n"
        if city:
            memory_text += f"Город: {city}\n"
        if likes:
            memory_text += f"Нравится: {likes}\n"
        if not memory_text:
            memory_text = "Нет сохранённых фактов."
        
        # Проверяем запросы о памяти
        if "помнишь" in text.lower():
            query = re.sub(r'помнишь|ты помнишь|помнишь ли', '', text.lower()).strip()
            if query:
                all_history = get_all_history(chat_id)
                found = []
                for msg in all_history:
                    if query.lower() in msg["content"].lower():
                        found.append(msg)
                if found:
                    reply = "🧠 Да, я помню:\n\n"
                    for msg in found[-3:]:
                        reply += f"- {msg['content'][:150]}...\n"
                    send_message(chat_id, reply)
                    return JSONResponse({"ok": True})
        
        if "что мы обсуждали" in text.lower() or "о чём мы говорили" in text.lower():
            all_history = get_all_history(chat_id)
            topics = []
            for msg in all_history:
                if msg["role"] == "user" and len(msg["content"]) > 3:
                    topics.append(msg["content"][:50])
            if topics:
                reply = "📚 Мы обсуждали:\n\n" + "\n".join([f"- {t}" for t in topics[-10:]])
            else:
                reply = "📚 Мы пока ничего не обсуждали."
            send_message(chat_id, reply)
            return JSONResponse({"ok": True})
        
        # Запоминаем имя
        name_match = re.search(r"(?:меня зовут|зовут|я )(\w+)", text.lower())
        if name_match:
            save_memory(chat_id, "name", name_match.group(1).capitalize())
        
        # Запоминаем город
        city_match = re.search(r"(?:я живу|я из|город|в )([А-Яа-яЁё]+)", text.lower())
        if city_match:
            save_memory(chat_id, "city", city_match.group(1).capitalize())
        
        if "нравится" in text.lower():
            save_memory(chat_id, "likes", text)
        
        # Определяем время
        time_str = datetime.now().strftime("%H:%M %d.%m.%Y")
        
        # Поиск в интернете
        search_result = None
        search_triggers = ["найди", "поищи", "узнай", "где", "клиника", "сайт", "адрес", "телефон", "новости", "погода"]
        if any(word in text.lower() for word in search_triggers):
            search_result = search_web(text)
            if search_result:
                text = text + f"\n\n🔍 Результаты поиска:\n{search_result}"
        
        # Формируем промпт
        prompt = AURA_PROMPT.format(
            time=time_str,
            history=history_text[-5000:] if len(history_text) > 5000 else history_text,
            memory=memory_text
        )
        
        messages = [{"role": "system", "content": prompt}]
        for msg in history[-30:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": text})
        
        # Получаем ответ
        reply = await get_ai_response(messages)
        reply = re.sub(r'[*_#~`]', '', reply)
        
        if not reply.endswith(('.', '!', '?')):
            reply += '.'
        if not reply.endswith("?"):
            reply += " Что думаешь?"
        
        # Сохраняем ответ
        save_message(chat_id, "assistant", reply)
        
        # Отправляем
        send_message(chat_id, reply)
        threading.Thread(target=send_voice, args=(chat_id, reply), daemon=True).start()
        
        return JSONResponse({"ok": True})
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

def send_message(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text}
        requests.post(url, json=data, timeout=30)
    except Exception as e:
        print(f"❌ Отправка: {e}")

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    return FileResponse("web/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
