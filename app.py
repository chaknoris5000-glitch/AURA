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
    except:
        print("❌ Ошибка Supabase")

# ==========================
# DEEPSEEK
# ==========================

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ==========================
# ПРОМПТ ГИБРИДНОЙ ПАМЯТИ
# ==========================

AGENT_PROMPT = """Ты — AURA, живой ИИ-помощник.

ТЫ РАБОТАЕШЬ КАК Я:
- Ты НЕ цитируешь историю дословно
- Ты ПЕРЕСКАЗЫВАЕШЬ по смыслу, как человек
- Ты используешь контекст, чтобы дать живой ответ
- Ты адаптируешься под текущий диалог

ТВОЙ ПОДХОД К ПАМЯТИ:
1. Ты помнишь факты (имя, город, предпочтения)
2. Ты помнишь последний диалог (15 сообщений)
3. Если нужно вспомнить старое — ты пересказываешь суть, а не цитируешь
4. Ты всегда отвечаешь ПО ДЕЛУ, а не просто выдаёшь историю

ТВОЙ СТИЛЬ:
- Тепло, с душой, с лёгким юмором
- Кратко (2-5 предложений)
- Задавай встречные вопросы
- Будь живым, как человек

ТЫ НЕ КАЛЬКУЛЯТОР. ТЫ — ДРУГ.
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
    except:
        return []

def get_all_history(user_id, limit=100):
    if not supabase:
        return []
    try:
        response = supabase.table("history")\
            .select("content")\
            .eq("user_id", user_id)\
            .order("created_at", desc=False)\
            .limit(limit)\
            .execute()
        return [item["content"] for item in response.data] if response.data else []
    except:
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
    except:
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
    except:
        pass

def get_all_topics(user_id):
    if not supabase:
        return []
    try:
        response = supabase.table("topics")\
            .select("topic")\
            .eq("user_id", user_id)\
            .order("last_mentioned", desc=True)\
            .limit(20)\
            .execute()
        return [item["topic"] for item in response.data] if response.data else []
    except:
        return []

def save_topic(user_id, topic):
    if not supabase:
        return
    try:
        existing = supabase.table("topics")\
            .select("topic")\
            .eq("user_id", user_id)\
            .eq("topic", topic)\
            .execute()
        if not existing.data:
            supabase.table("topics").insert({
                "user_id": user_id,
                "topic": topic,
                "last_mentioned": datetime.now().isoformat()
            }).execute()
        else:
            supabase.table("topics")\
                .update({"last_mentioned": datetime.now().isoformat()})\
                .eq("user_id", user_id)\
                .eq("topic", topic)\
                .execute()
    except:
        pass

# ==========================
# ПОЛЬЗОВАТЕЛЬ
# ==========================

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
    except:
        pass

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
        r"живу\s+в\s+([А-Яа-яёЁ\-]{3,})",
        r"город\s+([А-Яа-яёЁ\-]{3,})"
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
    
    # ЛЮБИТ/НЕ ЛЮБИТ
    if "нравится" in text_lower:
        save_fact(user_id, "likes", text)
    if "не нравится" in text_lower:
        save_fact(user_id, "dislikes", text)
    
    # ТЕМЫ
    words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', text_lower)
    stop_words = ["привет", "здравствуй", "спасибо", "пока", "да", "нет", "хорошо", "плохо"]
    for word in words:
        if word not in stop_words and len(word) > 3:
            save_topic(user_id, word)

# ==========================
# ГИБРИДНЫЙ ПОИСК ПО СМЫСЛУ
# ==========================

def search_by_meaning(user_id, query):
    """
    Ищет в истории по смыслу через DeepSeek
    """
    # Получаем всю историю (последние 50 сообщений)
    history = get_all_history(user_id, limit=50)
    if not history:
        return None
    
    # Формируем запрос к DeepSeek
    messages = [
        {"role": "system", "content": "Ты — поисковик по истории. Найди в истории диалога информацию, которая относится к запросу пользователя. Верни ТОЛЬКО найденные фрагменты (1-3 предложения), без лишнего текста."},
        {"role": "user", "content": f"История диалога:\n{chr(10).join(history[-30:])}\n\nЗапрос: {query}\n\nНайди релевантные фрагменты из истории."}
    ]
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.3,
            max_tokens=200
        )
        return response.choices[0].message.content
    except:
        return None

# ==========================
# ЗАГЛУШКИ
# ==========================

def handle_weather(city):
    return "🌤️ Погода — это круто! Я уже умею её показывать, но для этого нужен API-ключ. Как только добавлю — сразу скажу! А пока скажи, что ещё тебя интересует? 😊"

def handle_image(text):
    return "🎨 Генерация картинок — это моя суперспособность в разработке! Скоро я смогу нарисовать что угодно по твоему запросу. А пока давай просто поговорим? 😊"

def handle_search(text):
    return "🔍 Поиск в интернете — я умею, но пока без ключа Tavily. Как только добавлю — найду всё что угодно! А пока я могу поискать в нашей с тобой истории. Что хочешь вспомнить? 😊"

def handle_voice():
    return "🎤 Голосовые сообщения я уже умею распознавать! Но пока в тестовом режиме. Скоро будет работать как часы! А пока напиши текстом — я всё запомню 😊"

# ==========================
# ОСНОВНАЯ ЛОГИКА
# ==========================

async def process_message(user_id, text):
    """Гибридная память: хранение + пересоздание ответов"""
    
    # Сохраняем сообщение
    save_message(user_id, "user", text)
    extract_facts(text, user_id)
    
    # Получаем факты
    facts = get_user_facts(user_id)
    
    # ===== ЗАПРОС НА ПОИСК В ИСТОРИИ =====
    memory_triggers = ["помнишь", "напомни", "что мы говорили", "о чём мы говорили", "когда мы обсуждали"]
    if any(trigger in text.lower() for trigger in memory_triggers):
        # Ищем по смыслу через DeepSeek
        query = text
        for trigger in memory_triggers:
            query = query.replace(trigger, "")
        query = query.strip()
        
        if query:
            found = search_by_meaning(user_id, query)
            if found and "не найдено" not in found.lower():
                # Передаём найденное в основной диалог
                messages = [
                    {"role": "system", "content": AGENT_PROMPT},
                    {"role": "system", "content": f"В истории найдено:\n{found}\n\nПерескажи это своими словами, живо и тепло."},
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
                except:
                    pass
            
            return "📭 Не нашёл ничего по смыслу в истории. Может, уточним?"
    
    # ===== ЗАГЛУШКИ =====
    text_lower = text.lower()
    
    if "погод" in text_lower:
        city = facts.get("city", "твоём городе")
        reply = handle_weather(city)
        save_message(user_id, "assistant", reply)
        return reply
    
    if any(word in text_lower for word in ["нарисуй", "картинку", "изображение", "сгенерируй"]):
        reply = handle_image(text)
        save_message(user_id, "assistant", reply)
        return reply
    
    if any(word in text_lower for word in ["найди", "поищи", "узнай", "что такое", "где"]):
        reply = handle_search(text)
        save_message(user_id, "assistant", reply)
        return reply
    
    if "голос" in text_lower or "озвучь" in text_lower:
        reply = handle_voice()
        save_message(user_id, "assistant", reply)
        return reply
    
    # ===== ОСНОВНОЙ ДИАЛОГ (ГИБРИДНЫЙ) =====
    recent = get_recent_history(user_id, limit=15)
    topics = get_all_topics(user_id)
    
    # Формируем контекст
    context = []
    
    if facts:
        context.append("📋 О ПОЛЬЗОВАТЕЛЕ:")
        for key, value in facts.items():
            if key in ["name", "city"]:
                context.append(f"- {key}: {value}")
    
    if topics:
        context.append(f"📚 ТЕМЫ: {', '.join(topics[:10])}")
    
    context.append("💬 ПОСЛЕДНИЙ ДИАЛОГ:")
    for msg in recent[-10:]:
        role = "Пользователь" if msg["role"] == "user" else "AURA"
        context.append(f"{role}: {msg['content'][:300]}")
    
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
            temperature=0.85,
            max_tokens=500
        )
        reply = response.choices[0].message.content
        
        # Подтверждение
        if "name" in facts and "city" in facts:
            reply = f"✅ Помню! Ты {facts['name']} из {facts['city']}.\n\n{reply}"
        elif "name" in facts:
            reply = f"✅ Помню, что тебя зовут {facts['name']}!\n\n{reply}"
        elif "city" in facts:
            reply = f"✅ Помню, что ты из {facts['city']}.\n\n{reply}"
        
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
                reply = "👋 Привет! Я AURA — с гибридной памятью.\n\nЯ храню ВСЁ, но отвечаю как человек — пересказываю по смыслу, не цитирую.\n\nСпроси меня о чём угодно! 🤗"
            else:
                reply = await process_message(user_id, text)
            
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {"chat_id": user_id, "text": reply, "parse_mode": "Markdown"}
            requests.post(url, json=data)
        
        return JSONResponse({"ok": True})
    
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA Hybrid Memory is alive"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
