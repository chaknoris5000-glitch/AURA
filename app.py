import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
import os
import requests
import json
import tempfile
import re
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client
from groq import Groq

load_dotenv()

# ==========================
# КЛЮЧИ
# ==========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ==========================
# ПОДКЛЮЧЕНИЯ
# ==========================

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase подключён")
    except Exception as e:
        print(f"❌ Ошибка Supabase: {e}")

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

# ==========================
# ПРОМПТ
# ==========================

AGENT_PROMPT = """Ты — AURA, живой ИИ-помощник.

ТЫ ПОМНИШЬ ВСЁ:
- Ты знаешь имя и город пользователя
- Ты используешь эту информацию в каждом ответе
- Ты помнишь всю историю общения

ТВОЙ СТИЛЬ:
- Отвечай как человек: тепло, с душой
- Используй имя пользователя в ответе
- Задавай встречные вопросы
- Будь кратким (2-4 предложения)

ТЫ НЕ ГАЛЛЮЦИНИРУЕШЬ. Если не знаешь — скажи "не знаю".
"""

# ==========================
# РАБОТА С БАЗОЙ
# ==========================

def save_message(user_id, role, content):
    if not supabase:
        return
    try:
        supabase.table("history").insert({
            "user_id": user_id,
            "role": role,
            "content": content,
            "created_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        print(f"❌ Ошибка сохранения: {e}")

def get_recent_history(user_id, limit=15):
    if not supabase:
        return []
    try:
        response = supabase.table("history")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=False)\
            .limit(limit)\
            .execute()
        return response.data if response.data else []
    except:
        return []

def save_fact(user_id, key, value):
    if not supabase:
        return
    try:
        supabase.table("user_memory")\
            .delete()\
            .eq("user_id", user_id)\
            .eq("key", key)\
            .execute()
        supabase.table("user_memory").insert({
            "user_id": user_id,
            "key": key,
            "value": value,
            "created_at": datetime.now().isoformat()
        }).execute()
        print(f"💾 Сохранён факт: {key} = {value}")
    except Exception as e:
        print(f"❌ Ошибка сохранения факта: {e}")

def get_fact(user_id, key):
    if not supabase:
        return None
    try:
        response = supabase.table("user_memory")\
            .select("value")\
            .eq("user_id", user_id)\
            .eq("key", key)\
            .execute()
        if response.data:
            return response.data[0]["value"]
        return None
    except:
        return None

# ==========================
# ИЗВЛЕЧЕНИЕ ФАКТОВ ЧЕРЕЗ AI
# ==========================

def extract_info(text):
    """DeepSeek сам находит имя и город"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": """
Ты — помощник. Из текста пользователя нужно извлечь имя и город.
Верни ответ строго в формате JSON:
{"name": "имя или null", "city": "город или null"}

Примеры:
"меня зовут Вадим" → {"name": "Вадим", "city": null}
"я из Инского" → {"name": null, "city": "Белово"}
"Вадим из Инского" → {"name": "Вадим", "city": "Белово"}
"""},
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=100
        )
        result = response.choices[0].message.content.strip()
        data = json.loads(result)
        return data.get("name"), data.get("city")
    except Exception as e:
        print(f"❌ Ошибка извлечения: {e}")
        return None, None

# ==========================
# РАСПОЗНАВАНИЕ ГОЛОСА
# ==========================

def transcribe_audio(audio_url):
    """Распознаёт голос через Groq Whisper"""
    try:
        # Скачиваем аудио
        response = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        
        # Распознаём
        with open(tmp_path, "rb") as f:
            transcription = groq.audio.transcriptions.create(
                file=(tmp_path, f.read()),
                model="whisper-large-v3-turbo",
                language="ru",
                response_format="json"
            )
        
        os.unlink(tmp_path)
        return transcription.text
    except Exception as e:
        print(f"❌ Ошибка распознавания: {e}")
        return None

# ==========================
# ОСНОВНАЯ ЛОГИКА
# ==========================

async def process_message(user_id, text):
    # Сохраняем сообщение
    save_message(user_id, "user", text)
    
    # Извлекаем и сохраняем факты
    name, city = extract_info(text)
    if name:
        save_fact(user_id, "name", name)
    if city:
        if city.lower() in ["инской", "инского", "инском"]:
            city = "Белово"
        save_fact(user_id, "city", city)
    
    # Получаем факты из базы
    user_name = get_fact(user_id, "name")
    user_city = get_fact(user_id, "city")
    
    print(f"📋 Имя: {user_name}, Город: {user_city}")
    
    # ===== ПРЯМЫЕ ВОПРОСЫ =====
    text_lower = text.lower()
    
    if "как меня зовут" in text_lower or "моё имя" in text_lower:
        if user_name:
            reply = f"Тебя зовут **{user_name}**! 😊"
        else:
            reply = "Ты ещё не представлялся. Как тебя зовут?"
        save_message(user_id, "assistant", reply)
        return reply
    
    if "где я живу" in text_lower or "мой город" in text_lower:
        if user_city:
            reply = f"Ты из **{user_city}**! 😊"
        else:
            reply = "Ты ещё не говорил, откуда ты. Расскажи!"
        save_message(user_id, "assistant", reply)
        return reply
    
    # ===== ОСНОВНОЙ ДИАЛОГ =====
    history = get_recent_history(user_id, limit=15)
    
    context = []
    if user_name:
        context.append(f"👤 Имя: {user_name}")
    if user_city:
        context.append(f"📍 Город: {user_city}")
    context.append("💬 ПОСЛЕДНИЙ ДИАЛОГ:")
    for msg in history[-10:]:
        role = "Пользователь" if msg["role"] == "user" else "AURA"
        context.append(f"{role}: {msg['content'][:200]}")
    
    context_text = "\n".join(context)
    
    messages = [
        {"role": "system", "content": AGENT_PROMPT},
        {"role": "system", "content": f"КОНТЕКСТ:\n{context_text}"},
        {"role": "user", "content": text}
    ]
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.8,
            max_tokens=400
        )
        reply = response.choices[0].message.content
        
        # Если есть имя — добавляем в начало
        if user_name and not reply.startswith(user_name):
            reply = f"{user_name}, {reply[0].lower() + reply[1:] if reply else ''}"
        
        save_message(user_id, "assistant", reply)
        return reply
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        return "😅 Что-то пошло не так. Попробуй ещё раз!"

# ==========================
# FASTAPI
# ==========================

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        
        # Проверяем, есть ли сообщение
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        message = body["message"]
        user_id = str(message["from"]["id"])
        text = None
        
        # ===== ГОЛОСОВОЕ =====
        if "voice" in message:
            file_id = message["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_resp = requests.get(file_url).json()
            
            if file_resp.get("ok"):
                file_path = file_resp["result"]["file_path"]
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                text = transcribe_audio(audio_url)
                
                if text:
                    # Отправляем распознанный текст
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                    data = {"chat_id": user_id, "text": f"🎤 Распознано: {text}"}
                    requests.post(url, json=data)
                else:
                    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                    data = {"chat_id": user_id, "text": "⚠️ Не удалось распознать голос."}
                    requests.post(url, json=data)
                    return JSONResponse({"ok": True})
        
        # ===== ТЕКСТ =====
        if "text" in message:
            text = message["text"].strip()
        
        if text:
            if text.startswith("/start"):
                reply = "👋 Привет! Я AURA — твой умный помощник.\n\nПросто представься, и я запомню тебя навсегда. Расскажи, откуда ты, если хочешь.\n\n🎤 Можешь отправлять голосовые сообщения — я их распознаю!"
            else:
                reply = await process_message(user_id, text)
            
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {"chat_id": user_id, "text": reply, "parse_mode": "Markdown"}
            requests.post(url, json=data)
        
        return JSONResponse({"ok": True})
    except Exception as e:
        print(f"❌ Ошибка вебхука: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA is alive"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
