import os
import re
import json
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
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

# ===== FASTAPI =====
app = FastAPI()

def get_user_fact(user_id, key):
    if not supabase:
        return None
    res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
    return res.data[0]["value"] if res.data else None

def save_user_fact(user_id, key, value):
    if not supabase:
        return
    supabase.table("user_memory").delete().eq("user_id", user_id).eq("key", key).execute()
    supabase.table("user_memory").insert({"user_id": user_id, "key": key, "value": value}).execute()

def get_history(user_id, limit=20):
    if not supabase:
        return []
    res = supabase.table("history").select("role, content").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
    return list(reversed(res.data)) if res.data else []

def save_message(user_id, role, content):
    if not supabase:
        return
    supabase.table("history").insert({
        "user_id": user_id,
        "role": role,
        "content": content,
        "created_at": datetime.now().isoformat()
    }).execute()

def extract_name(text):
    match = re.search(r"меня зовут\s+([А-Яа-я]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    return None

def extract_city(text):
    match = re.search(r"(?:из|в|живу в|живу)\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        city = match.group(1).capitalize()
        if city.lower() in ["инской", "инского", "инском"]:
            return "Белово"
        return city
    return None

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

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})

        msg = body["message"]
        user_id = str(msg["from"]["id"])
        text = None

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

        # ===== ЛОГИКА =====
        if text.startswith("/start"):
            await send_message(user_id, "👋 Привет! Скажи 'Меня зовут ...' — я запомню.")
            return JSONResponse({"ok": True})

        # Сохраняем сообщение пользователя
        save_message(user_id, "user", text)

        # Извлекаем имя и город
        name = extract_name(text)
        if name:
            save_user_fact(user_id, "name", name)
        city = extract_city(text)
        if city:
            save_user_fact(user_id, "city", city)

        # Получаем имя и город из базы
        user_name = get_user_fact(user_id, "name")
        user_city = get_user_fact(user_id, "city")

        # ===== ПРЯМЫЕ ВОПРОСЫ =====
        lower = text.lower()
        if "как меня зовут" in lower or "моё имя" in lower:
            reply = f"Тебя зовут {user_name}." if user_name else "Ты ещё не представлялся."
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        if "где я живу" in lower or "мой город" in lower:
            reply = f"Ты из {user_city}." if user_city else "Ты ещё не говорил, откуда ты."
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== ОСНОВНОЙ ДИАЛОГ =====
        history = get_history(user_id, limit=20)
        messages = [{"role": "system", "content": "Ты — AURA, живой помощник."}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text})

        try:
            response = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                temperature=0.8,
                max_tokens=400
            )
            reply = response.choices[0].message.content

            if user_name and not reply.startswith(user_name):
                reply = f"{user_name}, {reply[0].lower() + reply[1:] if reply else ''}"

            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)

        except Exception as e:
            print(f"❌ Ошибка DeepSeek: {e}")
            await send_message(user_id, "😅 Что-то пошло не так. Попробуй ещё раз.")

        return JSONResponse({"ok": True})

    except Exception as e:
        print(f"❌ Ошибка вебхука: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

async def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=data)

@app.get("/")
async def root():
    return {"status": "AURA is alive"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
