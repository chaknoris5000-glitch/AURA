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

# ===== ЛОГИРОВАНИЕ =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ===== КЛЮЧИ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

logger.info("🚀 БОТ ЗАПУЩЕН")

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
# 1. БАЗА
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

def get_recent_history(user_id, limit=15):
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
# 3. ПОИСК (TAVILY)
# ============================================================

def search_web(query, max_results=5):
    if not TAVILY_API_KEY:
        logger.error("❌ Tavily API ключ не найден")
        return None
    
    logger.info(f"🌐 Tavily: '{query}'")
    
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
            logger.info(f"✅ Tavily: {len(data.get('results', []))} результатов")
            return {
                "answer": data.get("answer", ""),
                "results": data.get("results", []),
                "query": query
            }
        else:
            logger.error(f"❌ Ошибка Tavily: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
        return None

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
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    try:
        requests.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except:
        pass

async def send_message(chat_id, text):
    await send_chat_action(chat_id)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    if len(text) > 4000:
        text = text[:3997] + "..."
    
    try:
        response = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=30)
        
        if response.status_code != 200:
            requests.post(url, json={
                "chat_id": chat_id,
                "text": text
            }, timeout=30)
            
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

# ============================================================
# 6. ГЛАВНАЯ ЛОГИКА
# ============================================================

def extract_name_from_text(text):
    match = re.search(r"меня зовут\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    
    match = re.search(r"зовут\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    
    match = re.search(r"я\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        name = match.group(1).capitalize()
        if len(name) > 1 and name not in ["Я", "Ты", "Он", "Она", "Мы", "Вы", "Они"]:
            return name
    
    return None

def extract_city_from_text(text):
    text_lower = text.lower()
    
    markers = ["живу в", "я из", "из города", "в городе", "посёлок", "город"]
    if not any(marker in text_lower for marker in markers):
        return None
    
    match = re.search(r"(?:из|в|живу в|живу)\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        city = match.group(1).capitalize()
        if len(city) > 2:
            return city
    
    return None

def deepseek_process(user_id, text):
    try:
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")
        
        # === ВРЕМЯ ===
        text_lower = text.lower()
        if any(word in text_lower for word in ["время", "сколько времени", "который час", "какое сегодня число", "какой день"]):
            return get_current_time()
        
        # === ИМЯ ===
        name = extract_name_from_text(text)
        if name:
            save_fact(user_id, "name", name)
            return f"Запомнил! Тебя зовут **{name}** 😊"
        
        # === ГОРОД ===
        city = extract_city_from_text(text)
        if city:
            save_fact(user_id, "city", city)
            return f"Запомнил! Ты из **{city}** 😊"
        
        # === ОСНОВНОЙ ДИАЛОГ ===
        history = get_recent_history(user_id, limit=15)
        history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])
        
        system_prompt = f"""Ты — AURA. Ты живой собеседник, друг.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История:
{history_text}

Общайся как человек. Коротко, с душой, с эмодзи.

Если нужно найти информацию — напиши [SEARCH: запрос]

Будь собой.
"""
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text})
        
        logger.info(f"🧠 DeepSeek: {text[:50]}...")
        
        try:
            response = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                temperature=0.9,
                max_tokens=600,
                timeout=30
            )
            reply = response.choices[0].message.content
            logger.info(f"🧠 DeepSeek ответил: {reply[:50]}...")
        except Exception as e:
            logger.error(f"❌ Ошибка DeepSeek: {e}")
            return "😅 Не удалось связаться с DeepSeek. Попробуй ещё раз."
        
        # === ПОИСК В ИНТЕРНЕТЕ ===
        search_match = re.search(r'\[SEARCH:\s*(.+?)\]', reply)
        if search_match:
            query = search_match.group(1).strip()
            logger.info(f"🔍 Поиск: '{query}'")
            
            if user_city and not any(word in query.lower() for word in ["белово", "инской", "кемерово"]):
                query = f"{query} {user_city}"
                logger.info(f"🔍 Добавил город: '{user_city}'")
            
            data = search_web(query)
            
            if data and data.get("results"):
                results = data.get("results", [])[:5]
                results_text = ""
                for r in results:
                    title = r.get("title", "Без названия")
                    content = r.get("content", "")[:300]
                    url = r.get("url", "")
                    results_text += f"\n**{title}**\n{content}...\n[Источник]({url})\n"
                
                format_prompt = f"Вот что нашлось по запросу '{query}':\n{results_text}\n\nОтветь пользователю как живой человек, коротко, дай ссылку."
                
                try:
                    final = deepseek.chat.completions.create(
                        model="deepseek-v4-flash",
                        messages=[{"role": "user", "content": format_prompt}],
                        temperature=0.8,
                        max_tokens=300,
                        timeout=30
                    )
                    result_text = final.choices[0].message.content
                    
                    if result_text and result_text.strip() not in ["", "...", "…"]:
                        return result_text
                    else:
                        return f"Нашёл информацию по запросу '{query}':\n\n{results_text}"
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка форматирования: {e}")
                    return f"Нашёл информацию по запросу '{query}':\n\n{results_text}"
            else:
                return f"Не удалось найти информацию по запросу '{query}'. Попробуй переформулировать запрос 😊"
        
        # === ПОИСК В ИСТОРИИ ===
        history_match = re.search(r'\[HISTORY:\s*(.+?)\]', reply)
        if history_match:
            query = history_match.group(1).strip()
            logger.info(f"📚 История: '{query}'")
            
            results = search_history(user_id, query)
            
            if results:
                text_results = "\n".join([f"{r['role']}: {r['content']}" for r in results[:5]])
                format_prompt = f"Вот что нашлось в истории по запросу '{query}':\n{text_results}\n\nОтветь пользователю как живой человек, коротко."
                
                try:
                    final = deepseek.chat.completions.create(
                        model="deepseek-v4-flash",
                        messages=[{"role": "user", "content": format_prompt}],
                        temperature=0.8,
                        max_tokens=300,
                        timeout=30
                    )
                    result_text = final.choices[0].message.content
                    
                    if result_text and result_text.strip() not in ["", "...", "…"]:
                        return result_text
                    else:
                        return f"В истории нашлось:\n\n{text_results}"
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка форматирования: {e}")
                    return f"В истории нашлось:\n\n{text_results}"
            else:
                return "Ничего не нашёл в истории по этому запросу 😊"
        
        # === ЕСЛИ НЕТ КОМАНД ===
        if not reply or reply.strip() in ["", "...", "…"]:
            return "Что-то пошло не так. Попробуй переформулировать вопрос 😊"
        
        return reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
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
