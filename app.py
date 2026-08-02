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

logger.info("🚀 БОТ ЗАПУСКАЕТСЯ...")

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
# 1. БАЗА ДАННЫХ
# ============================================================

def save_message(user_id, role, content):
    if not supabase:
        return False
    try:
        supabase.table("history").insert({
            "user_id": user_id,
            "role": role,
            "content": content,
            "created_at": datetime.now().isoformat()
        }).execute()
        logger.info(f"💾 {role}: {content[:30]}...")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")
        return False

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
        return False
    try:
        supabase.table("user_memory").delete().eq("user_id", user_id).eq("key", key).execute()
        supabase.table("user_memory").insert({
            "user_id": user_id,
            "key": key,
            "value": value,
            "created_at": datetime.now().isoformat()
        }).execute()
        logger.info(f"💾 Сохранён факт: {key} = {value}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения факта: {e}")
        return False

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
    
    logger.info(f"🌐 Tavily запрос: '{query}'")
    
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
            logger.info(f"✅ Tavily: найдено {len(data.get('results', []))} результатов")
            return {
                "answer": data.get("answer", ""),
                "results": data.get("results", []),
                "query": query
            }
        else:
            logger.error(f"❌ Ошибка Tavily: {response.status_code}")
            return None
            
    except requests.exceptions.Timeout:
        logger.error("❌ Таймаут Tavily")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка поиска: {e}")
        return None

# ============================================================
# 4. ГОЛОС (GROQ WHISPER)
# ============================================================

def transcribe_audio(audio_url):
    try:
        resp = requests.get(audio_url, timeout=30)
        if resp.status_code != 200:
            logger.error(f"❌ Ошибка загрузки аудио: {resp.status_code}")
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
# 5. ОТПРАВКА В TELEGRAM
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
    
    # Обрезаем слишком длинные сообщения
    if len(text) > 4000:
        text = text[:3997] + "..."
    
    try:
        response = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"❌ Ошибка отправки: {response.status_code}")
            # Пробуем отправить без Markdown
            requests.post(url, json={
                "chat_id": chat_id,
                "text": text
            }, timeout=30)
            
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

# ============================================================
# 6. ИЗВЛЕЧЕНИЕ ИМЕНИ И ГОРОДА
# ============================================================

def extract_name_from_text(text):
    """Извлекает имя из текста"""
    text_lower = text.lower()
    
    # Проверяем на "меня зовут"
    match = re.search(r"меня зовут\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    
    # Проверяем на "зовут"
    match = re.search(r"зовут\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    
    # Проверяем на "я"
    match = re.search(r"я\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        name = match.group(1).capitalize()
        if len(name) > 1 and name not in ["Я", "Ты", "Он", "Она", "Мы", "Вы", "Они"]:
            return name
    
    return None

def extract_city_from_text(text):
    """Извлекает город из текста"""
    text_lower = text.lower()
    
    # Маркеры города
    markers = ["живу в", "я из", "из города", "в городе", "посёлок", "город"]
    if not any(marker in text_lower for marker in markers):
        return None
    
    match = re.search(r"(?:из|в|живу в|живу)\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        city = match.group(1).capitalize()
        if len(city) > 2:
            return city
    
    return None

# ============================================================
# 7. DEEPSEEK — ОБРАБОТКА ЗАПРОСА
# ============================================================

def deepseek_process(user_id, text):
    try:
        # Получаем данные пользователя
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")
        
        # ===== ПРОВЕРКА НА ВРЕМЯ =====
        text_lower = text.lower()
        if any(word in text_lower for word in ["время", "сколько времени", "который час", "какое сегодня число", "какой день"]):
            return get_current_time()
        
        # ===== ПРОВЕРКА НА ИМЯ =====
        extracted_name = extract_name_from_text(text)
        if extracted_name:
            save_fact(user_id, "name", extracted_name)
            return f"Запомнил! Тебя зовут **{extracted_name}** 😊"
        
        # ===== ПРОВЕРКА НА ГОРОД =====
        extracted_city = extract_city_from_text(text)
        if extracted_city:
            save_fact(user_id, "city", extracted_city)
            return f"Запомнил! Ты из **{extracted_city}** 😊"
        
        # ===== ОСНОВНОЙ ДИАЛОГ С DEEPSEEK =====
        history = get_recent_history(user_id, limit=15)
        history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])
        
        # ===== ЖЁСТКИЙ ПРОМТ =====
        system_prompt = f"""Ты — AURA, живой собеседник.

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История (последние 15 сообщений):
{history_text}

ПРАВИЛА:
1. Если пользователь просит найти что-то в интернете (адрес, сайт, новости, картинки, видео, погода, курс, фильмы) — ОБЯЗАТЕЛЬНО используй [SEARCH: запрос]
2. Если пользователь спрашивает о прошлом или истории — используй [HISTORY: запрос]
3. Если пользователь просто общается — отвечай естественно, как человек, коротко (2-3 предложения), с эмодзи

ПРИМЕРЫ:
- Пользователь: "Найди клинику Калашникова" → Ты: [SEARCH: клиника Калашникова]
- Пользователь: "Что мы говорили про работу?" → Ты: [HISTORY: работа]
- Пользователь: "Привет!" → Ты: Привет! 😊 Как дела?

НИКОГДА НЕ ОТВЕЧАЙ ПУСТОТОЙ.
ЕСЛИ НЕ ЗНАЕШЬ — ИСПОЛЬЗУЙ [SEARCH].
"""
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text})
        
        logger.info(f"🧠 DeepSeek запрос: {text[:50]}...")
        
        # ===== ЗАПРОС К DEEPSEEK =====
        try:
            response = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                temperature=0.85,
                max_tokens=600,
                timeout=30
            )
            reply = response.choices[0].message.content
            logger.info(f"🧠 DeepSeek ответил: {reply[:50]}...")
        except Exception as e:
            logger.error(f"❌ Ошибка DeepSeek: {e}")
            return "😅 Не удалось связаться с DeepSeek. Попробуй ещё раз."
        
        # ===== ОБРАБОТКА КОМАНД =====
        
        # 1. Поиск в интернете
        search_match = re.search(r'\[SEARCH:\s*(.+?)\]', reply)
        if search_match:
            query = search_match.group(1).strip()
            logger.info(f"🔍 Поиск в интернете: '{query}'")
            
            # Добавляем город если есть
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
                    results_text += f"\n- {title}: {content}...\n  Источник: {url}\n"
                
                format_prompt = f"Вот что нашлось по запросу '{query}':\n{results_text}\n\nОтветь пользователю коротко, с эмодзи, дай ссылку на источник."
                
                try:
                    final = deepseek.chat.completions.create(
                        model="deepseek-v4-flash",
                        messages=[{"role": "user", "content": format_prompt}],
                        temperature=0.7,
                        max_tokens=300,
                        timeout=30
                    )
                    result_text = final.choices[0].message.content
                    
                    if result_text and result_text.strip() not in ["", "...", "…"]:
                        return result_text
                    else:
                        # Если DeepSeek вернул пустоту — отдаём сырые данные
                        return f"Нашёл информацию по запросу '{query}':\n\n{results_text}"
                        
                except Exception as e:
                    logger.error(f"❌ Ошибка форматирования: {e}")
                    return f"Нашёл информацию по запросу '{query}':\n\n{results_text}"
            else:
                return f"Не удалось найти информацию по запросу '{query}'. Попробуй переформулировать запрос 😊"
        
        # 2. Поиск в истории
        history_match = re.search(r'\[HISTORY:\s*(.+?)\]', reply)
        if history_match:
            query = history_match.group(1).strip()
            logger.info(f"📚 Поиск в истории: '{query}'")
            
            results = search_history(user_id, query)
            
            if results:
                text_results = "\n".join([f"{r['role']}: {r['content']}" for r in results[:5]])
                format_prompt = f"Вот что нашлось в истории по запросу '{query}':\n{text_results}\n\nОтветь пользователю коротко, с эмодзи."
                
                try:
                    final = deepseek.chat.completions.create(
                        model="deepseek-v4-flash",
                        messages=[{"role": "user", "content": format_prompt}],
                        temperature=0.7,
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
        
        # 3. Если нет команд — возвращаем ответ DeepSeek
        if not reply or reply.strip() in ["", "...", "…"]:
            return "Что-то пошло не так. Попробуй переформулировать вопрос 😊"
        
        return reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка в deepseek_process: {e}")
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
        
        # === ГОЛОС ===
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
                logger.error(f"❌ Ошибка обработки голоса: {e}")
                await send_message(user_id, "⚠️ Ошибка обработки голоса.")
                return JSONResponse({"ok": True})
        
        # === ТЕКСТ ===
        if "text" in msg:
            text = msg["text"].strip()
        
        if not text:
            return JSONResponse({"ok": True})
        
        # Сохраняем сообщение пользователя
        save_message(user_id, "user", text)
        
        # Обрабатываем запрос
        reply = deepseek_process(user_id, text)
        
        # Сохраняем ответ
        save_message(user_id, "assistant", reply)
        
        # Отправляем пользователю
        await send_message(user_id, reply)
        
        return JSONResponse({"ok": True})
        
    except Exception as e:
        logger.error(f"❌ Ошибка в webhook: {e}")
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
