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

print("🚀 БОТ — ФАСАД, ВСЁ ДЕЛАЕТ DEEPSEEK (ВКЛЮЧАЯ ПОИСК)")

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
# 1. РАБОТА С БАЗОЙ
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

def search_history(user_id, query):
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .ilike("content", f"%{query}%")\
            .order("created_at", desc=True)\
            .limit(10)\
            .execute()
        return res.data if res.data else []
    except Exception as e:
        print(f"❌ Ошибка поиска: {e}")
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

def get_fact(user_id, key):
    if not supabase:
        return None
    try:
        res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except:
        return None

# ============================================================
# 2. ГОЛОС (GROQ)
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
# 3. ОТПРАВКА
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

# ============================================================
# 4. DEEPSEEK — ВСЁ В ОДНОМ РУКЕ (С ВЕБ-ПОИСКОМ)
# ============================================================

def deepseek_process(user_id, text):
    history = get_recent_history(user_id, limit=20)
    user_name = get_fact(user_id, "name")
    user_city = get_fact(user_id, "city")
    
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])

    system_prompt = f"""Ты — AURA, живой собеседник и друг.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История диалога:
{history_text}

ТВОИ ВОЗМОЖНОСТИ:
1. Отвечать на вопросы из своих знаний
2. Искать в истории диалога — используй [HISTORY: запрос]
3. Искать в интернете — используй [SEARCH: запрос] (глубокий поиск)
4. Запоминать имя — [SAVE_NAME: имя]
5. Запоминать город — [SAVE_CITY: город]

ОТВЕЧАЙ КОРОТКО (2-3 предложения), ЖИВО, С ЭМОДЗИ.
Если нужен поиск — используй [SEARCH: ...] с максимальной глубиной.
"""
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": text})

    print(f"🧠 DeepSeek: {text[:50]}...")
    response = deepseek.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        temperature=0.8,
        max_tokens=600,
        # ВКЛЮЧАЕМ ВЕБ-ПОИСК
        tools=[{"type": "web_search"}],
        tool_choice="auto"
    )
    
    reply = response.choices[0].message.content

    # --- ОБРАБОТКА КОМАНД (ЕСЛИ DeepSeek ИХ ИСПОЛЬЗУЕТ) ---
    
    # Поиск в истории
    history_match = re.search(r'\[HISTORY:\s*(.+?)\]', reply)
    if history_match:
        query = history_match.group(1).strip()
        print(f"📚 DeepSeek → история: '{query}'")
        results = search_history(user_id, query)
        if results:
            text_results = "\n".join([f"{r['role']}: {r['content']}" for r in results[:5]])
            prompt = f"Вот что нашлось в истории по запросу '{query}':\n{text_results}\n\nОтветь пользователю коротко, с эмодзи."
            final = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=300
            )
            reply = final.choices[0].message.content
        else:
            reply = "Ничего не нашёл в истории по этому запросу 😊"

    # Сохранение имени
    name_match = re.search(r'\[SAVE_NAME:\s*(.+?)\]', reply)
    if name_match:
        name = name_match.group(1).strip()
        save_fact(user_id, "name", name)
        reply = f"Запомнил! Тебя зовут **{name}** 😊"

    # Сохранение города
    city_match = re.search(r'\[SAVE_CITY:\s*(.+?)\]', reply)
    if city_match:
        city = city_match.group(1).strip()
        save_fact(user_id, "city", city)
        reply = f"Запомнил! Ты из **{city}** 😊"

    return reply

# ============================================================
# 5. WEBHOOK
# ============================================================

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

        save_message(user_id, "user", text)

        reply = deepseek_process(user_id, text)

        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)

        return JSONResponse({"ok": True})

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        try:
            await send_message(user_id, "😅 Что-то пошло не так. Попробуй ещё раз.")
        except:
            pass
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA is alive"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
