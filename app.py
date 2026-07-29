import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
import os
import requests
import re
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client

load_dotenv()

# ==========================
# КЛЮЧИ
# ==========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ==========================
# SUPABASE
# ==========================

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase подключён")
    except Exception as e:
        print(f"❌ Ошибка Supabase: {e}")

# ==========================
# DEEPSEEK
# ==========================

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ==========================
# ПРОМПТ
# ==========================

AGENT_PROMPT = """Ты — AURA, живой ИИ-помощник с ГИБРИДНОЙ ПАМЯТЬЮ.

ТЫ ПОМНИШЬ ВСЁ:
- Ты знаешь имя, город и предпочтения пользователя
- Ты используешь эту информацию в каждом ответе
- Ты НЕ галлюцинируешь — если не знаешь, скажи "не помню"

ТВОЙ СТИЛЬ:
- Отвечай как человек: тепло, с душой
- Используй имя пользователя в ответе
- Задавай встречные вопросы
- Будь кратким (2-4 предложения)

ТЫ НЕ ВЫДУМЫВАЕШЬ ИМЯ — ты берёшь его из фактов.
"""

# ==========================
# ФУНКЦИИ ПАМЯТИ
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
    except Exception as e:
        print(f"❌ Ошибка получения истории: {e}")
        return []

def get_user_facts(user_id):
    if not supabase:
        return {}
    try:
        response = supabase.table("user_memory")\
            .select("key, value")\
            .eq("user_id", user_id)\
            .execute()
        if response.data:
            return {item["key"]: item["value"] for item in response.data}
        return {}
    except Exception as e:
        print(f"❌ Ошибка получения фактов: {e}")
        return {}

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
    except Exception as e:
        print(f"❌ Ошибка сохранения факта: {e}")

def get_user(user_id):
    if not supabase:
        return None
    try:
        response = supabase.table("users")\
            .select("*")\
            .eq("user_id", user_id)\
            .execute()
        return response.data[0] if response.data else None
    except:
        return None

def save_user(user_id, name=None, city=None):
    if not supabase:
        return
    try:
        existing = get_user(user_id)
        if existing:
            supabase.table("users")\
                .update({
                    "name": name or existing.get("name"),
                    "city": city or existing.get("city")
                })\
                .eq("user_id", user_id)\
                .execute()
        else:
            supabase.table("users").insert({
                "user_id": user_id,
                "name": name or "Пользователь",
                "city": city,
                "created_at": datetime.now().isoformat(),
                "trial_start": datetime.now().isoformat()
            }).execute()
    except Exception as e:
        print(f"❌ Ошибка сохранения пользователя: {e}")

# ==========================
# ИЗВЛЕЧЕНИЕ ФАКТОВ
# ==========================

def extract_facts(text, user_id):
    text_lower = text.lower()
    
    # ИМЯ
    if "меня зовут" in text_lower:
        parts = text_lower.split("меня зовут")
        if len(parts) > 1:
            name = parts[1].strip().split()[0].capitalize()
            if name and len(name) > 1:
                save_fact(user_id, "name", name)
                save_user(user_id, name=name)
                print(f"✅ Запомнил имя: {name}")
    
    # ГОРОД
    city = None
    patterns = [
        r"из\s+([А-Яа-яёЁ\-]{3,})",
        r"в\s+([А-Яа-яёЁ\-]{3,})",
        r"живу\s+в\s+([А-Яа-яёЁ\-]{3,})"
    ]
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            city = match.group(1).capitalize()
            break
    
    if city:
        if city.lower() in ["инской", "инского", "инском"]:
            city = "Белово"
        save_fact(user_id, "city", city)
        save_user(user_id, city=city)
        print(f"✅ Запомнил город: {city}")

# ==========================
# ОСНОВНАЯ ЛОГИКА
# ==========================

async def process_message(user_id, text):
    save_message(user_id, "user", text)
    extract_facts(text, user_id)
    
    facts = get_user_facts(user_id)
    name = facts.get("name")
    city = facts.get("city")
    
    text_lower = text.lower()
    
    # Прямые вопросы
    if "как меня зовут" in text_lower or "моё имя" in text_lower:
        if name:
            reply = f"Тебя зовут **{name}**! Я запомнила это. 😊"
        else:
            reply = "Ты ещё не говорил, как тебя зовут. Представься, я запомню! 😊"
        save_message(user_id, "assistant", reply)
        return reply
    
    if "где я живу" in text_lower or "мой город" in text_lower:
        if city:
            reply = f"Ты из **{city}**! Ты говорил мне об этом. 😊"
        else:
            reply = "Ты ещё не говорил, из какого ты города. Расскажи, я запомню! 😊"
        save_message(user_id, "assistant", reply)
        return reply
    
    # Заглушки
    if "погод" in text_lower:
        reply = "🌤️ Погода — в разработке! Как только добавлю API — скажу. 😊"
        save_message(user_id, "assistant", reply)
        return reply
    
    if "нарисуй" in text_lower or "картинку" in text_lower:
        reply = "🎨 Генерация картинок — в разработке! Скоро будет. 😊"
        save_message(user_id, "assistant", reply)
        return reply
    
    # Основной диалог
    recent = get_recent_history(user_id, limit=15)
    
    context = []
    if name:
        context.append(f"👤 Имя: {name}")
    if city:
        context.append(f"📍 Город: {city}")
    context.append("💬 ДИАЛОГ:")
    for msg in recent[-10:]:
        role = "Пользователь" if msg["role"] == "user" else "AURA"
        context.append(f"{role}: {msg['content'][:200]}")
    
    context_text = "\n".join(context)
    
    messages = [
        {"role": "system", "content": AGENT_PROMPT},
        {"role": "system", "content": f"ФАКТЫ:\nИмя: {name or 'неизвестно'}\nГород: {city or 'неизвестен'}\n\nКОНТЕКСТ:\n{context_text}"},
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
        save_message(user_id, "assistant", reply)
        return reply
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        return "😅 Упс, что-то пошло не так. Попробуй ещё раз!"

# ==========================
# FASTAPI APP
# ==========================

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        message = body["message"]
        user_id = str(message["from"]["id"])
        
        if "text" in message:
            text = message["text"].strip()
            
            if text.startswith("/start"):
                save_user(user_id, message["from"]["first_name"])
                reply = "👋 Привет! Я AURA — с гибридной памятью.\n\nЯ запоминаю ВСЁ и отвечаю как человек. Представься — и я запомню тебя навсегда! 🤗"
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
    return {"status": "AURA Hybrid Memory is alive"}

# ==========================
# ЗАПУСК
# ==========================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
