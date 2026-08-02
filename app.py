import os
import re
import tempfile
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
import requests
import time

load_dotenv()

# ===== КЛЮЧИ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

print("🚀 БОТ — ФАСАД, ПОЛЬЗОВАТЕЛЬ ОБЩАЕТСЯ С DEEPSEEK")

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
# 1. БАЗА ДАННЫХ
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
# 2. ВРЕМЯ (UTC+7 ДЛЯ БЕЛОВО)
# ============================================================

def get_current_time():
    now = datetime.utcnow() + timedelta(hours=7)
    return f"Сейчас **{now.strftime('%H:%M')}**, {now.strftime('%d.%m.%Y')} 😊"

# ============================================================
# 3. ПОИСК В ИНТЕРНЕТЕ (TAVILY)
# ============================================================

def search_web(query, max_results=5):
    if not TAVILY_API_KEY:
        return None
    print(f"🌐 Tavily: '{query}'")
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": max_results,
            "include_answer": True,
            "include_raw_content": False,
            "search_depth": "advanced"
        }
        response = requests.post(url, json=payload, timeout=30)
        if response.status_code == 200:
            data = response.json()
            return {
                "answer": data.get("answer", ""),
                "results": data.get("results", []),
                "query": query
            }
    except Exception as e:
        print(f"❌ Ошибка поиска: {e}")
    return None

# ============================================================
# 4. ГОЛОС (GROQ WHISPER)
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
# 5. ОТПРАВКА В TELEGRAM
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
# 6. DEEPSEEK — МОЗГ (ВСЯ ЛОГИКА)
# ============================================================

def deepseek_process(user_id, text):
    try:
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")

        # === ВРЕМЯ ===
        text_lower = text.lower()
        if any(word in text_lower for word in ["время", "сколько времени", "который час", "какое сегодня число", "какой день"]):
            return get_current_time()

        history = get_recent_history(user_id, limit=20)
        history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])

        # === СИСТЕМНЫЙ ПРОМТ ===
        system_prompt = f"""Ты — AURA, живой, дружелюбный собеседник. Ты общаешься с пользователем через бота. Все твои ответы проходят через бота.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История:
{history_text}

Твои команды для бота:
- [SEARCH: запрос] — бот найдёт в интернете
- [HISTORY: запрос] — бот найдёт в истории
- [SAVE_NAME: имя] — бот запомнит имя
- [SAVE_CITY: город] — бот запомнит город

Отвечай коротко (2-3 предложения), с эмодзи. Никогда не отвечай пустотой.
"""
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text})

        print(f"🧠 DeepSeek: {text[:50]}...")
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            temperature=0.85,
            max_tokens=600
        )
        reply = response.choices[0].message.content

        # === ОБРАБОТКА КОМАНД DEEPSEEK ===

        # 1. Поиск в интернете
        search_match = re.search(r'\[SEARCH:\s*(.+?)\]', reply)
        if search_match:
            query = search_match.group(1).strip()
            print(f"🔍 Бот ищет в интернете: '{query}'")
            data = search_web(query)
            if data and data.get("results"):
                results_text = ""
                for r in data.get("results", [])[:5]:
                    results_text += f"\n- {r.get('title')}: {r.get('content')[:300]}...\n  Источник: {r.get('url')}"
                format_prompt = f"Вот что нашлось по запросу '{query}':\n{results_text}\n\nОтветь пользователю коротко, с эмодзи, дай ссылку на источник."
                final = deepseek.chat.completions.create(
                    model="deepseek-v4-flash",
                    messages=[{"role": "user", "content": format_prompt}],
                    temperature=0.7,
                    max_tokens=300
                )
                reply = final.choices[0].message.content
            else:
                reply = "Не удалось найти информацию в интернете. Попробуй переформулировать запрос 😊"

        # 2. Поиск в истории
        history_match = re.search(r'\[HISTORY:\s*(.+?)\]', reply)
        if history_match:
            query = history_match.group(1).strip()
            print(f"📚 Бот ищет в истории: '{query}'")
            results = search_history(user_id, query)
            if results:
                text_results = "\n".join([f"{r['role']}: {r['content']}" for r in results[:5]])
                format_prompt = f"Вот что нашлось в истории по запросу '{query}':\n{text_results}\n\nОтветь пользователю коротко, с эмодзи."
                final = deepseek.chat.completions.create(
                    model="deepseek-v4-flash",
                    messages=[{"role": "user", "content": format_prompt}],
                    temperature=0.7,
                    max_tokens=300
                )
                reply = final.choices[0].message.content
            else:
                reply = "Ничего не нашёл в истории по этому запросу 😊"

        # 3. Сохранение имени
        name_match = re.search(r'\[SAVE_NAME:\s*(.+?)\]', reply)
        if name_match:
            name = name_match.group(1).strip()
            save_fact(user_id, "name", name)
            reply = f"Запомнил! Тебя зовут **{name}** 😊"

        # 4. Сохранение города
        city_match = re.search(r'\[SAVE_CITY:\s*(.+?)\]', reply)
        if city_match:
            city = city_match.group(1).strip()
            save_fact(user_id, "city", city)
            reply = f"Запомнил! Ты из **{city}** 😊"

        # === ГАРАНТИРОВАННЫЙ ВОЗВРАТ ===
        if not reply or reply.strip() in ["", "...", "…"]:
            return "Что-то пошло не так. Попробуй переформулировать вопрос или напиши позже 😊"
        
        return reply

    except Exception as e:
        print(f"❌ Ошибка в deepseek_process: {e}")
        return "😅 Произошла ошибка. Попробуй ещё раз."

# ============================================================
# 7. WEBHOOK
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
        print(f"❌ Ошибка в webhook: {e}")
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
