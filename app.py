import os
import re
import tempfile
import json
from datetime import datetime, timedelta
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
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# ===== ПОДКЛЮЧЕНИЯ =====
print("🚀 БОТ ЗАПУЩЕН. АРХИТЕКТУРА: БОТ — ФАСАД, ВСЁ РЕШАЕТ DEEPSEEK")

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
# 1. БОТ — РАБОТА С БАЗОЙ (ТОЛЬКО ПО КОМАНДЕ DEEPSEEK)
# ============================================================

def save_message(user_id, role, content):
    if not supabase:
        return None
    try:
        result = supabase.table("history").insert({
            "user_id": user_id,
            "role": role,
            "content": content,
            "created_at": datetime.now().isoformat()
        }).execute()
        print(f"💾 Сохранено в history: {role} -> {content[:30]}...")
        if result.data and len(result.data) > 0:
            return result.data[0].get('id')
        return None
    except Exception as e:
        print(f"❌ Ошибка сохранения: {e}")
        return None

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
        print(f"❌ Ошибка загрузки: {e}")
        return []

def get_all_history(user_id, limit=1000):
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at, id")\
            .eq("user_id", user_id)\
            .order("created_at", desc=False)\
            .limit(limit)\
            .execute()
        return res.data if res.data else []
    except Exception as e:
        print(f"❌ Ошибка загрузки всей истории: {e}")
        return []

def search_history(user_id, query, exclude_id=None):
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at, id")\
            .eq("user_id", user_id)\
            .ilike("content", f"%{query}%")\
            .order("created_at", desc=True)\
            .limit(30)\
            .execute()
        results = res.data if res.data else []
        if exclude_id:
            results = [r for r in results if r.get('id') != exclude_id]
        return results[:10]
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
# 2. БОТ — ПОИСК В ИНТЕРНЕТЕ (ТОЛЬКО ПО КОМАНДЕ DEEPSEEK)
# ============================================================

def search_web(query, max_results=5):
    if not TAVILY_API_KEY:
        return None
    
    print(f"🌐 Бот ищет в интернете: '{query}'")
    
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": max_results,
            "include_answer": True,
            "include_raw_content": False,
            "search_depth": "basic"
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
# 3. БОТ — ФАСАД: ПЕРЕДАЁТ ВСЁ DEEPSEEK
# ============================================================

def deepseek_decide(user_id, text, user_name=None, user_city=None):
    """
    DeepSeek решает, что делать с запросом.
    Возвращает JSON с инструкцией для бота.
    """
    
    # 1. Загружаем контекст
    history = get_recent_history(user_id, limit=15)
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])
    
    # 2. Формируем запрос к DeepSeek
    prompt = f"""
Ты — AURA, живой собеседник. Твоя задача — обработать запрос пользователя.

Пользователь: {user_name or "Неизвестно"}
Город пользователя: {user_city or "Неизвестно"}

История диалога (последние 15 сообщений):
{history_text}

Новое сообщение пользователя: "{text}"

Твоя задача — определить, что нужно сделать, и вернуть JSON с инструкцией для бота.

Доступные действия для бота:
1. "reply" — ответить самому (текст ответа)
2. "search_history" — найти в истории диалога (ключевое слово для поиска)
3. "search_web" — найти в интернете через Tavily (поисковый запрос)
4. "save_name" — запомнить имя пользователя (имя)
5. "save_city" — запомнить город пользователя (город)
6. "get_time" — вернуть текущее время
7. "get_currency" — вернуть курс валют
8. "get_weather" — вернуть погоду в городе

Правила:
- Если вопрос простой (приветствие, "кто ты", "ты дипсик") → reply
- Если спрашивает имя/город → reply из памяти или спросить
- Если нужно что-то найти из истории → search_history
- Если нужно что-то найти в интернете → search_web
- Если спрашивает время → get_time
- Если спрашивает курс → get_currency
- Если спрашивает погоду → get_weather

Ответь ТОЛЬКО JSON (без лишнего текста):
{{
    "action": "reply|search_history|search_web|save_name|save_city|get_time|get_currency|get_weather",
    "data": "текст для действия"
}}
"""
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=200
        )
        result = response.choices[0].message.content.strip()
        print(f"🧠 DeepSeek решил: {result}")
        
        # Парсим JSON
        # Ищем JSON в ответе
        json_match = re.search(r'\{[^{}]*\}', result)
        if json_match:
            return json.loads(json_match.group())
        return None
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
        return {"action": "reply", "data": "Что-то пошло не так. Попробуй ещё раз 😊"}

def execute_action(user_id, action, data, user_name=None, user_city=None):
    """
    Бот выполняет действие, которое решил DeepSeek
    """
    
    if action == "reply":
        return data
    
    elif action == "search_history":
        results = search_history(user_id, data)
        if results:
            # Передаём результаты DeepSeek для форматирования
            return format_history_results(results, data, user_name, user_city)
        return "Ничего не нашёл в истории. Попробуй переформулировать вопрос 😊"
    
    elif action == "search_web":
        search_data = search_web(data)
        if search_data and search_data.get("results"):
            return format_web_results(search_data, data, user_name, user_city)
        return "Не удалось найти информацию в интернете. Попробуй переформулировать вопрос 😊"
    
    elif action == "save_name":
        if data and len(data) > 1:
            save_fact(user_id, "name", data)
            return f"Запомнил! Тебя зовут **{data}** 😊"
        return "Не удалось запомнить имя. Скажи: 'Меня зовут ...'"
    
    elif action == "save_city":
        if data and len(data) > 2:
            save_fact(user_id, "city", data)
            return f"Запомнил! Ты из **{data}** 😊"
        return "Не удалось запомнить город. Скажи: 'Я живу в ...'"
    
    elif action == "get_time":
        now = datetime.now()
        return f"Сейчас **{now.strftime('%H:%M')}**, {now.strftime('%d.%m.%Y')} 😊"
    
    elif action == "get_currency":
        try:
            response = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
            data = response.json()
            rate = data.get("rates", {}).get("RUB")
            if rate:
                return f"Курс доллара к рублю: **{rate:.2f}** 😊"
        except:
            pass
        return "Не удалось получить курс. Попробуй позже 😊"
    
    elif action == "get_weather":
        city = data or user_city
        if not city:
            return "Скажи, в каком городе узнать погоду 😊"
        try:
            response = requests.get(f"https://wttr.in/{city}?format=%C+%t", timeout=10)
            if response.status_code == 200:
                return f"Погода в {city}: {response.text.strip()} 😊"
        except:
            pass
        return f"Не удалось получить погоду для {city}. Проверь название 😊"
    
    return "Не понял команду. Попробуй переформулировать вопрос 😊"

# ============================================================
# 4. ФОРМАТИРОВАНИЕ РЕЗУЛЬТАТОВ (DEEPSEEK)
# ============================================================

def format_history_results(results, query, user_name=None, user_city=None):
    messages = []
    for r in results[:5]:
        content = r['content']
        if content.startswith('📚') or content.startswith('assistant:'):
            continue
        messages.append(content)
    
    if not messages:
        return "Ничего не нашёл в истории по этому запросу 😊"
    
    history_text = "\n".join(messages)
    name_context = f"Пользователь: {user_name}." if user_name else ""
    
    prompt = f"""
    Пользователь спросил: "{query}"
    
    Вот что нашлось в истории:
    {history_text}
    
    {name_context}
    
    Ответь КОРОТКО (2-3 предложения) как в обычном разговоре.
    Используй эмодзи 😊🔥
    """
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200
        )
        return response.choices[0].message.content.strip()
    except:
        return f"В истории нашлось: {history_text[:300]}..."

def format_web_results(search_data, query, user_name=None, user_city=None):
    results = search_data.get("results", [])
    answer = search_data.get("answer", "")
    
    results_text = ""
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "Без названия")
        content = r.get("content", "")[:400]
        url = r.get("url", "")
        results_text += f"\n{i}. {title}\n   {content}\n   Источник: {url}\n"
    
    prompt = f"""
    Пользователь спросил: "{query}"
    
    Вот что нашлось в интернете:
    {results_text}
    
    Ответь КОРОТКО (2-3 предложения) как в обычном разговоре.
    Если есть ответ (answer) — используй его.
    Дай ссылку на источник.
    Используй эмодзи 😊🔥
    """
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=250
        )
        return response.choices[0].message.content.strip()
    except:
        if answer:
            return f"Вот что я нашёл: {answer[:500]}"
        if results:
            first = results[0]
            return f"Вот что я нашёл: {first.get('title', '')}\n\n{first.get('content', '')[:300]}\n\nИсточник: {first.get('url', '')}"
        return "Ничего не нашёл в интернете. Попробуй переформулировать вопрос 😊"

# ============================================================
# 5. РАСПОЗНАВАНИЕ ГОЛОСА (GROQ WHISPER)
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
# 6. ОТПРАВКА СООБЩЕНИЙ
# ============================================================

async def send_chat_action(chat_id, action="typing"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    data = {"chat_id": chat_id, "action": action}
    try:
        requests.post(url, json=data)
    except Exception as e:
        print(f"❌ Ошибка отправки action: {e}")

async def send_message(chat_id, text):
    await send_chat_action(chat_id, "typing")
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=data)
    except Exception as e:
        print(f"❌ Ошибка отправки сообщения: {e}")

# ============================================================
# 7. ОСНОВНАЯ ЛОГИКА (WEBHOOK)
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

        # ===== ОБРАБОТКА ГОЛОСОВЫХ =====
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

        # ===== БОТ СОХРАНЯЕТ СООБЩЕНИЕ =====
        save_message(user_id, "user", text)

        # ===== БОТ ПОЛУЧАЕТ ДАННЫЕ О ПОЛЬЗОВАТЕЛЕ =====
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")

        # ===== БОТ ПЕРЕДАЁТ ВСЁ DEEPSEEK =====
        decision = deepseek_decide(user_id, text, user_name, user_city)

        if decision:
            action = decision.get("action")
            data = decision.get("data")

            # ===== БОТ ВЫПОЛНЯЕТ ДЕЙСТВИЕ =====
            reply = execute_action(user_id, action, data, user_name, user_city)
        else:
            reply = "Что-то пошло не так. Попробуй ещё раз 😊"

        # ===== БОТ ОТПРАВЛЯЕТ ОТВЕТ =====
        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)

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
