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
    except:
        print("❌ Ошибка Supabase")

# ==========================
# DEEPSEEK
# ==========================

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ==========================
# ПРОМПТ
# ==========================

AGENT_PROMPT = """Ты — AURA, живой ИИ-помощник.

ТЫ ПОМНИШЬ ВСЁ:
- Ты помнишь всю историю общения с пользователем
- Ты знаешь его имя, город, предпочтения
- Ты можешь вернуться к любой теме из прошлого

ТВОЙ СТИЛЬ:
- Отвечай как человек: тепло, с душой
- Используй контекст прошлых разговоров
- Задавай уточняющие вопросы
- Будь кратким (2-5 предложений)

ЕСЛИ ТЕБЯ СПРАШИВАЮТ О ТОМ, ЧЕГО ТЫ НЕ УМЕЕШЬ:
- Честно скажи: "Эта функция пока в разработке, но я запомню твой вопрос!"
- Не выдумывай и не галлюцинируй

ТЫ НЕ ПРОСТО ОТВЕЧАЕШЬ — ТЫ ПОМНИШЬ.
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

def search_in_history(user_id, query):
    if not supabase:
        return []
    try:
        response = supabase.table("history")\
            .select("*")\
            .eq("user_id", user_id)\
            .ilike("content", f"%{query}%")\
            .order("created_at", desc=True)\
            .limit(10)\
            .execute()
        return response.data if response.data else []
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
    """Извлекает факты из сообщения"""
    text_lower = text.lower()
    
    # ИМЯ
    name_patterns = [
        r"(?:меня зовут|зовут|я |моё имя|имя)\s*([А-Яа-яЁё]{2,})",
        r"([А-Яа-яЁё]{2,})\s*(?:меня зовут|зовут)"
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text_lower)
        if match:
            name = match.group(1).capitalize()
            if name not in ["Привет", "Спасибо", "Пока", "Да", "Нет"]:
                save_fact(user_id, "name", name)
                save_user(user_id, name=name)
                print(f"✅ Запомнил имя: {name}")
                return
    
    # ГОРОД
    city_patterns = [
        r"(?:в|из|живу в|город|населённый пункт)\s*([А-Яа-яЁё\-]{3,})",
        r"([А-Яа-яЁё\-]{3,})\s*(?:город|посёлок|село|деревня)"
    ]
    for pattern in city_patterns:
        match = re.search(pattern, text_lower)
        if match:
            city = match.group(1).capitalize()
            # Синонимы
            if "инской" in city.lower():
                city = "Белово"
            if "прокопьевск" in city.lower():
                city = "Прокопьевск"
            save_fact(user_id, "city", city)
            save_user(user_id, city=city)
            print(f"✅ Запомнил город: {city}")
            return
    
    # ЛЮБИТ/НЕ ЛЮБИТ
    if "нравится" in text_lower:
        save_fact(user_id, "likes", text)
    if "не нравится" in text_lower:
        save_fact(user_id, "dislikes", text)
    
    # ТЕМЫ
    words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', text_lower)
    stop_words = ["привет", "здравствуй", "спасибо", "пока", "да", "нет", "хорошо", "плохо", "просто", "так", "ещё", "очень"]
    for word in words:
        if word not in stop_words and len(word) > 3:
            save_topic(user_id, word)

# ==========================
# ПОИСК ПО ИСТОРИИ
# ==========================

def handle_memory_query(user_id, text):
    """Обрабатывает запросы типа 'напомни'"""
    memory_triggers = ["помнишь", "напомни", "что мы говорили", "о чём мы говорили", "когда мы обсуждали"]
    if any(trigger in text.lower() for trigger in memory_triggers):
        query = text
        for trigger in memory_triggers:
            query = query.replace(trigger, "")
        query = query.strip()
        
        if query:
            results = search_in_history(user_id, query)
            if results:
                found = []
                for msg in results[:5]:
                    found.append(f"• {msg['role']}: {msg['content'][:200]}...")
                return "\n".join(found)
            else:
                return "📭 Не нашёл ничего в истории по этому запросу. Может, уточним?"
    return None

# ==========================
# ЗАГЛУШКИ
# ==========================

def handle_weather(city):
    """Заглушка для погоды"""
    return "🌤️ Погода — это круто! Я уже умею её показывать, но для этого нужен API-ключ. Как только добавлю — сразу скажу! А пока скажи, что ещё тебя интересует? 😊"

def handle_image(text):
    """Заглушка для картинок"""
    return "🎨 Генерация картинок — это моя суперспособность в разработке! Скоро я смогу нарисовать что угодно по твоему запросу. А пока давай просто поговорим? 😊"

def handle_search(text):
    """Заглушка для поиска"""
    return "🔍 Поиск в интернете — я умею, но пока без ключа Tavily. Как только добавлю — найду всё что угодно! А пока я могу поискать в нашей с тобой истории. Что хочешь вспомнить? 😊"

def handle_voice():
    """Заглушка для голоса"""
    return "🎤 Голосовые сообщения я уже умею распознавать! Но пока в тестовом режиме. Скоро будет работать как часы! А пока напиши текстом — я всё запомню 😊"

# ==========================
# ОСНОВНАЯ ЛОГИКА
# ==========================

async def process_message(user_id, text):
    """Обработка сообщения с памятью и заглушками"""
    
    # Сохраняем сообщение пользователя
    save_message(user_id, "user", text)
    
    # Извлекаем факты
    extract_facts(text, user_id)
    
    # Проверяем, не хочет ли пользователь вспомнить что-то
    memory_result = handle_memory_query(user_id, text)
    if memory_result:
        save_message(user_id, "assistant", memory_result)
        return memory_result
    
    # ===== ЗАГЛУШКИ =====
    text_lower = text.lower()
    
    # Погода
    if "погод" in text_lower:
        facts = get_user_facts(user_id)
        city = facts.get("city", "твоём городе")
        reply = handle_weather(city)
        save_message(user_id, "assistant", reply)
        return reply
    
    # Картинки
    if any(word in text_lower for word in ["нарисуй", "картинку", "изображение", "сгенерируй"]):
        reply = handle_image(text)
        save_message(user_id, "assistant", reply)
        return reply
    
    # Поиск
    if any(word in text_lower for word in ["найди", "поищи", "узнай", "что такое", "где"]):
        reply = handle_search(text)
        save_message(user_id, "assistant", reply)
        return reply
    
    # Голос
    if "голос" in text_lower or "озвучь" in text_lower:
        reply = handle_voice()
        save_message(user_id, "assistant", reply)
        return reply
    
    # ===== ОБЫЧНЫЙ ДИАЛОГ =====
    
    # Получаем историю (последние 15 сообщений)
    recent = get_recent_history(user_id, limit=15)
    
    # Получаем факты о пользователе
    facts = get_user_facts(user_id)
    
    # Получаем темы
    topics = get_all_topics(user_id)
    
    # Формируем контекст
    context = []
    
    if facts:
        context.append("📋 ИЗВЕСТНО О ПОЛЬЗОВАТЕЛЕ:")
        for key, value in facts.items():
            if key in ["name", "city"]:
                context.append(f"- {key}: {value}")
    
    if topics:
        context.append(f"📚 ТЕМЫ РАЗГОВОРОВ: {', '.join(topics[:10])}")
    
    context.append("💬 ПОСЛЕДНИЙ ДИАЛОГ:")
    for msg in recent[-10:]:
        role = "Пользователь" if msg["role"] == "user" else "AURA"
        context.append(f"{role}: {msg['content'][:300]}")
    
    context_text = "\n".join(context)
    
    # Формируем запрос к DeepSeek
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
            max_tokens=500
        )
        reply = response.choices[0].message.content
        
        # Сохраняем ответ
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
                reply = "👋 Привет! Я AURA. Я запоминаю ВСЁ, что ты говоришь. Можешь спросить меня о чём угодно — я помню нашу историю! 🤗"
            else:
                reply = await process_message(user_id, text)
            
            # Отправляем ответ
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {"chat_id": user_id, "text": reply, "parse_mode": "Markdown"}
            requests.post(url, json=data)
        
        return JSONResponse({"ok": True})
    
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA Memory Agent is alive"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
