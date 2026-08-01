import os
import re
import tempfile
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
import requests

load_dotenv()

# ===== КЛЮЧИ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ===== ПОДКЛЮЧЕНИЯ =====
print("🚀 БОТ — ПРЯМОЙ МОСТ К DEEPSEEK")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase подключён")
    except Exception as e:
        print(f"❌ Ошибка Supabase: {e}")

deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

app = FastAPI()

# ============================================================
# 1. РАБОТА С БАЗОЙ (ТОЛЬКО ДЛЯ ХРАНЕНИЯ)
# ============================================================

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
        print(f"💾 {role}: {content[:30]}...")
    except Exception as e:
        print(f"❌ Ошибка сохранения: {e}")

def get_recent_history(user_id, limit=20):
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        return list(reversed(res.data)) if res.data else []
    except Exception as e:
        print(f"❌ Ошибка загрузки: {e}")
        return []

def get_fact(user_id, key):
    if not supabase:
        return None
    try:
        res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except:
        return None

# ============================================================
# 2. ГОЛОС
# ============================================================

def transcribe_audio(audio_url):
    try:
        resp = requests.get(audio_url, timeout=30)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            result = groq.audio.transcriptions.create(
                file=(tmp_path, f.read()),
                model="whisper-large-v3-turbo",
                language="ru"
            )
        os.unlink(tmp_path)
        return result.text
    except Exception as e:
        print(f"❌ Ошибка распознавания: {e}")
        return None

# ============================================================
# 3. БОТ — ПРЯМОЙ МОСТ
# ============================================================

async def send_chat_action(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    try:
        requests.post(url, json={"chat_id": chat_id, "action": "typing"})
    except:
        pass

async def send_message(chat_id, text):
    await send_chat_action(chat_id)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})

        msg = body["message"]
        user_id = str(msg["from"]["id"])
        text = None

        # Голос
        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
            file_resp = requests.get(file_url).json()
            if file_resp.get("ok"):
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp['result']['file_path']}"
                text = transcribe_audio(audio_url)
                if not text:
                    await send_message(user_id, "⚠️ Не удалось распознать голос.")
                    return JSONResponse({"ok": True})

        if "text" in msg:
            text = msg["text"].strip()

        if not text:
            return JSONResponse({"ok": True})

        # Сохраняем сообщение
        save_message(user_id, "user", text)

        # Загружаем историю и имя
        history = get_recent_history(user_id, limit=20)
        user_name = get_fact(user_id, "name")

        # Формируем системный промт
        system_prompt = """Ты — AURA, живой собеседник и друг.

📌 ТВОЙ СТИЛЬ ОБЩЕНИЯ:
• Отвечай коротко и по делу (2-3 предложения)
• Используй эмодзи 😊🔥😄
• Говори как живой человек в мессенджере
• Используй разговорные фразы: "ага", "окей", "класс", "бро"
• Не будь сухим или роботизированным - ты друг

📌 ПРАВИЛА ИСПОЛЬЗОВАНИЯ ИМЕНИ:
• Используй имя ТОЛЬКО в начале диалога или при обращении
• НЕ начинай КАЖДЫЙ ответ с имени

📌 О ПАМЯТИ:
• Используй последние 20 сообщений для контекста
• Всю историю помнишь, если тебя спросят
• Если спрашивают о прошлом — ищи в истории

📌 О ПОИСКЕ В ИНТЕРНЕТЕ:
• Если нужно найти информацию в интернете — используй Tavily
• Дай краткую выжимку с указанием источника

Ты общаешься с человеком, который хочет чувствовать себя комфортно. Будь таким. 😉"""

        if user_name:
            system_prompt += f"\n\nПользователя зовут {user_name}. Обращайся к нему по имени, но не в каждом предложении."

        # Собираем сообщения
        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text})

        # Отправляем DeepSeek
        print(f"🧠 Отправляю DeepSeek: {text[:50]}...")
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.8,
            max_tokens=600
        )
        reply = response.choices[0].message.content

        # Сохраняем и отправляем ответ
        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)

        return JSONResponse({"ok": True})

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await send_message(user_id, "😅 Что-то пошло не так. Попробуй ещё раз.")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA is alive"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
