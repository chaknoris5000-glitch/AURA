import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
import os
import requests
import re
import json
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
        print(f"💾 Сохранено {role}: {content[:50]}...")
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
            facts = {item["key"]: item["value"] for item in response.data}
            print(f"📋 Факты: {facts}")
            return facts
        return {}
    except Exception as e:
        print(f"❌ Ошибка получения фактов: {e}")
        return {}

def save_fact(user_id, key, value):
    if not supabase:
        return
    try:
        # Удаляем старый
        supabase.table("user_memory")\
            .delete()\
            .eq("user_id", user_id)\
            .eq("key", key)\
            .execute()
        # Вставляем новый
        supabase.table("user_memory").insert({
            "user_id": user_id,
            "key": key,
            "value": value,
            "created_at": datetime.now().isoformat()
        }).execute()
        print(f"💾 Сохранён факт: {key} = {value}")
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
        print(f"💾 Сохранён пользователь: {user_id} -> name={name}, city={city}")
    except Exception as e:
        print(f"❌ Ошибка сохранения пользователя: {e}")

# ==========================
# ИЗВЛЕЧЕНИЕ ФАКТОВ — ЖЁСТКО!
# ==========================

def extract_facts(text, user_id):
    """Извлекает факты и ПРИНУДИТЕЛЬНО сохраняет"""
    text_lower = text.lower()
    saved = False
    
    # ===== ИМЯ =====
    if "меня зовут" in text_lower:
        parts = text_lower.split("меня зовут")
        if len(parts) > 1:
            name = parts[1].strip().split()[0].capitalize()
            if name and len(name) > 1:
                save_fact(user_id, "name", name)
                save_user(user_id, name=name)
                print(f"✅ ЗАПОМНИЛ ИМЯ: {name}")
                saved = True
    
    if not saved and ("зовут" in text_lower) and ("меня" not in text_lower):
        parts = text_lower.split("зовут")
        if len(parts) > 1:
            name = parts[1].strip().split()[0].capitalize()
            if name and len(name) > 1:
                save_fact(user_id, "name", name)
                save_user(user_id, name=name)
                print(f"✅ ЗАПОМНИЛ ИМЯ: {name}")
                saved = True
    
    # ===== ГОРОД =====
    city = None
    patterns = [
        r"из\s+([А-Яа-яёЁ\-]{3,})",
        r"в\s+([А-Яа-яёЁ\-]{3,})",
        r"живу\s+в\s+([А-Яа-яёЁ\-]{3,})",
        r"живу\s+([А-Яа-яёЁ\-]{3,})",
        r"город\s+([А-Яа-яёЁ\-]{3,})"
    ]
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            city = match.group(1).capitalize()
            break
    
    if city:
        # Инской → Белово
        if city.lower() in ["инской", "инского", "инском"]:
            city = "Белово"
        save_fact(user_id, "city", city)
        save_user(user_id, city=city)
        print(f"✅ ЗАПОМНИЛ ГОРОД: {city}")
        saved = True
    
    return saved

# ==========================
# ОСНОВНАЯ ЛОГИКА
# ==========================

async def process_message(user_id, text):
    """Гибридная память с жёстким сохранением"""
    
    # 1. Сохраняем сообщение пользователя
    save_message(user_id, "user", text)
    
    # 2. ИЗВЛЕКАЕМ И СОХРАНЯЕМ ФАКТЫ
    extract_facts(text, user_id)
    
    # 3. ПОЛУЧАЕМ СОХРАНЁННЫЕ ФАКТЫ
    facts = get_user_facts(user_id)
    name = facts.get("name")
    city = facts.get("city")
    
    print(f"🔍 Факты для {user_id}: name={name}, city={city}")
    
    # 4. БЫСТРЫЕ ОТВЕТЫ НА ИМЯ И ГОРОД
    text_lower = text.lower()
    
    if "как меня зовут" in text_lower or "моё имя" in text_lower or "напомни имя" in text_lower:
        if name:
            reply = f"Тебя зовут **{name}**! Я запомнила это, когда ты представился. 😊"
            save_message(user_id, "assistant", reply)
            return reply
        else:
            reply = "Ты ещё не говорил, как тебя зовут. Представься, я запомню! 😊"
            save_message(user_id, "assistant", reply)
            return reply
    
    if "где я живу" in text_lower or "мой город" in text_lower or "из какого я города" in text_lower:
        if city:
            reply = f"Ты из **{city}**! Ты говорил мне об этом. 😊"
            save_message(user_id, "assistant", reply)
            return reply
        else:
            reply = "Ты ещё не говорил, из какого ты города. Расскажи, я запомню! 😊"
            save_message(user_id, "assistant", reply)
            return reply
    
    # 5. ЗАГЛУШКИ
    if "погод" in text_lower:
        reply = "🌤️ Погода — это круто! Я уже умею её показывать, но для этого нужен API-ключ. Как только добавлю — сразу скажу! 😊"
        save_message(user_id, "assistant", reply)
        return reply
    
    if any(word in text_lower for word in ["нарисуй", "картинку", "изображение", "сгенерируй"]):
        reply = "🎨 Генерация картинок — это моя суперспособность в разработке! Скоро я смогу нарисовать что угодно! 😊"
        save_message(user_id, "assistant", reply)
        return reply
    
    if any(word in text_lower for word in ["найди", "поищи", "узнай", "что такое"]):
        reply = "🔍 Поиск в интернете — я умею, но пока без ключа Tavily. Как только добавлю — найду всё! 😊"
        save_message(user_id, "assistant", reply)
        return reply
    
    # 6. ОСНОВНОЙ ДИАЛОГ
    recent = get_recent_history(user_id, limit=15)
    
    context = []
    
    if name:
        context.append(f"👤 Имя пользователя: {name}")
    if city:
        context.append(f"📍 Город: {city}")
    
    context.append("💬 ПОСЛЕДНИЙ ДИАЛОГ:")
    for msg in recent[-10:]:
        role = "Пользователь" if msg["role"] == "user" else "AURA"
        context.append(f"{role}: {msg['content'][:300]}")
    
    context_text = "\n".join(context)
    
    messages = [
        {"role": "system", "content": AGENT_PROMPT},
        {"role": "system", "content": f"ФАКТЫ О ПОЛЬЗОВАТЕЛЕ:\nИмя: {name or 'неизвестно'}\nГород: {city or 'неизвестен'}\n\nКОНТЕКСТ:\n{context_text}"},
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
        
        # Добавляем подтверждение в начале
        if name and city and ("представ" not in text_lower and "познаком" not in text_lower):
            if len(reply) > 0 and not reply.startswith("✅"):
                reply = f"✅ Помню! Ты {name} из {city}.\n\n{reply}"
        elif name and ("представ" not in text_lower and "познаком" not in text_lower):
            if len(reply) > 0 and not reply.startswith("✅"):
                reply = f"✅ Помню, что тебя зовут {name}!\n\n{reply}"
        
        save_message(user_id, "assistant", reply)
        return reply
    
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        return "😅 Упс, что-то пошло не так. Попробуй ещё раз!"

# ==========================
# ВЕБХУК
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
