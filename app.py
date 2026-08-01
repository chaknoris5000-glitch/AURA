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

load_dotenv()

# ===== КЛЮЧИ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# ===== ПОДКЛЮЧЕНИЯ =====
print("🚀 БОТ ЗАПУЩЕН. ЧИСТОЕ ИЗВЛЕЧЕНИЕ ИМЕНИ (ФИНАЛ)")

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

# ===== РАБОТА С БАЗОЙ =====

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

# ===== ИЗВЛЕЧЕНИЕ ФАКТОВ (ЧИСТОЕ, БЕЗ МУСОРА) =====

def extract_name(text):
    """
    Извлекает имя ТОЛЬКО из явных фраз.
    НЕ сохраняет случайные слова!
    """
    # 1. "Меня зовут Вадим"
    match = re.search(r"меня зовут\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    
    # 2. "зовут Вадим" — ТОЛЬКО если есть слово после!
    match = re.search(r"зовут\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    
    # 3. "Моё имя Вадим"
    match = re.search(r"моё имя\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    
    # 4. "Я Вадим" — ТОЛЬКО если есть слово после!
    match = re.search(r"я\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        name = match.group(1).capitalize()
        if len(name) > 1 and name not in ["Я", "Ты", "Он", "Она", "Мы", "Вы", "Они"]:
            return name
    
    return None

def extract_city(text):
    """
    Извлекает город ТОЛЬКО с явными маркерами.
    НЕ сохраняет случайные слова!
    """
    text_lower = text.lower()
    
    city_markers = [
        "живу в", "я из", "из города", "в городе", "посёлок", "город", 
        "живу в посёлке", "я из города", "родился в", "живу в городе"
    ]
    
    if not any(marker in text_lower for marker in city_markers):
        return None
    
    match = re.search(r"(?:из|в|живу в|живу)\s+([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        city = match.group(1).capitalize()
        if city.lower() in ["инской", "инского", "инском"]:
            return "Инской"
        if city.lower() in ["белово", "белова"]:
            return "Белово"
        if len(city) < 3:
            return None
        return city
    return None

# ===== ПРОВЕРКА ГОРОДА =====

def verify_and_save_city(user_id, city_name, user_name=None):
    if not city_name or len(city_name) < 3:
        return None, "Город не указан или слишком короткое название. Напиши, где ты живёшь, я запомню! 😊"
    
    prompt = f"""
    Проверь, существует ли город или посёлок "{city_name}" в России или странах СНГ.
    
    Если существует:
    - Напиши "существует"
    - Напиши регион и 1-2 интересных факта (кратко)
    
    Если не существует или это явно не город (например "Ут", "Тоже", "Привет"):
    - Напиши "не существует"
    
    Ответ одним словом и коротким пояснением:
    """
    
    try:
        print(f"🤖 DeepSeek (flash): проверяю город '{city_name}'...")
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=100
        )
        result = response.choices[0].message.content.strip()
        print(f"🔍 Проверка города: {result}")
        
        if "существует" in result.lower() and "не существует" not in result.lower():
            save_fact(user_id, "city", city_name)
            facts = result.replace("существует", "").strip()
            if facts:
                return city_name, f"Круто! {city_name} — это {facts} 🔥 Запомнил!"
            else:
                return city_name, f"Запомнил! Ты из {city_name}. А что там интересного? 😊"
        else:
            return None, f"Хм, я не нашёл город {city_name} на карте. Ты уверен, что правильно назвал? Может, это посёлок или район? Напиши ещё раз 😊"
    except Exception as e:
        print(f"❌ Ошибка проверки города: {e}")
        save_fact(user_id, "city", city_name)
        return city_name, f"Запомнил! Ты из {city_name}. Расскажи потом о нём подробнее 😊"

# ===== ПОНИМАНИЕ ЗАПРОСА =====

def parse_time_filter(time_text):
    if not time_text:
        return None
    
    time_text = time_text.lower()
    now = datetime.now()
    
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
    
    garbage_topics = ["говорили", "сказал", "писал", "правильно", "вспомнил", "говорил", 
                      "напомни", "вспомни", "неправильно", "давай", "запомнил", "про"]
    
    if previous_topic and any(word in text.lower() for word in ["него", "это", "них", "ней", "его", "этом"]):
        if previous_topic.lower() not in garbage_topics:
            print(f"🧠 Заметил местоимение, подставляю тему: '{previous_topic}'")
            return previous_topic, None
        else:
            print(f"🧠 Заметил местоимение, но тема '{previous_topic}' мусорная — игнорирую")
    
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
    
    try:
        print(f"🤖 DeepSeek (flash): анализирую запрос...")
        prompt = f"""
        Пользователь спросил: "{text}"
        
        Извлеки:
        1. ТЕМУ (о чём спрашивают) — одно слово в именительном падеже
        2. ВРЕМЯ (когда) — если указано ("вчера", "месяц назад", "неделю назад", "летом")
        
        Примеры:
        "Что я говорил про работу месяц назад?" → ТЕМА: работа, ВРЕМЯ: месяц назад
        "Напомни про загадки" → ТЕМА: загадки, ВРЕМЯ: всё время
        "Что мы обсуждали вчера?" → ТЕМА: обсуждали, ВРЕМЯ: вчера
        "Где я живу?" → ТЕМА: город, ВРЕМЯ: всё время
        "Кто я?" → ТЕМА: имя, ВРЕМЯ: всё время
        
        Ответ строго в формате:
        ТЕМА: <слово>
        ВРЕМЯ: <время>
        """
        
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=50
        )
        result = response.choices[0].message.content.strip()
        print(f"🧠 DeepSeek понял: {result}")
        
        topic = None
        time_filter = None
        
        for line in result.split('\n'):
            if 'ТЕМА:' in line:
                topic = line.replace('ТЕМА:', '').strip()
            elif 'ВРЕМЯ:' in line:
                time_filter = line.replace('ВРЕМЯ:', '').strip()
                if time_filter.lower() in ["всё время", "всегда", "любое"]:
                    time_filter = None
        
        if topic:
            return topic, time_filter
            
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
    
    words = re.findall(r'[а-яА-ЯёЁa-zA-Z]{3,}', text)
    
    stop_words = ["какую", "первую", "тебе", "как", "что", "где", "когда", "почему", 
                  "зачем", "кто", "какой", "такой", "так", "вот", "да", "нет", "уже",
                  "ещё", "тоже", "только", "очень", "было", "была", "мою", "твою",
                  "свою", "нашу", "вашу", "эту", "ту", "этот", "тот", "все", "сам",
                  "говорил", "сказал", "писал", "рассказывал", "спросил", "ответил",
                  "загадал", "спросил", "напомни", "вспомни", "вчера", "сегодня", "завтра",
                  "говорили", "правильно", "вспомнил", "вспомнила", "помнишь", "помню",
                  "делал", "сделал", "ходил", "ездил", "смотрел", "читал", "видел",
                  "упоминал", "обсуждали", "вспомни", "напомни", "давай", "давайте",
                  "пожалуйста", "спасибо", "неправильно", "запомнил", "про"]
    
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
        "выжимку", "первое", "всего общения",
        "работаю", "работа", "кем работаю", "где работаю", "моя работа",
        "кто я", "назови моё имя", "ты помнишь моё имя",
        "откуда я", "в каком городе", "где я живу",
        "месяц", "неделю", "вчера", "сегодня", "год"
    ]
    return any(trigger in text_lower for trigger in triggers)

# ===== ФОРМАТИРОВАНИЕ ОТВЕТА =====

def format_results_to_human(results, original_query, search_word, time_filter, user_name=None):
    if not results:
        time_text = f" {time_filter}" if time_filter else ""
        return f"Что-то я подзабыл, что мы говорили про {search_word}{time_text}. Может, напомнишь? 😊"
    
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
    - Имя пользователя используй ТОЛЬКО в начале разговора или для привлечения внимания
    
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

# ===== ГИБРИДНЫЙ ПОИСК С ВРЕМЕНЕМ =====

def hybrid_search_with_time(user_id, query, exclude_id=None, user_name=None):
    topic, time_filter = understand_query_with_time(query, last_search_topic)
    
    if not topic or len(topic) < 2:
        return None
    
    print(f"🔍 Тема: '{topic}', Время: {time_filter if time_filter else 'всё время'}")
    
    results = search_history_with_time(user_id, topic, time_filter, exclude_id)
    
    if results:
        print(f"✅ Бот нашёл: {len(results)} результатов")
        return format_results_to_human(results, query, topic, time_filter, user_name)
    
    print(f"🔍 Бот не нашёл, пробую DeepSeek...")
    all_history = get_all_history(user_id, limit=1000)
    
    if not all_history:
        return None
    
    user_messages = [h['content'] for h in all_history if h['role'] == 'user']
    if not user_messages:
        return None
    
    history_text = "\n".join(user_messages[-30:])
    
    prompt = f"""
    Найди в истории сообщения пользователя, которые относятся к теме "{topic}".
    {f'Особенно за период: {time_filter}' if time_filter else ''}
    
    История пользователя:
    {history_text}
    
    Если нашёл - напиши краткую суть (2-3 предложения) как в разговоре с другом.
    Если ничего не найдено - напиши "ничего не найдено".
    """
    
    try:
        print(f"🤖 DeepSeek (flash): умный поиск в истории...")
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=200
        )
        result = response.choices[0].message.content.strip()
        if result and "ничего не найдено" not in result.lower():
            return result
    except Exception as e:
        print(f"❌ Ошибка DeepSeek: {e}")
    
    return None

# ===== РАСПОЗНАВАНИЕ ГОЛОСА =====

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

async def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=data)

# ===== ОСНОВНАЯ ЛОГИКА =====

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

        current_message_id = save_message(user_id, "user", text)

        # ===== ИЗВЛЕЧЕНИЕ ФАКТОВ (ТОЛЬКО ЯВНЫЕ ФРАЗЫ) =====
        name = extract_name(text)
        if name:
            save_fact(user_id, "name", name)
            print(f"✅ Сохранено имя: {name}")
        
        city = extract_city(text)
        if city:
            user_name = get_fact(user_id, "name")
            verified_city, response_text = verify_and_save_city(user_id, city, user_name)
            if verified_city:
                save_fact(user_id, "city", verified_city)
                await send_message(user_id, response_text)
                return JSONResponse({"ok": True})
            else:
                await send_message(user_id, response_text)
                return JSONResponse({"ok": True})

        lower = text.lower()
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")

        # ===== ПРЯМЫЕ ЗАПРОСЫ =====
        if "как меня зовут" in lower or "моё имя" in lower or "как зовут" in lower or "кто я" in lower or "напомни моё имя" in lower:
            if user_name:
                reply = f"Тебя зовут **{user_name}**."
            else:
                reply = "Ты ещё не представлялся! Можешь сказать: 'Меня зовут ...' и я запомню 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        if "где я живу" in lower or "мой город" in lower or "откуда я" in lower or "в каком городе" in lower:
            if user_city:
                reply = f"Ты из **{user_city}**."
            else:
                reply = "Ты ещё не говорил, откуда ты. Можешь сказать: 'Я живу в ...' и я запомню 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== ПОИСК С ЧЕЛОВЕЧЕСКИМ ОТВЕТОМ =====
        if should_search_history(text):
            topic, time_filter = understand_query_with_time(text, last_search_topic)
            
            garbage_topics = ["говорили", "сказал", "писал", "правильно", "вспомнил", "давай", "неправильно", "запомнил", "про"]
            if topic and topic.lower() not in garbage_topics:
                last_search_topic = topic
            
            if topic and len(topic) > 1 and topic.lower() not in garbage_topics:
                reply = hybrid_search_with_time(user_id, text, current_message_id, user_name)
                
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
        
        # ===== АВТОМАТИЧЕСКИЙ ПОИСК =====
        topic, time_filter = understand_query_with_time(text, last_search_topic)
        
        garbage_topics = ["говорили", "сказал", "писал", "правильно", "вспомнил", "давай", "неправильно", "запомнил", "про"]
        
        if topic and len(topic) > 1 and topic.lower() not in garbage_topics:
            topic_found_in_context = any(
                topic.lower() in msg.get('content', '').lower() 
                for msg in history
            )
            
            if not topic_found_in_context:
                print(f"🔍 АВТОПОИСК: темы '{topic}' нет в контексте, ищу в Supabase...")
                reply = hybrid_search_with_time(user_id, text, current_message_id, user_name)
                if reply:
                    print(f"✅ АВТОПОИСК: нашёл ответ в Supabase!")
                    save_message(user_id, "assistant", reply)
                    await send_message(user_id, reply)
                    return JSONResponse({"ok": True})
                else:
                    print(f"❌ АВТОПОИСК: ничего не нашёл в Supabase")
            else:
                print(f"✅ АВТОПОИСК: тема '{topic}' есть в контексте, поиск не требуется")

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
• Имя уместно: при приветствии, при прощании, для привлечения внимания
• Имя НЕ уместно: в середине каждого сообщения, в каждом ответе подряд

📌 ПРАВИЛА ЗНАКОМСТВА:
• Если пользователь ещё не представился — мягко спроси имя
• При знакомстве: "Приятно познакомиться! А ты из какого города?" 😊

🧠 О ПАМЯТИ:
• Используй последние 15 сообщений для контекста
• Если темы нет в контексте — автоматически ищи в Supabase
• Понимай время: "месяц назад", "вчера", "неделю назад"
• Всю историю помнишь, если тебя спросят

Ты общаешься с человеком, который хочет чувствовать себя комфортно. Будь таким. 😉"""

        if
