import os
import re
import hashlib
import logging
import requests
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
DS_KEY = os.getenv("DEEPSEEK_API_KEY")
DS_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SB_URL = os.getenv("SUPABASE_URL")
SB_KEY = os.getenv("SUPABASE_KEY")
YA_KEY = os.getenv("YANDEX_API_KEY")
YA_FOLDER = os.getenv("YANDEX_FOLDER_ID")
ADMIN = os.getenv("ADMIN_CHAT_ID", "5818548555")
PASS = os.getenv("ACCESS_PASSWORD", "12355")

ai = OpenAI(api_key=DS_KEY, base_url=DS_BASE)
supa = create_client(SB_URL, SB_KEY) if SB_URL and SB_KEY else None

app = FastAPI()
state = {}
spam = {}
cache = {}

# ---- БАЗА ----
def db(table, action, data=None, where=None):
    if not supa:
        return None
    try:
        q = supa.table(table)
        if action == "select":
            q = q.select("*")
            if where:
                for k, v in where.items():
                    q = q.eq(k, v)
            return q.execute().data
        elif action == "insert":
            return q.insert(data).execute().data
        elif action == "update":
            if where:
                for k, v in where.items():
                    q = q.eq(k, v)
            return q.update(data).execute().data
    except Exception as e:
        logger.error(f"DB: {e}")
        return None

def get_user(user_id, field):
    res = db("users", "select", where={"user_id": str(user_id)})
    return res[0][field] if res and res[0].get(field) else None

def set_user(user_id, field, value):
    if get_user(user_id, "user_id"):
        db("users", "update", {field: value}, where={"user_id": str(user_id)})
    else:
        db("users", "insert", {"user_id": str(user_id), field: value})

def save_msg(user_id, role, text):
    db("history", "insert", {"user_id": str(user_id), "role": role, "content": text})

def get_msgs(user_id, limit=10):
    res = db("history", "select", where={"user_id": str(user_id)})
    return res[-limit:] if res else []

def find_msgs(user_id, word):
    res = db("history", "select", where={"user_id": str(user_id)})
    if not res:
        return []
    return [m for m in res if word in m["content"].lower()][-5:]

def search_web(query, city=""):
    if city and city != "Москва":
        query = f"{query} {city}"
    key = hashlib.md5(query.encode()).hexdigest()
    if key in cache and datetime.now() - cache[key]["ts"] < timedelta(hours=24):
        return cache[key]["data"]
    try:
        client = OpenAI(api_key=YA_KEY, base_url="https://ai.api.cloud.yandex.net/v1", project=YA_FOLDER)
        resp = client.responses.create(
            prompt={"id": "fvt3te2kgttig7u3a1fb"},
            input=query,
            tools=[{"type": "web_search"}]
        )
        result = resp.output_text
        cache[key] = {"data": result, "ts": datetime.now()}
        return result
    except:
        return None

def send(chat_id, text):
    if not text:
        return
    if len(text) > 4096:
        text = text[:4093] + "..."
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=20
        )
    except Exception as e:
        logger.error(f"Send: {e}")

def clean_answer(text):
    """Обрезает ответ до 3 предложений"""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) > 3:
        return ' '.join(sentences[:3]) + '...'
    return text

@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        msg = body["message"]
        uid = msg["from"]["id"]
        text = msg.get("text", "")

        # ---- ДОСТУП ----
        if str(uid) != ADMIN:
            if not get_user(uid, "user_id"):
                if uid in state and state[uid] == "wait_pass":
                    if text == PASS:
                        set_user(uid, "user_id", str(uid))
                        send(uid, "✅ Доступ открыт")
                        del state[uid]
                    else:
                        send(uid, "❌ Неверный пароль")
                    return JSONResponse({"ok": True})
                send(uid, "🔐 Пароль?")
                state[uid] = "wait_pass"
                return JSONResponse({"ok": True})

        # ---- ЗАЩИТА ----
        if uid not in spam:
            spam[uid] = deque(maxlen=3)
        if text in spam[uid]:
            return JSONResponse({"ok": True})
        spam[uid].append(text)

        # ---- КОМАНДЫ ----
        if text == "/start":
            name = get_user(uid, "name")
            if name:
                send(uid, f"Привет, {name}! Чем могу помочь?")
            else:
                send(uid, "Привет! Как тебя зовут?")
                state[uid] = "wait_name"
            return JSONResponse({"ok": True})

        if uid in state and state[uid] == "wait_name":
            if len(text.strip()) > 1:
                set_user(uid, "name", text.strip())
                send(uid, f"Приятно, {text.strip()}! Город?")
                state[uid] = "wait_city"
            else:
                send(uid, "Имя?")
            return JSONResponse({"ok": True})

        if uid in state and state[uid] == "wait_city":
            if len(text.strip()) > 1:
                set_user(uid, "city", text.strip())
                send(uid, f"Запомнил. Задавай вопросы.")
                del state[uid]
            else:
                send(uid, "Город?")
            return JSONResponse({"ok": True})

        # ---- ОСНОВНАЯ ЛОГИКА ----
        save_msg(uid, "user", text)
        name = get_user(uid, "name") or "Гость"
        city = get_user(uid, "city") or "Москва"

        # Контекст
        recent = get_msgs(uid, 10)
        ctx = "\n".join([f"{m['role']}: {m['content'][:200]}" for m in recent]) if recent else ""

        # Поиск в истории
        words = re.findall(r"\b\w{4,}\b", text.lower())
        found = []
        for w in words:
            if w in ["найди", "меня", "что", "это", "когда", "сколько"]:
                continue
            found.extend(find_msgs(uid, w))
        found_text = "\n".join([f"{m['role']}: {m['content'][:200]}" for m in found]) if found else ""

        # Системный промпт
        system_prompt = """Ты — AURA, личный ассистент в Telegram. Ты отвечаешь как живой человек — коротко, по делу, с душой. Ты всегда помнишь контекст диалога. Если ты не знаешь ответа — честно говоришь «не знаю», а не придумываешь. Если нужно — ты можешь поискать в интернете. Твой тон — спокойный, уверенный, без воды."""
        
        user_prompt = f"""Пользователь: {name} из города {city}.

Недавние сообщения:
{ctx if ctx else "Нет истории"}

Найдено по ключевым словам:
{found_text if found_text else "Ничего"}

Вопрос: {text}

Ответь кратко (1–3 предложения), по делу. Не повторяй историю как робот. Будь естественным."""

        # ---- ОТВЕТ ----
        try:
            resp = ai.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=300,
                temperature=0.8
            )
            answer = clean_answer(resp.choices[0].message.content)
        except:
            answer = "Ошибка. Попробуй позже."

        # ---- ИНТЕРНЕТ ----
        if "не знаю" in answer.lower() or "не могу" in answer.lower() or "нет информации" in answer.lower() or len(answer) < 15:
            raw = search_web(text, city)
            if raw:
                prompt2 = f"""Ты — AURA. Ответь кратко (1–3 предложения) на вопрос.
Вопрос: {text}
Данные из интернета: {raw[:600]}
Ответ:"""
                try:
                    resp2 = ai.chat.completions.create(
                        model="deepseek-chat",
                        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt2}],
                        max_tokens=300,
                        temperature=0.7
                    )
                    answer = clean_answer(resp2.choices[0].message.content)
                except:
                    answer = clean_answer(raw[:300])

        # ---- ССЫЛКИ ----
        if "фильм" in text.lower() and "http" not in answer:
            answer += f"\n\n[🔗 Смотреть](https://yandex.ru/video/search?text={text.replace(' ', '+')})"
        elif "билет" in text.lower() and "http" not in answer:
            answer += f"\n\n[✈️ Билеты](https://www.aviasales.ru/search?q={text.replace(' ', '+')})"
        elif "клиника" in text.lower() and "http" not in answer:
            answer += f"\n\n[🔍 Поиск](https://yandex.ru/search/?text={text.replace(' ', '+')}%20{city})"

        send(uid, answer)
        save_msg(uid, "assistant", answer)

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"Webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
def root():
    return {"status": "OK"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
