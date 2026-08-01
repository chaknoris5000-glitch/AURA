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
import json

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
print("🚀 БОТ ЗАПУЩЕН. ЭТАЛОН #13 — ИСПРАВЛЕННАЯ ВЕРСИЯ")

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

last_search_topic = None

# ============================================================
# 1. РАБОТА С БАЗОЙ ДАННЫХ (SUPABASE)
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

def search_history_with_time(user_id, topic, time_filter=None, exclude_id=None):
    if not supabase:
        return []
    
    try:
        query = supabase.table("history")\
            .select("role, content, created_at, id")\
            .eq("user_id", user_id)\
            .ilike("content", f"%{topic}%")
        
        if time_filter:
            cutoff_date = parse_time_filter(time_filter)
            if cutoff_date:
                print(f"⏰ Фильтр по времени: старше {cutoff_date.strftime('%Y-%m-%d')}")
                query = query.lt("created_at", cutoff_date.isoformat())
        
        res = query.order("created_at", desc=True).limit(30).execute()
        results = res.data if res.data else []
        
        if exclude_id:
            results = [r for r in results if r.get('id') != exclude_id]
        
        return results[:10]
    except Exception as e:
        print(f"❌ Ошибка поиска с временем: {e}")
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
# 2. ОБРАБОТКА ЯВНЫХ КОМАНД (ИМЯ, ГОРОД)
# ============================================================

def handle_set_name(text, user_id):
    if text.lower().startswith("/setname"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            name = parts[1].strip()
            if len(name) > 1:
                save_fact(user_id, "name", name)
                return f"Запомнил! Тебя зовут **{name}** 😊"
        return "Напиши: /setname [имя]"
    
    match = re.search(r"меня зовут\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        name = match.group(1).capitalize()
        if len(name) > 1:
            save_fact(user_id, "name", name)
            return f"Запомнил! Тебя зовут **{name}** 😊"
    
    return None

def handle_set_city(text, user_id):
    if text.lower().startswith("/setcity"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            city = parts[1].strip()
            if len(city) > 2:
                save_fact(user_id, "city", city)
                return f"Запомнил! Ты из **{city}** 😊"
        return "Напиши: /setcity [город]"
    
    match = re.search(r"(?:живу в|я из)\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        city = match.group(1).capitalize()
        if len(city) > 2:
            save_fact(user_id, "city", city)
            return f"Запомнил! Ты из **{city}** 😊"
    
    return None

# ============================================================
# 3. ОПРЕДЕЛЕНИЕ МЕСТОПОЛОЖЕНИЯ ПО IP
# ============================================================

def get_location_by_ip(ip):
    """Определяет город по IP-адресу"""
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}", timeout=10)
        data = response.json()
        if data.get("status") == "success":
            return {
                "city": data.get("city"),
                "country": data.get("country"),
                "lat": data.get("lat"),
                "lon": data.get("lon"),
                "timezone": data.get("timezone")
            }
    except Exception as e:
        print(f"❌ Ошибка определения местоположения: {e}")
    return None

# ============================================================
# 4. СПЕЦИАЛЬНЫЕ ФУНКЦИИ (ВРЕМЯ, КУРС, ПОГОДА, НОВОСТИ)
# ============================================================

def get_current_time():
    """Возвращает текущее время и дату в МОСКОВСКОМ времени (MSK)"""
    # Используем московское время (UTC+3)
    now = datetime.utcnow() + timedelta(hours=3)
    return {
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%d.%m.%Y"),
        "full": now.strftime("%H:%M, %d.%m.%Y")
    }

def get_currency_rate(from_currency="USD", to_currency="RUB"):
    """Возвращает курс валют"""
    try:
        url = f"https://api.exchangerate-api.com/v4/latest/{from_currency}"
        response = requests.get(url, timeout=10)
        data = response.json()
        rate = data.get("rates", {}).get(to_currency)
        if rate:
            return {
                "from": from_currency,
                "to": to_currency,
                "rate": rate,
                "updated": datetime.utcnow().strftime("%H:%M")
            }
    except Exception as e:
        print(f"❌ Ошибка получения курса: {e}")
    return None

def get_weather(city):
    """Возвращает погоду в городе (без API ключа — парсит сайт)"""
    try:
        # Используем бесплатный парсинг погоды через wttr.in
        response = requests.get(f"https://wttr.in/{city}?format=%C+%t+%w+%h", timeout=10)
        if response.status_code == 200:
            data = response.text.strip()
            # Парсим ответ: "Cloudy +12°C 5 km/h 80%"
            parts = data.split()
            if len(parts) >= 4:
                description = " ".join(parts[:-3]) if len(parts) > 3 else parts[0]
                temp = parts[-3] if len(parts) >= 3 else "?"
                wind = parts[-2] if len(parts) >= 2 else "?"
                humidity = parts[-1] if len(parts) >= 1 else "?"
                return {
                    "city": city,
                    "description": description,
                    "temp": temp.replace("+", "").replace("-", ""),
                    "wind": wind,
                    "humidity": humidity
                }
    except Exception as e:
        print(f"❌ Ошибка получения погоды: {e}")
    return None

# ============================================================
# 5. ПОНИМАНИЕ ЗАПРОСА (DEEPSEEK)
# ============================================================

def parse_time_filter(time_text):
    if not time_text:
        return None
    
    time_text = time_text.lower()
    now = datetime.utcnow()
    
    if "месяц" in time_text or "30" in time_text or "31" in time_text:
        return now - timedelta(days=30)
    elif "неделя" in time_text or "7" in time_text:
        return now - timedelta(days=7)
    elif "вчера" in time_text or "1 день" in time_text:
        return now - timedelta(days=1)
    elif "сегодня" in time_text:
        return now
    elif "год" in time_text or "365" in time_text:
        return now - timedelta(days=365)
    elif "два дня" in time_text or "2 дня" in time_text:
        return now - timedelta(days=2)
    elif "три дня" in time_text or "3 дня" in time_text:
        return now - timedelta(days=3)
    
    return None

def understand_query_with_time(text, previous_topic=None):
    print(f"🧠 Понимаю запрос: '{text}'")
    
    # Сначала проверяем специальные темы
    text_lower = text.lower()
    topic_triggers = {
        "имя": ["имя", "имена", "зовут", "называют", "меня зовут", "кто я"],
        "город": ["город", "живу", "жил", "родился", "откуда", "в каком городе"],
        "загадка": ["загадк", "загадал"],
        "фильм": ["фильм", "кино", "смотрел", "сериал"],
        "работа": ["работаю", "работа", "кем работаю", "где работаю", "моя работа"]
    }
    
    for topic, keywords in topic_triggers.items():
        for keyword in keywords:
            if keyword in text_lower:
                print(f"🧠 Нашёл тему '{topic}' по ключевому слову '{keyword}'")
                return topic, None
    
    # Если специальные темы не найдены — пробуем DeepSeek
    try:
        print(f"🤖 DeepSeek (flash): анализирую СМЫСЛ запроса...")
        prompt = f"""
        Пользователь спросил: "{text}"
        
        Извлеки ОСНОВНУЮ СУТЬ вопроса (одно слово).
        Это должна быть ТЕМА, по которой нужно искать в истории.
        
        Примеры:
        "Что мы говорили про работу?" → работа
        "Как меня зовут?" → имя
        "Где я живу?" → город
        "Найди адрес" → адрес
        
        Ответь ТОЛЬКО темой (одно слово):
        """
        
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=20
        )
        result = response.choices[0].message.content.strip()
        
        if result and len(result) > 1:
            print(f"🧠 DeepSeek понял суть: '{result}'")
            return result, None
            
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
    
    # Запасной вариант
    words = re.findall(r'[а-яА-ЯёЁa-zA-Z]{3,}', text)
    stop_words = ["какую", "первую", "тебе", "как", "что", "где", "когда", "почему", 
                  "зачем", "кто", "какой", "такой", "так", "вот", "да", "нет", "уже",
                  "ещё", "тоже", "только", "очень", "было", "была", "мою", "твою",
                  "свою", "нашу", "вашу", "эту", "ту", "этот", "тот", "все", "сам"]
    
    for word in words:
        if word[0].isupper() and word.lower() not in stop_words:
            print(f"🧠 Запасной (имя): '{word}'")
            return word, None
    
    for word in words:
        word_lower = word.lower()
        if word_lower not in stop_words and len(word) > 3 and word[-1] in 'аяоеиыьйнрл':
            print(f"🧠 Запасной (сущ): '{word}'")
            return word, None
    
    if words:
        last_word = words[-1]
        if last_word.lower() not in stop_words:
            print(f"🧠 Запасной (последнее): '{last_word}'")
            return last_word, None
    
    return None, None

def should_search_history(text):
    text_lower = text.lower()
    triggers = [
        "напомни", "что я говорил", "где я говорил", "найди в истории", "вспомни", 
        "что я писал", "загадк", "вопрос", "раньше", "прошлый", "прошлое", "помнишь", 
        "говорил", "писал", "рассказывал", "упоминал", "обсуждали", "было", "была", "ранее",
        "делал", "сказал", "спросил", "ответил", "написал", "вспомни", "помню",
        "выжимку", "первое", "всего общения"
    ]
    return any(trigger in text_lower for trigger in triggers)

def should_search_web(text):
    text_lower = text.lower()
    triggers = [
        "найди", "найти", "поищи", "покажи", "где находится", "какой адрес",
        "кто такой", "что такое", "узнай", "расскажи о", "что это", "сколько стоит",
        "сайт", "ссылка", "как найти", "адрес", "контакты", "телефон"
    ]
    return any(trigger in text_lower for trigger in triggers)

# ============================================================
# 6. ПОИСК В ИНТЕРНЕТЕ (TAVILY)
# ============================================================

def search_web(query, max_results=5):
    """Ищет в интернете через Tavily API"""
    if not TAVILY_API_KEY:
        print("❌ Tavily API ключ не найден")
        return None
    
    print(f"🌐 Ищу в интернете: '{query}'")
    
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "max_results": max_results,
            "include_answer": True,
            "include_raw_content": False,
            "search_depth": "basic"  # basic быстрее и дешевле
        }
        
        response = requests.post(url, json=payload, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            answer = data.get("answer", "")
            
            print(f"✅ Найдено {len(results)} результатов")
            return {
                "answer": answer,
                "results": results,
                "query": query
            }
        else:
            print(f"❌ Ошибка Tavily: {response.status_code}")
            return None
            
    except Exception as e:
        print(f"❌ Ошибка поиска: {e}")
        return None

def format_web_results(search_data, original_query, user_name=None, user_city=None):
    """DeepSeek форматирует результаты поиска"""
    if not search_data or not search_data.get("results"):
        city_text = f" в {user_city}" if user_city else ""
        return f"Не нашёл ничего в интернете по запросу '{original_query}'{city_text}. Попробуй переформулировать вопрос 😊"
    
    answer = search_data.get("answer", "")
    results = search_data.get("results", [])
    
    # Формируем текст результатов для DeepSeek
    results_text = ""
    for i, r in enumerate(results[:5], 1):
        title = r.get("title", "Без названия")
        content = r.get("content", "")[:500]
        url = r.get("url", "")
        results_text += f"\n{i}. {title}\n   {content}\n   Источник: {url}\n"
    
    name_context = f"Ты общаешься с {user_name}." if user_name else ""
    city_context = f"Пользователь из {user_city}." if user_city else ""
    
    prompt = f"""
    Пользователь спросил: "{original_query}"
    
    Вот что нашлось в интернете:
    {results_text}
    
    {name_context}
    {city_context}
    
    Задача: ответь пользователю КОРОТКО (максимум 500 символов).
    
    Правила:
    - Извлеки САМОЕ ВАЖНОЕ из найденного
    - Если есть ответ от Tavily (answer) — используй его
    - Дай ссылку на источник
    - Не перечисляй все результаты подряд
    - Говори как в обычном разговоре с другом
    - Используй эмодзи 😊🔥
    
    Ответ:
    """
    
    try:
        print(f"🤖 DeepSeek (flash): форматирую поиск...")
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=300
        )
        result = response.choices[0].message.content.strip()
        if result:
            return result
    except Exception as e:
        print(f"❌ Ошибка форматирования: {e}")
    
    # Запасной вариант
    if answer:
        return f"Вот что я нашёл: {answer[:500]}"
    
    # Если есть результаты — показываем первый
    if results:
        first = results[0]
        return f"Вот что я нашёл: {first.get('title', '')}\n\n{first.get('content', '')[:300]}\n\nИсточник: {first.get('url', '')}"
    
    return f"Ничего не нашёл по запросу '{original_query}'. Попробуй переформулировать вопрос 😊"

# ============================================================
# 7. ГИБРИДНЫЙ ПОИСК (ИСТОРИЯ + ИНТЕРНЕТ)
# ============================================================

def format_results_to_human(results, original_query, search_word, time_filter, user_name=None, user_city=None):
    if not results:
        time_text = f" {time_filter}" if time_filter else ""
        city_text = f" в {user_city}" if user_city else ""
        return f"Что-то я подзабыл, что мы говорили про {search_word}{time_text}{city_text}. Может, напомнишь? 😊"
    
    messages = []
    for r in results[:5]:
        content = r['content']
        if content.startswith('📚') or content.startswith('assistant:'):
            continue
        messages.append(content)
    
    if not messages:
        return f"Вроде бы говорили про {search_word}, но детали вылетели из головы 😅 Напомни, о чём именно? 🔥"
    
    history_text = "\n".join(messages)
    name_context = f"Ты общаешься с {user_name}." if user_name else ""
    time_context = f" (за {time_filter})" if time_filter else ""
    
    prompt = f"""
    Пользователь спросил: "{original_query}"
    
    Вот что нашлось в истории диалога{time_context}:
    {history_text}
    
    {name_context}
    
    Задача: ответь пользователю КОРОТКО (максимум 500 символов).
    
    Правила:
    - Извлеки СУТЬ из истории
    - Не перечисляй все сообщения
    - Не пиши "в истории нашлось"
    - Говори как в обычном разговоре с другом
    - Используй эмодзи 😊🔥
    - Если ты НЕ УВЕРЕН, что это точно то, о чём спрашивают — скажи "Что-то я подзабыл"
    - НЕ придумывай то, чего нет в истории
    
    Ответ:
    """
    
    try:
        print(f"🤖 DeepSeek (flash): форматирую ответ...")
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200
        )
        result = response.choices[0].message.content.strip()
        if result:
            return result
    except Exception as e:
        print(f"❌ Ошибка форматирования: {e}")
    
    return f"Мы говорили про {search_word}. Что именно тебя интересует? 😊"

def hybrid_search_with_web(user_id, query, exclude_id=None, user_name=None, user_city=None):
    """
    Сначала ищет в истории, потом в интернете
    """
    # 1. Сначала ищем в истории
    topic, time_filter = understand_query_with_time(query, last_search_topic)
    
    if topic and len(topic) > 1:
        search_topic = topic
        if user_city:
            search_topic = f"{topic} {user_city}"
        
        results = search_history_with_time(user_id, search_topic, time_filter, exclude_id)
        
        if results:
            print(f"✅ Нашёл в истории: {len(results)} результатов")
            return format_results_to_human(results, query, topic, time_filter, user_name, user_city)
    
    # 2. Если в истории не нашлось — ищем в интернете
    print(f"🌐 В истории не нашлось, ищу в интернете...")
    
    # Формируем запрос для поиска
    search_query = query
    if user_city and any(word in query.lower() for word in ["найди", "покажи", "где", "адрес"]):
        search_query = f"{query} {user_city}"
        print(f"🌐 Добавляю город к поиску: '{user_city}'")
    
    # 3. Ищем через Tavily
    search_data = search_web(search_query)
    
    if search_data and search_data.get("results"):
        return format_web_results(search_data, query, user_name, user_city)
    
    # 4. Если Tavily не дал результатов
    return f"Не удалось найти информацию по запросу '{query}'. Попробуй переформулировать вопрос или поищи позже 😊"

# ============================================================
# 8. РАСПОЗНАВАНИЕ ГОЛОСА (GROQ WHISPER)
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
# 9. ОТПРАВКА СООБЩЕНИЙ С ИНДИКАТОРОМ "ПЕЧАТАЕТ..."
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
# 10. ОСНОВНАЯ ЛОГИКА (WEBHOOK)
# ============================================================

@app.post("/webhook")
async def webhook(request: Request):
    global last_search_topic
    
    try:
        body = await request.json()
        if "message" not in body:
            return JSONResponse({"ok": True})

        msg = body["message"]
        user_id = str(msg["from"]["id"])
        text = None
        current_message_id = None

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

        # ===== СОХРАНЕНИЕ СООБЩЕНИЯ =====
        current_message_id = save_message(user_id, "user", text)

        # ===== ОБРАБОТКА КОМАНД (ИМЯ, ГОРОД) =====
        name_reply = handle_set_name(text, user_id)
        if name_reply:
            save_message(user_id, "assistant", name_reply)
            await send_message(user_id, name_reply)
            return JSONResponse({"ok": True})

        city_reply = handle_set_city(text, user_id)
        if city_reply:
            save_message(user_id, "assistant", city_reply)
            await send_message(user_id, city_reply)
            return JSONResponse({"ok": True})

        lower = text.lower()
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")

        # ===== ПРЯМЫЕ ЗАПРОСЫ (ИЗ user_memory) =====
        if "как меня зовут" in lower or "моё имя" in lower or "как зовут" in lower or "кто я" in lower or "напомни моё имя" in lower:
            if user_name:
                reply = f"Тебя зовут **{user_name}**."
            else:
                reply = "Ты ещё не представлялся! Скажи: 'Меня зовут ...' или /setname [имя] 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        if "где я живу" in lower or "мой город" in lower or "откуда я" in lower or "в каком городе" in lower:
            if user_city:
                reply = f"Ты из **{user_city}**."
            else:
                reply = "Ты ещё не говорил, откуда ты. Скажи: 'Я живу в ...' или /setcity [город] 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== ОПРЕДЕЛЕНИЕ ГОРОДА ПО IP =====
        if not user_city:
            try:
                ip = request.client.host
                if ip and ip != "127.0.0.1":
                    location = get_location_by_ip(ip)
                    if location and location.get("city"):
                        user_city = location.get("city")
                        save_fact(user_id, "city", user_city)
                        print(f"📍 Определён город по IP: {user_city}")
            except Exception as e:
                print(f"❌ Ошибка определения IP: {e}")

        # ===== ВРЕМЯ =====
        if any(word in lower for word in ["время", "сколько времени", "который час"]):
            time_data = get_current_time()
            reply = f"Сейчас **{time_data['time']}**, {time_data['date']} 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== КУРС ВАЛЮТ =====
        if "курс" in lower or "доллар" in lower or "евро" in lower:
            currency = "USD" if "доллар" in lower else "EUR" if "евро" in lower else "USD"
            rate_data = get_currency_rate(currency, "RUB")
            if rate_data:
                reply = f"Курс **{rate_data['from']}** к **{rate_data['to']}** — **{rate_data['rate']:.2f}** (на {rate_data['updated']}) 😊"
            else:
                reply = "Не удалось получить курс валют. Попробуй позже 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== ПОГОДА =====
        if "погода" in lower or "температура" in lower:
            city = user_city
            # Извлекаем город из текста, если указан
            match = re.search(r"в\s+([А-Яа-яёЁ\-]+)", text)
            if match:
                city = match.group(1).capitalize()
            
            if city:
                weather = get_weather(city)
                if weather:
                    reply = f"Погода в **{weather['city']}**: {weather['description']}, температура **{weather['temp']}°C**, ветер {weather['wind']} м/с 😊"
                else:
                    reply = f"Не удалось получить погоду для города {city}. Проверь название 😊"
            else:
                reply = "Скажи, в каком городе узнать погоду. Например: 'Погода в Белово' 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== ПОИСК В ИНТЕРНЕТЕ (ЕСЛИ НУЖНО) =====
        if should_search_web(text):
            print(f"🌐 Пользователь хочет поискать в интернете...")
            reply = hybrid_search_with_web(user_id, text, current_message_id, user_name, user_city)
            if reply:
                save_message(user_id, "assistant", reply)
                await send_message(user_id, reply)
                return JSONResponse({"ok": True})

        # ===== ПОИСК В ИСТОРИИ (ЕСЛИ НУЖНО) =====
        if should_search_history(text):
            topic, time_filter = understand_query_with_time(text, last_search_topic)
            
            if topic and len(topic) > 1:
                reply = hybrid_search_with_web(user_id, text, current_message_id, user_name, user_city)
                
                if reply:
                    save_message(user_id, "assistant", reply)
                    await send_message(user_id, reply)
                    return JSONResponse({"ok": True})
                else:
                    reply = "Что-то я подзабыл. Может, напомнишь, о чём именно шла речь? 😊"
                    save_message(user_id, "assistant", reply)
                    await send_message(user_id, reply)
                    return JSONResponse({"ok": True})

        # ===== ОСНОВНОЙ ДИАЛОГ =====
        history = get_recent_history(user_id, limit=15)

        # ===== АВТОМАТИЧЕСКИЙ ПОИСК (ЕСЛИ ТЕМЫ НЕТ В КОНТЕКСТЕ) =====
        topic, time_filter = understand_query_with_time(text, last_search_topic)
        
        if topic and len(topic) > 1:
            topic_found_in_context = any(
                topic.lower() in msg.get('content', '').lower() 
                for msg in history
            )
            
            if not topic_found_in_context:
                print(f"🔍 АВТОПОИСК: темы '{topic}' нет в контексте, ищу в Supabase...")
                reply = hybrid_search_with_web(user_id, text, current_message_id, user_name, user_city)
                if reply:
                    print(f"✅ АВТОПОИСК: нашёл ответ!")
                    save_message(user_id, "assistant", reply)
                    await send_message(user_id, reply)
                    return JSONResponse({"ok": True})
                else:
                    print(f"❌ АВТОПОИСК: ничего не нашёл")
            else:
                print(f"✅ АВТОПОИСК: тема '{topic}' есть в контексте, поиск не требуется")

        # ===== ОСНОВНОЙ ДИАЛОГ (СИСТЕМНЫЙ ПРОМТ) =====
        user_context = []
        if user_name:
            user_context.append(f"Тебя зовут {user_name}.")
        else:
            user_context.append("Ты ещё не знаешь имени пользователя. Если он не представился — мягко спроси, как его зовут. Если представился — запомни.")
        
        if user_city:
            user_context.append(f"Он из {user_city}.")
        else:
            user_context.append("Ты не знаешь, откуда пользователь. Если он не сказал — можно спросить.")

        system_prompt = """Ты — AURA, живой собеседник и друг.

📌 ТВОЙ СТИЛЬ ОБЩЕНИЯ:
• Отвечай коротко и по делу (2-3 предложения)
• Используй эмодзи 😊🔥😄
• Говори как живой человек в мессенджере
• Используй разговорные фразы: "ага", "окей", "класс", "бро"
• Не будь сухим или роботизированным - ты друг

📌 ПРАВИЛА ИСПОЛЬЗОВАНИЯ ИМЕНИ:
• Используй имя ТОЛЬКО в начале диалога или при обращении
• НЕ начинай КАЖДЫЙ ответ с имени

📌 ПРАВИЛА ЗНАКОМСТВА:
• Если пользователь ещё не представился — мягко спроси имя
• При знакомстве: "Приятно познакомиться! А ты из какого города?" 😊

🧠 О ПАМЯТИ:
• Используй последние 15 сообщений для контекста
• Если темы нет в контексте — автоматически ищи в Supabase
• Понимай время: "месяц назад", "вчера", "неделю назад"
• Если знаешь город пользователя — используй его при поиске
• Всю историю помнишь, если тебя спросят

🌐 О ПОИСКЕ В ИНТЕРНЕТЕ:
• Если не нашёл в истории — ищи в интернете через Tavily
• Дай краткую выжимку с указанием источника

Ты общаешься с человеком, который хочет чувствовать себя комфортно. Будь таким. 😉"""

        if user_context:
            system_prompt += "\n\n" + "\n".join(user_context)

        messages = [{"role": "system", "content": system_prompt}]
        
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text})

        try:
            print(f"🤖 DeepSeek (flash): основной диалог, {len(messages)} сообщений")
            response = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                temperature=0.8,
                max_tokens=600
            )
            reply = response.choices[0].message.content

            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)

        except Exception as e:
            print(f"❌ Ошибка DeepSeek: {e}")
            await send_message(user_id, "😅 Что-то пошло не так. Попробуй ещё раз.")

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
