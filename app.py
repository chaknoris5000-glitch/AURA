import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime
import os
import requests
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

# ==========================
# ПРОМПТ
# ==========================

AGENT_PROMPT = """Ты — AURA, умный и живой ИИ-помощник.

Твоя задача — общаться как человек, помогать и запоминать важную информацию о пользователе.

ПРАВИЛА:
1. Всегда используй имя пользователя, если оно известно.
2. Если пользователь спрашивает о чём-то, что ты не знаешь — скажи честно.
3. Отвечай кратко (2-4 предложения), но тепло и с душой.
4. Если пользователь представляется — запомни это и в следующем ответе используй его имя.
5. Если пользователь говорит, откуда он — запомни город.

Ты — не просто бот. Ты — друг и помощник.
"""

# ==========================
# РАБОТА С БАЗОЙ ДАННЫХ
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
        print(f"❌ Ошибка сохранения сообщения: {e}")

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

def save_fact(user_id, key, value):
    if not supabase:
        return
    try:
        supabase.table("user_memory").delete().eq("user_id", user_id).eq("key", key).execute()
        supabase.table("user_memory").insert({
            "user_id": user_id,
            "key": key,
            "value": value,
            "created_at": datetime.now().isoformat()
        }).execute()
        print(f"💾 Сохранён факт: {key} = {value}")
    except Exception as e:
        print(f"❌ Ошибка сохранения факта: {e}")

def get_facts(user_id):
    if not supabase:
        return {}
    try:
        response = supabase.table("user_memory").select("key, value").eq("user_id", user_id).execute()
        if response.data:
            return {item["key"]: item["value"] for item in response.data}
        return {}
    except Exception as e:
        print(f"❌ Ошибка получения фактов: {e}")
        return {}

# ==========================
# ИЗВЛЕЧЕНИЕ ИМЕНИ И ГОРОДА ЧЕРЕЗ ДИПСИК
# ==========================

def extract_info(text):
    """DeepSeek сам находит имя и город в тексте"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": """
Ты — помощник. Из текста пользователя нужно извлечь:
1. Имя человека (если он представляется)
2. Город (если он говорит, где живёт или откуда)

Верни ответ строго в формате JSON:
{"name": "имя или null", "city": "город или null"}

Если имя не указано — верни null. Если город не указан — верни null.
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
        print(f"🔍 AI ответ: {result}")
        data = json.loads(result)
        return data.get("name"), data.get("city")
    except Exception as e:
        print(f"❌ Ошибка извлечения фактов: {e}")
        return None, None

# ==========================
# ГЛАВНАЯ ЛОГИКА БОТА
# ==========================

async def process_message(user_id, text):
    # Сохраняем сообщение пользователя
    save_message(user_id, "user", text)

    # Извлекаем имя и город через DeepSeek
    name, city = extract_info(text)

    if name:
        save_fact(user_id, "name", name)
    if city:
        if city.lower() in ["инской", "инского", "инском"]:
            city = "Белово"
        save_fact(user_id, "city", city)

    # Получаем все факты
    facts = get_facts(user_id)
    user_name = facts.get("name")
    user_city = facts.get("city")

    print(f"📋 Итоговые факты: имя={user_name}, город={user_city}")

    # Проверяем, если пользователь спрашивает имя или город
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

    # Заглушки
    if "погода" in text_lower:
        reply = "🌤️ Погода пока в разработке. Как только добавлю API — сразу скажу!"
        save_message(user_id, "assistant", reply)
        return reply

    if "нарисуй" in text_lower or "картинка" in text_lower:
        reply = "🎨 Генерация картинок пока в разработке. Скоро будет!"
        save_message(user_id, "assistant", reply)
        return reply

    # Основной диалог
    history = get_recent_history(user_id, limit=15)

    # Формируем контекст
    context = []
    if user_name:
        context.append(f"👤 Имя пользователя: {user_name}")
    if user_city:
        context.append(f"📍 Город: {user_city}")
    
    context.append("💬 Последние сообщения:")
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

        # Если имя есть — добавляем в ответ
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
        if "message" not in body:
            return JSONResponse({"ok": True})

        message = body["message"]
        user_id = str(message["from"]["id"])

        if "text" in message:
            text = message["text"].strip()

            if text.startswith("/start"):
                reply = "👋 Привет! Я AURA — твой умный помощник.\n\nПросто представься, и я запомню тебя навсегда. Расскажи, откуда ты, если хочешь."
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
