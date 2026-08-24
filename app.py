import os
import json
import httpx
import asyncio
import logging
import tempfile
import hashlib
import base64
import re
import requests
from datetime import datetime, timedelta
from collections import deque
from bs4 import BeautifulSoup
import pytz
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv

# ===== НАСТРОЙКА =====
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== КЛЮЧИ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
GIS_API_KEY = os.getenv("GIS_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "5818548555")

AGENT_SEARCH_ID = os.getenv("YANDEX_AGENT_ID", "fvt3te2kgttig7u3a1fb")
AGENT_RESEARCH_ID = os.getenv("YANDEX_AGENT_RESEARCH_ID", "fvti80ngse2778agbmdl")
AGENT_REASONING_ID = os.getenv("YANDEX_AGENT_REASONING_ID", "fvtg0c38oi7n43d0n9gf")

ACCESS_PASSWORD = "12355"

# ===== КЛИЕНТЫ =====
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

# ===== SUPABASE =====
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Supabase подключён")

app = FastAPI()
user_states = {}
user_last_requests = {}
agent_cache = {}

# ============================================================
# 1. ПАМЯТЬ И ПОРТРЕТ
# ============================================================
def save_fact(user_id, key, value):
    if not supabase: return
    try:
        existing = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        if existing.data:
            supabase.table("user_memory").update({"value": value}).eq("user_id", user_id).eq("key", key).execute()
        else:
            supabase.table("user_memory").insert({"user_id": user_id, "key": key, "value": value, "created_at": datetime.now().isoformat()}).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения факта: {e}")

def get_fact(user_id, key):
    if not supabase: return None
    try:
        res = supabase.table("user_memory").select("value").eq("user_id", user_id).eq("key", key).execute()
        return res.data[0]["value"] if res.data else None
    except:
        return None

def get_portrait(user_id):
    if not supabase: return None
    try:
        res = supabase.table("user_portrait").select("*").eq("user_id", user_id).execute()
        return res.data[0] if res.data else None
    except:
        return None

def save_portrait_field(user_id, field, value):
    if not supabase: return
    try:
        existing = supabase.table("user_portrait").select("user_id").eq("user_id", user_id).execute()
        if existing.data:
            supabase.table("user_portrait").update({field: value, "updated_at": datetime.now().isoformat()}).eq("user_id", user_id).execute()
        else:
            supabase.table("user_portrait").insert({"user_id": user_id, field: value, "updated_at": datetime.now().isoformat()}).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения портрета: {e}")

# ============================================================
# 2. ИСТОРИЯ
# ============================================================
def save_message(user_id, role, content):
    if not supabase: return
    try:
        supabase.table("history").insert({"user_id": user_id, "role": role, "content": content, "created_at": datetime.now().isoformat()}).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения истории: {e}")

def get_recent_history(user_id, limit=20):
    if not supabase: return []
    try:
        res = supabase.table("history").select("role, content, created_at").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
        return list(reversed(res.data)) if res.data else []
    except:
        return []

def clear_user_history(user_id):
    if not supabase: return
    try:
        supabase.table("history").delete().eq("user_id", user_id).execute()
    except Exception as e:
        logger.error(f"❌ Ошибка очистки: {e}")

# ============================================================
# 3. ПОИСК В ИСТОРИИ
# ============================================================
def find_in_history(history, text):
    if not history: return []
    stop_words = ["напомни", "скажи", "что", "я", "говорил", "про", "о", "в", "и", "с", "на", "за", "по", "из", "от", "для", "мне", "ты", "мы", "они", "он", "она", "оно", "вот", "этот", "эта", "это", "эти", "который", "которая", "которые", "которое", "мне", "меня", "тебя", "тебе", "ещё", "было", "были", "была"]
    keywords = [w for w in text.lower().split() if len(w) > 2 and w not in stop_words]
    if not keywords: return []
    found = []
    for msg in history:
        if any(k in msg['content'].lower() for k in keywords):
            found.append(msg)
    return found

def extract_facts(found_messages, text):
    if not found_messages: return None
    scored = []
    keywords = [w for w in text.lower().split() if len(w) > 2]
    for msg in found_messages:
        score = sum(1 for k in keywords if k in msg['content'].lower())
        scored.append((score, msg))
    scored.sort(key=lambda x: x[0], reverse=True)
    parts = []
    for _, msg in scored[:5]:
        role = "Пользователь" if msg['role'] == 'user' else "AURA"
        parts.append(f"{role}: {msg['content'][:300]}")
    return "\n\n".join(parts) if parts else None

# ============================================================
# 4. РАСПОЗНАВАНИЕ КАРТИНОК И ДОКУМЕНТОВ
# ============================================================
async def recognize_image_with_deepseek(image_url: str) -> str:
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code != 200:
            return "⚠️ Не удалось загрузить изображение."
        image_base64 = base64.b64encode(response.content).decode('utf-8')
        prompt = "Опиши картинку коротко, 2-3 предложения. Если есть текст — скажи, что текст есть, и предложи зачитать его."
        vision_client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        response = vision_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}]}],
            max_tokens=120,
            temperature=0.5
        )
        return response.choices[0].message.content or "Не удалось распознать."
    except Exception as e:
        logger.error(f"❌ Ошибка Vision: {e}")
        return "⚠️ Ошибка распознавания."

def parse_website(url: str) -> str:
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return "⚠️ Не удалось загрузить сайт."
        soup = BeautifulSoup(resp.text, 'html.parser')
        text = ' '.join([p.get_text() for p in soup.find_all('p')][:10])
        return text[:2000]
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга: {e}")
        return "⚠️ Ошибка загрузки сайта."

# ============================================================
# 5. АНАЛИЗ ТОНАЛЬНОСТИ
# ============================================================
def analyze_sentiment(text: str) -> str:
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": "Определи настроение: positive, neutral, negative. Верни только одно слово."}, {"role": "user", "content": text}],
            max_tokens=10,
            temperature=0.3
        )
        return response.choices[0].message.content.strip().lower()
    except:
        return "neutral"

# ============================================================
# 6. КЕШ
# ============================================================
def get_cached_response(hash_val):
    if hash_val in agent_cache:
        entry = agent_cache[hash_val]
        if datetime.now() - entry["timestamp"] < timedelta(hours=24):
            return entry["response"]
        else:
            del agent_cache[hash_val]
    return None

def cache_response(hash_val, response):
    agent_cache[hash_val] = {"response": response, "timestamp": datetime.now()}

# ============================================================
# 7. ВЫЗОВ ЯНДЕКС-АГЕНТОВ
# ============================================================
def call_yandex_agent(agent_id, user_text, user_name="", user_city="", budget=""):
    hash_val = hashlib.md5(f"{agent_id}:{user_text}:{user_name}:{user_city}:{budget}".encode()).hexdigest()
    cached = get_cached_response(hash_val)
    if cached: return cached
    try:
        client = OpenAI(api_key=YANDEX_API_KEY, base_url="https://ai.api.cloud.yandex.net/v1", project=YANDEX_FOLDER_ID)
        response = client.responses.create(
            prompt={"id": agent_id, "variables": {"user_name": user_name or "Гость", "user_city": user_city or "Москва", "budget": budget or "не указан"}},
            input=user_text,
            tools=[{"type": "web_search", "filters": {"allowed_domains": []}, "search_context_size": "low"}]
        )
        result = response.output_text
        cache_response(hash_val, result)
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка агента: {e}")
        return ""

# ============================================================
# 8. ОТПРАВКА СООБЩЕНИЙ
# ============================================================
async def send_typing(chat_id):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except:
        pass

async def send_message(chat_id, text):
    if not text: text = "😅 Не понял."
    if len(text) > 4096: text = text[:4093] + "..."
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=30)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

# ============================================================
# 9. ОСНОВНАЯ ЛОГИКА (DEEPSEEK)
# ============================================================
async def deepseek_interview(user_id, text, history):
    user_name = get_fact(user_id, "name") or "Гость"
    user_city = get_fact(user_id, "city") or "Москва"
    portrait = get_portrait(user_id)

    portrait_context = ""
    if portrait:
        parts = []
        if portrait.get('name'): parts.append(f"имя: {portrait['name']}")
        if portrait.get('city'): parts.append(f"город: {portrait['city']}")
        if portrait.get('hobbies'): parts.append(f"увлечения: {', '.join(portrait['hobbies'][:3])}")
        if portrait.get('profession'): parts.append(f"профессия: {portrait['profession']}")
        if parts: portrait_context = "ПОРТРЕТ: " + ", ".join(parts) + "."

    found = find_in_history(history, text)
    facts = extract_facts(found, text) if found else None

    if facts:
        context = f"В ИСТОРИИ:\n{facts}"
    else:
        context = "В ИСТОРИИ НИЧЕГО НЕ НАЙДЕНО."

    prompt = f"""Ты — AURA, личный ассистент. Отвечай коротко — 2-3 предложения. Без приветствий. Будь вовлечён и эмпатичен.

{portrait_context}

{context}

ПОЛЬЗОВАТЕЛЬ: "{text}"

ОТВЕТЬ (2-3 предложения, по делу):"""
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.85,
            max_tokens=120,
            timeout=20
        )
        reply = response.choices[0].message.content
        # Анализ тональности и сохранение
        mood = analyze_sentiment(text)
        save_portrait_field(user_id, "mood_trend", mood)
        return {"reply": reply, "found_in_history": bool(facts)}
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return {"reply": "😅 Не понял.", "found_in_history": False}

# ============================================================
# 10. WEBHOOK (ОСНОВНОЙ)
# ============================================================
@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body: return JSONResponse({"ok": True})
        msg = body["message"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")

        # === АВТОРИЗАЦИЯ ===
        if str(user_id) != ADMIN_CHAT_ID:
            if not supabase or not supabase.table("users").select("user_id").eq("user_id", user_id).execute().data:
                if user_id in user_states and user_states[user_id].get("state") == "entering_password":
                    if text == ACCESS_PASSWORD:
                        supabase.table("users").insert({"user_id": user_id}).execute()
                        await send_message(user_id, "✅ Доступ разрешён! Добро пожаловать в AURA.")
                        del user_states[user_id]
                        return JSONResponse({"ok": True})
                    else:
                        await send_message(user_id, "❌ Неверный пароль.")
                        return JSONResponse({"ok": True})
                await send_message(user_id, "🔐 Введите пароль для входа:")
                user_states[user_id] = {"state": "entering_password"}
                return JSONResponse({"ok": True})

        # === ПРИВЕТСТВИЕ ПРИ ПЕРВОМ ЗАПУСКЕ ===
        if not get_fact(user_id, "name"):
            await send_message(user_id, "Привет! Я AURA — твой личный ассистент. Давай познакомимся. Как тебя зовут?")
            user_states[user_id] = {"state": "collecting_name"}
            return JSONResponse({"ok": True})

        # === ОБРАБОТКА ИЗОБРАЖЕНИЙ ===
        if "photo" in msg or "document" in msg:
            file_id = msg["photo"][-1]["file_id"] if "photo" in msg else msg["document"]["file_id"]
            file_resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}").json()
            if file_resp.get("ok"):
                image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp['result']['file_path']}"
                recognized = await recognize_image_with_deepseek(image_url)
                await send_message(user_id, recognized)
                save_message(user_id, "assistant", recognized)
            return JSONResponse({"ok": True})

        # === ОБРАБОТКА ГОЛОСА ===
        if "voice" in msg:
            file_id = msg["voice"]["file_id"]
            file_resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}").json()
            if file_resp.get("ok"):
                audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_resp['result']['file_path']}"
                resp = requests.get(audio_url, timeout=30)
                if resp.status_code == 200:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
                        tmp.write(resp.content)
                        tmp_path = tmp.name
                    with open(tmp_path, "rb") as f:
                        result = groq.audio.transcriptions.create(file=(tmp_path, f.read()), model="whisper-large-v3-turbo", language="ru")
                    os.unlink(tmp_path)
                    text = result.text
                    save_message(user_id, "user", text)
            return JSONResponse({"ok": True})

        if not text:
            return JSONResponse({"ok": True})

        # === ЗАЩИТА ОТ ПОВТОРОВ ===
        hash_val = hashlib.md5(f"{user_id}:{text}".encode()).hexdigest()
        if user_id not in user_last_requests:
            user_last_requests[user_id] = deque(maxlen=5)
        if hash_val in user_last_requests[user_id]:
            return JSONResponse({"ok": True})
        user_last_requests[user_id].append(hash_val)

        # === ОБРАБОТКА ССЫЛОК ===
        if text.startswith("http"):
            content = parse_website(text)
            await send_message(user_id, f"🔍 Я проанализировал страницу. Вот краткое содержание:\n\n{content[:500]}\n\nЧто именно тебя интересует?")
            save_message(user_id, "assistant", f"Проанализировал ссылку: {text}")
            return JSONResponse({"ok": True})

        # === ВРЕМЯ ===
        if any(w in text.lower() for w in ["сколько время", "который час"]):
            city = get_fact(user_id, "city") or "Москва"
            tz = pytz.timezone({"москва": "Europe/Moscow"}.get(city.lower(), "Europe/Moscow"))
            await send_message(user_id, f"⏰ Сейчас {datetime.now(tz).strftime('%H:%M')} по местному времени ({city}).")
            return JSONResponse({"ok": True})

        # === КОМАНДЫ ===
        if text == "/start":
            await send_message(user_id, "Привет. Я AURA. Чем могу помочь?")
            return JSONResponse({"ok": True})
        if text.lower() in ["/clear", "/reset"]:
            clear_user_history(user_id)
            await send_message(user_id, "✅ История очищена.")
            return JSONResponse({"ok": True})

        # === ПОЛУЧЕНИЕ ИМЕНИ ===
        if user_id in user_states and user_states[user_id].get("state") == "collecting_name":
            name = text.strip()
            if name and len(name) > 1 and name[0].isupper():
                save_fact(user_id, "name", name)
                save_portrait_field(user_id, "name", name)
                await send_message(user_id, f"Приятно познакомиться, {name}! ✈️ В каком городе ты живёшь?")
                user_states[user_id] = {"state": "collecting_city"}
            else:
                await send_message(user_id, "Пожалуйста, напиши своё имя с заглавной буквы.")
            return JSONResponse({"ok": True})

        if user_id in user_states and user_states[user_id].get("state") == "collecting_city":
            city = text.strip()
            if city and len(city) > 1 and city[0].isupper():
                save_fact(user_id, "city", city)
                save_portrait_field(user_id, "city", city)
                await send_message(user_id, f"Отлично, {get_fact(user_id, 'name')}! Я запомнил твой город. Чем ты занимаешься?")
                user_states[user_id] = {"state": "collecting_profession"}
            else:
                await send_message(user_id, "Напиши город с заглавной буквы.")
            return JSONResponse({"ok": True})

        if user_id in user_states and user_states[user_id].get("state") == "collecting_profession":
            profession = text.strip()
            if profession:
                save_fact(user_id, "profession", profession)
                save_portrait_field(user_id, "profession", profession)
                await send_message(user_id, f"Понял, {get_fact(user_id, 'name')}! Теперь я знаю о тебе немного больше. Задавай любые вопросы.")
            else:
                await send_message(user_id, "Расскажи, чем ты занимаешься.")
            del user_states[user_id]
            return JSONResponse({"ok": True})

        # === ОСНОВНАЯ ЛОГИКА ===
        save_message(user_id, "user", text)
        history = get_recent_history(user_id, limit=20)

        # Проверяем историю
        result = await deepseek_interview(user_id, text, history)
        reply = result.get("reply", "😅 Не понял.")
        found = result.get("found_in_history", False)

        if found:
            await send_message(user_id, reply)
            save_message(user_id, "assistant", reply)
            return JSONResponse({"ok": True})

        # Если нет в истории — ищем в интернете
        user_name = get_fact(user_id, "name") or "Гость"
        user_city = get_fact(user_id, "city") or "Москва"
        budget = get_fact(user_id, "budget_travel") or ""

        if "билет" in text.lower():
            agent_id = AGENT_SEARCH_ID
            raw = call_yandex_agent(agent_id, text, user_name, user_city, budget)
            if raw:
                prompt = f"Ты — AURA. Отвечай коротко — 2-3 предложения. Добавь ссылку на билеты.\nСырой ответ: {raw}\nТвой ответ:"
                resp = deepseek.chat.completions.create(model="deepseek-chat", messages=[{"role": "system", "content": prompt}], max_tokens=120)
                answer = resp.choices[0].message.content
                await send_message(user_id, answer)
                save_message(user_id, "assistant", answer)
            else:
                await send_message(user_id, "Не нашёл билеты. Попробуй уточнить.")
        else:
            if any(w in text.lower() for w in ["посоветуй", "что лучше"]):
                agent_id = AGENT_REASONING_ID
            elif any(w in text.lower() for w in ["сравни", "проанализируй"]):
                agent_id = AGENT_RESEARCH_ID
            else:
                agent_id = AGENT_SEARCH_ID

            raw = call_yandex_agent(agent_id, text, user_name, user_city, budget)
            if raw:
                prompt = f"Ты — AURA. Отвечай коротко — 2 предложения. Добавь ссылку если есть.\nСырой ответ: {raw}\nТвой ответ:"
                resp = deepseek.chat.completions.create(model="deepseek-chat", messages=[{"role": "system", "content": prompt}], max_tokens=120)
                answer = resp.choices[0].message.content
                await send_message(user_id, answer)
                save_message(user_id, "assistant", answer)
            else:
                await send_message(user_id, "Не нашёл в интернете. Попробуй уточнить.")

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
