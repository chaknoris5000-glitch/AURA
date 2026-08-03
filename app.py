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
import logging
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

logger.info("🚀 AURA — НОВАЯ ЧИСТАЯ АРХИТЕКТУРА")

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase подключён")
    except Exception as e:
        logger.error(f"❌ Ошибка Supabase: {e}")

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
        logger.info(f"💾 {role}: {content[:30]}...")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")

def get_recent_history(user_id, limit=30):
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
        logger.error(f"❌ Ошибка загрузки: {e}")
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
        logger.error(f"❌ Ошибка поиска: {e}")
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
        logger.info(f"💾 Сохранён факт: {key} = {value}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения факта: {e}")

def get_fact(user_id, key):
    if not supabase:
        return None
    try:
        res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except:
        return None

# ============================================================
# 2. ВРЕМЯ
# ============================================================

def get_current_time():
    now = datetime.utcnow() + timedelta(hours=7)
    return f"Сейчас **{now.strftime('%H:%M')}**, {now.strftime('%d.%m.%Y')} 😊"

# ============================================================
# 3. TAVILY — ПОИСК В ИНТЕРНЕТЕ
# ============================================================

def tavily_search(query, max_results=5):
    if not TAVILY_API_KEY:
        return None
    logger.info(f"🌐 Поиск: '{query}'")
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
                "search_depth": "advanced"
            },
            timeout=30
        )
        if response.status_code == 200:
            data = response.json()
            logger.info(f"✅ Найдено {len(data.get('results', []))} результатов")
            return data
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
    return None

def is_link_working(url):
    try:
        head = requests.head(url, timeout=5)
        return head.status_code < 400
    except:
        return False

def get_good_links(data):
    if not data or not data.get("results"):
        return []
    good_links = []
    for r in data.get("results", []):
        url = r.get("url")
        if url and is_link_working(url):
            good_links.append({
                "url": url,
                "title": r.get("title", ""),
                "content": r.get("content", "")
            })
            if len(good_links) >= 3:
                break
    return good_links

# ============================================================
# 4. ГОЛОС
# ============================================================

def transcribe_audio(audio_url):
    try:
        resp = requests.get(audio_url, timeout=30)
        if resp.status_code != 200:
            return None
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
        logger.error(f"❌ Ошибка распознавания: {e}")
        return None

# ============================================================
# 5. ОТПРАВКА
# ============================================================

async def send_chat_action(chat_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5
        )
    except:
        pass

async def send_message(chat_id, text):
    await send_chat_action(chat_id)
    if len(text) > 4000:
        text = text[:3997] + "..."
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=30
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

# ============================================================
# 6. DEEPSEEK — МОЗГ (РЕШАЕТ, ФОРМАТИРУЕТ)
# ============================================================

def deepseek_decide(user_id, text, user_name, user_city, history):
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])
    
    system_prompt = f"""Ты — AURA. Ты живой собеседник и друг.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История:
{history_text}

ИНСТРУКЦИЯ ПО ПАМЯТИ:
- Если пользователь спрашивает о прошлом → напиши: [HISTORY: ключевое слово]
- Если нужно найти информацию в интернете → напиши: [SEARCH: запрос]
- Если это простой вопрос → ответь сам.

ПРИМЕРЫ:
Пользователь: "Что мы говорили про работу?" → [HISTORY: работа]
Пользователь: "Найди клинику Калашникова" → [SEARCH: клиника Калашникова]
Пользователь: "Привет" → Привет! 😊

ПРАВИЛА:
- Отвечай коротко (2-3 предложения)
- Используй эмодзи 😊🔥
- Если не знаешь → переспроси
- Будь собой
"""
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": text})
    
    logger.info(f"🧠 DeepSeek решает: {text[:50]}...")
    response = deepseek.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        temperature=0.95,
        max_tokens=700,
        timeout=30
    )
    return response.choices[0].message.content

def deepseek_format_search(query, good_links):
    if not good_links:
        return "Ничего не нашёл. Попробуй переформулировать запрос 😊"
    
    links_text = ""
    for i, link in enumerate(good_links, 1):
        links_text += f"{i}. **{link['title']}**\n[Ссылка]({link['url']})\n\n"
    
    prompt = f"""Пользователь искал: "{query}"
Вот что нашлось:
{links_text}
Ответь пользователю КОРОТКО (2-3 предложения) как живой человек.
Дай самую важную информацию и ссылку на источник.
"""
    try:
        final = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=300,
            timeout=30
        )
        return final.choices[0].message.content
    except:
        return links_text

def deepseek_format_history(query, results):
    if not results:
        return "Ничего не нашёл в истории по этому запросу 😊"
    
    text_results = "\n".join([f"{h['role']}: {h['content']}" for h in results[:5]])
    prompt = f"""Вот что нашлось в истории по запросу '{query}':
{text_results}

Ответь пользователю коротко (2-3 предложения) как живой человек.
"""
    try:
        final = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,
            timeout=30
        )
        return final.choices[0].message.content
    except:
        return f"В истории нашлось:\n{text_results[:300]}"

# ============================================================
# 7. ОСНОВНАЯ ЛОГИКА
# ============================================================

def deepseek_process(user_id, text):
    try:
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")
        
        # === ВРЕМЯ ===
        if any(word in text.lower() for word in ["время", "сколько времени", "который час", "какое сегодня число"]):
            return get_current_time()
        
        # === ПРЯМЫЕ ЗАПРОСЫ (ИЗ USER_MEMORY) ===
        if any(word in text.lower() for word in ["как меня зовут", "моё имя", "кто я", "напомни моё имя"]):
            return f"Тебя зовут **{user_name}** 😊" if user_name else "Я не знаю твоего имени. Скажи: 'Меня зовут ...'"
        
        if any(word in text.lower() for word in ["где я живу", "мой город", "откуда я"]):
            return f"Ты из **{user_city}** 😊" if user_city else "Я не знаю, откуда ты. Скажи: 'Я живу в ...'"
        
        # === ОСНОВНОЙ ДИАЛОГ ===
        history = get_recent_history(user_id, limit=30)
        reply = deepseek_decide(user_id, text, user_name, user_city, history)
        
        # === ОБРАБОТКА КОМАНД DEEPSEEK ===
        
        # SEARCH
        search_match = re.search(r'\[SEARCH:\s*(.+?)\]', reply)
        if search_match:
            query = search_match.group(1).strip()
            logger.info(f"🔍 Поиск в интернете: '{query}'")
            
            if user_city:
                query = f"{query} {user_city}"
                logger.info(f"🔍 Добавил город: '{user_city}'")
            
            data = tavily_search(query)
            good_links = get_good_links(data)
            return deepseek_format_search(query, good_links)
        
        # HISTORY
        history_match = re.search(r'\[HISTORY:\s*(.+?)\]', reply)
        if history_match:
            query = history_match.group(1).strip()
            logger.info(f"📚 Поиск в истории: '{query}'")
            results = search_history(user_id, query)
            return deepseek_format_history(query, results)
        
        # === ЕСЛИ НЕТ КОМАНД ===
        if not reply or reply.strip() in ["", "...", "…"]:
            return "Что-то пошло не так. Попробуй переформулировать вопрос 😊"
        
        return reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return "😅 Произошла ошибка. Попробуй ещё раз."

# ============================================================
# 8. WEBHOOK
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
            try:
                file_resp = requests.get(file_url, timeout=30)
                if file_resp.status_code == 200 and file_resp.json().get("ok"):
                    audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp.json()['result']['file_path']}"
                    text = transcribe_audio(audio_url)
                    if not text:
                        await send_message(user_id, "⚠️ Не удалось распознать голос.")
                        return JSONResponse({"ok": True})
            except Exception as e:
                logger.error(f"❌ Ошибка голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса.")
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
        logger.error(f"❌ Ошибка webhook: {e}")
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
