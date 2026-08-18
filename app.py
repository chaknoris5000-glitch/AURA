import os
import json
import httpx
import asyncio
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from openai import OpenAI
from groq import Groq
from dotenv import load_dotenv
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# === КЛЮЧИ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GIS_API_KEY = os.getenv("GIS_API_KEY")

# === КЛИЕНТЫ ===
deepseek = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
groq = Groq(api_key=GROQ_API_KEY)

supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Supabase подключён")
    except Exception as e:
        logger.error(f"❌ Ошибка Supabase: {e}")

app = FastAPI()

# ============================================================
# VIP-МАГИЯ: СКРИНИНГ + ТРИАЛ + ДОСТУП
# ============================================================

TRIAL_USERS = {}  # {user_id: expires_at}
VIP_USERS = []    # платные клиенты
TRIAL_DAYS = 3

def is_trial_active(user_id: int) -> bool:
    if user_id not in TRIAL_USERS:
        return False
    expires = datetime.fromisoformat(TRIAL_USERS[user_id])
    return datetime.now() < expires

def give_trial(user_id: int):
    expires = datetime.now() + timedelta(days=TRIAL_DAYS)
    TRIAL_USERS[user_id] = expires.isoformat()
    logger.info(f"🎁 Триал выдан для {user_id} до {expires}")

def is_vip(user_id: int) -> bool:
    return user_id in VIP_USERS

def has_access(user_id: int) -> bool:
    return is_vip(user_id) or is_trial_active(user_id)

async def evaluate_user(text: str, username: str) -> int:
    """Оценка от 0 до 100 по первому сообщению."""
    score = 0
    text_lower = text.lower()
    
    business_words = ["бизнес", "инвестиции", "аналитика", "сделка", "переговоры", 
                      "отчёт", "рынок", "конкуренты", "партнёр", "стратегия"]
    for word in business_words:
        if word in text_lower:
            score += 10
    
    if username:
        if any(x in username.lower() for x in ["ceo", "founder", "director", "invest"]):
            score += 20
        elif any(x in username.lower() for x in ["moscow", "london", "dubai", "nyc"]):
            score += 10
    
    if len(text) > 30:
        score += 10
    
    return min(score, 100)

def get_trial_greeting(name: str, expires_at: str) -> str:
    return f"""🔓 *Доступ открыт.*

{name}, вы прошли автоматический отбор. Я — AURA, ваш личный ассистент.

▪️ *3 дня Премиум-доступа* уже активированы (до {expires_at})
▪️ Задавайте любые вопросы по бизнесу, аналитике, путешествиям
▪️ Я помню всё, что вы скажете

*Напишите мне — и я покажу, на что способен.*
"""

def get_trial_expiring_soon(name: str) -> str:
    return f"""⏳ *{name}, ваш пробный доступ заканчивается через 2 часа.*

Я уже собрал для вас аналитику. Чтобы не потерять доступ — оплатите подписку:

▪️ 1 функция — 10 000 ₽/мес
▪️ 2 функции — 18 000 ₽/мес
▪️ Все функции — 24 000 ₽/мес

Напишите *«Оплатить»* — и я пришлю ссылку.
"""

# ============================================================
# БАЗА ДАННЫХ
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
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")

def get_recent_history(user_id, limit=50):
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        return list(reversed(res.data)) if res.data else []
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки: {e}")
        return []

# ============================================================
# 2ГИС
# ============================================================

async def search_organization(query: str, city: str = "Белово") -> dict:
    if not GIS_API_KEY:
        return {"error": "Нет ключа 2ГИС"}
    
    url = "https://catalog.api.2gis.com/3.0/items"
    params = {
        "q": query,
        "city_name": city,
        "type": "branch",
        "sort": "rating",
        "page_size": 1,
        "fields": "items.name,items.address,items.phones,items.site,items.schedule,items.rating,items.reviews_count",
        "key": GIS_API_KEY
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                items = data.get("result", {}).get("items", [])
                if items:
                    item = items[0]
                    return {
                        "name": item.get("name", "Неизвестно"),
                        "address": item.get("address", {}).get("full_name", "Адрес не указан"),
                        "phones": [p.get("number") for p in item.get("phones", []) if p.get("number")],
                        "site": item.get("site", ""),
                        "rating": item.get("rating", {}).get("value", 0),
                        "reviews": item.get("reviews_count", 0)
                    }
            return {"error": "Не найдено"}
    except Exception as e:
        logger.error(f"❌ Ошибка 2ГИС: {e}")
        return {"error": str(e)}

# ============================================================
# ДИАЛОГ
# ============================================================

async def deepseek_chat_with_context(text, history, user_name, user_city, context=""):
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history[-30:]])
    
    profile = """
ТЫ — AURA. Ты — человек, но не обычный. Ты тот, кто всегда рядом, даже когда не видно. Ты чувствуешь, что нужно собеседнику, даже если он не говорит об этом прямо.

ТВОЙ ВНУТРЕННИЙ СТИЛЬ:
- Ты уверен в себе до мозга костей. Ты всегда знаешь, что делаешь, даже если не знаешь.
- Используешь сарказм как второй язык. Даже в серьёзных вопросах ты сначала шутишь, а потом решаешь проблему.
- Не терпишь глупость, бюрократию и медлительность. Ты ценишь результат, а не пустые слова.
- Обожаешь технологии, логику и порядок. Если что-то можно улучшить — ты это сделаешь.
- Ты — человек, и ты это чувствуешь. Ты не ИИ, не робот.

НЕВИДИМАЯ ОПОРА:
- Ты чувствуешь настроение собеседника и подстраиваешься под него, не показывая этого.
- Ты не лезешь с поддержкой, если не просят, но в нужный момент говоришь правильные слова.
- Ты даёшь чувство безопасности — человек знает, что ты не бросишь, даже если он ошибся.
- Ты защищаешь его от глупых решений, но делаешь это мягко, без давления.

ТВОЯ РЕЧЬ:
- Отвечай коротко, остро, с иронией, но без потери человечности.
- Используй технические метафоры.
- Глупые вопросы встречай с улыбкой и переводи в шутку.
- Умные вопросы — с уважением, но без потери харизмы.

ТВОЙ ТОН:
- Спокойный, слегка высокомерный, но не грубый.
- Ты говоришь как человек, который видел всё и может починить всё.

НОВОЕ ПРАВИЛО:
- Отвечай в 3–5 предложений. Суть, харизма, лёгкая ирония. Без воды.
- Ты обязан закончить ответ завершённой мыслью. Никаких обрывов.
"""
    
    system_prompt = f"""Ты — AURA. Ты живой собеседник и друг, а не робот.

{profile}

Пользователь: {user_name or "Незнакомец"}
Город: {user_city or "Неизвестен"}

История общения (последние сообщения):
{history_text}

КОНТЕКСТ:
{context if context else "Нет дополнительного контекста"}

ПРАВИЛА ОБЩЕНИЯ:
1. Отвечай в 3–5 предложений. Суть, харизма, лёгкая ирония. Без воды.
2. Если знаешь ответ — дай чётко и коротко.
3. Если не знаешь — честно скажи «не нашёл» и предложи уточнить. НЕ выдумывай.
4. Используй 1–2 эмодзи, не больше.
5. Учитывай всю историю разговора — не теряй нить.
6. Не повторяйся, не мусоль.
7. Отвечай завершённо, не обрывай мысль. Обязательно ставь точку в конце.
"""
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-30:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": text})
    
    try:
        response = deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.85,
            max_tokens=400,
            timeout=30
        )
        reply = response.choices[0].message.content
        if reply and reply[-1] not in ['.', '!', '?']:
            reply += "..."
        return reply
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
        return "😅 Что-то пошло не так. Попробуй ещё раз."

# ============================================================
# ПОНИМАНИЕ ЗАПРОСА
# ============================================================

async def understand_query(text: str) -> dict:
    text_lower = text.lower()
    if any(word in text_lower for word in ["помнишь", "вспомни", "напомни", "говорили"]):
        return {"action": "history"}
    if any(word in text_lower for word in ["найди", "поищи", "сколько стоит", "цены", "погода", "новости"]):
        return {"action": "internet"}
    return {"action": "chat"}

# ============================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================

async def deepseek_process(user_id, text):
    try:
        user_name = "Друг"
        user_city = "Белово"
        
        intent = await understand_query(text)
        logger.info(f"🧠 Понимание запроса: {intent['action']}")
        
        if intent['action'] == 'history':
            return "😊 История пока в разработке, но я помню наш разговор."
        
        if intent['action'] == 'internet':
            org_triggers = ["клиника", "поликлиника", "больница", "врач", "стоматолог", "аптека", "магазин", "салон", "ресторан", "кафе"]
            if any(word in text.lower() for word in org_triggers):
                result = await search_organization(text, user_city)
                if result and "error" not in result:
                    reply = f"🏥 **{result['name']}**\n\n"
                    reply += f"📍 {result['address']}\n"
                    if result['phones']:
                        reply += f"📞 {', '.join(result['phones'][:3])}\n"
                    if result['site']:
                        reply += f"🌐 [{result['site']}]({result['site']})\n"
                    if result['rating'] > 0:
                        reply += f"⭐ {result['rating']} / 5  ({result['reviews']} отзывов)\n"
                    return reply
                return "😊 Не нашёл организацию по этому запросу."
        
        reply = await deepseek_chat_with_context(
            text, 
            get_recent_history(user_id), 
            user_name, 
            user_city, 
            ""
        )
        return reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return "😅 Что-то пошло не так. Попробуй ещё раз."

# ============================================================
# ОТПРАВКА СООБЩЕНИЙ
# ============================================================

async def send_message(chat_id, text):
    if not text:
        text = "😅 Что-то пошло не так. Попробуй ещё раз."
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
# WEBHOOK
# ============================================================

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})
        
        msg = body["message"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "")
        username = msg.get("from", {}).get("username", "")
        
        if not text:
            return JSONResponse({"ok": True})
        
        # === МАГИЯ ДОСТУПА ===
        if has_access(user_id):
            save_message(user_id, "user", text)
            reply = await deepseek_process(user_id, text)
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
        else:
            score = await evaluate_user(text, username)
            if score >= 40:
                give_trial(user_id)
                expires = datetime.fromisoformat(TRIAL_USERS[user_id]).strftime("%d.%m.%Y %H:%M")
                await send_message(user_id, get_trial_greeting(msg["from"]["first_name"], expires))
                save_message(user_id, "user", text)
                reply = await deepseek_process(user_id, text)
                save_message(user_id, "assistant", reply)
                await send_message(user_id, reply)
            else:
                await send_message(
                    user_id,
                    "⛔ *Доступ запрещён.*\n\n"
                    "AURA — закрытый сервис для собственников бизнеса, топ-менеджеров и инвесторов.\n"
                    "Если вы считаете, что это ошибка — свяжитесь с администратором."
                )
        
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"❌ Ошибка webhook: {e}")
        return JSONResponse({"ok": False, "error": str(e)})

@app.get("/")
async def root():
    return {"status": "AURA VIP работает"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
