import os
import re
import tempfile
from datetime import datetime
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
print("🚀 БОТ ЗАПУЩЕН. ФИНАЛЬНАЯ ВЕРСИЯ (ИСПРАВЛЕНО ИСПОЛЬЗОВАНИЕ ИМЕНИ)")

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

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====
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

# ===== ИЗВЛЕЧЕНИЕ ФАКТОВ =====

def extract_name(text):
    match = re.search(r"меня зовут\s*([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    match = re.search(r"зовут\s*([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        return match.group(1).capitalize()
    return None

def extract_city(text):
    text_lower = text.lower()
    
    city_markers = ["живу в", "я из", "из города", "в городе", "посёлок", "город", "живу в посёлке"]
    
    if not any(marker in text_lower for marker in city_markers):
        return None
    
    match = re.search(r"(?:из|в|живу в|живу)\s*([А-Яа-яёЁ\-]+)", text, re.I)
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

def understand_query(text, previous_topic=None):
    print(f"🧠 Понимаю запрос: '{text}'")
    
    if previous_topic and any(word in text.lower() for word in ["него", "это", "них", "ней", "его"]):
        stop_words_check = ["говорили", "сказал", "писал", "правильно", "вспомнил", "говорил", "напомни", "вспомни"]
        if previous_topic.lower() not in stop_words_check:
            print(f"🧠 Заметил местоимение, подставляю тему: '{previous_topic}'")
            return previous_topic
    
    text_lower = text.lower()
    topic_triggers = {
        "имя": ["имя", "имена", "зовут", "называют", "меня зовут"],
        "город": ["город", "живу", "жил", "родился", "откуда"],
        "загадка": ["загадк", "загадал"],
        "фильм": ["фильм", "кино", "смотрел", "сериал"],
    }
    
    for topic, keywords in topic_triggers.items():
        for keyword in keywords:
            if keyword in text_lower:
                print(f"🧠 Нашёл тему '{topic}' по ключевому слову '{keyword}'")
                return topic
    
    try:
        prompt = f"""
        Из вопроса пользователя извлеки ОДНО ключевое слово для поиска в истории.
        Ответь только этим словом в именительном падеже.
        
        Примеры:
        "Что мы говорили про Магнето?" → Магнето
        "Найди мне про росомаху" → росомаха
        "Какую загадку я загадал?" → загадка
        "Что мы говорили про угольный разрез?" → угольный разрез
        
        Вопрос: "{text}"
        
        Ключевое слово:
        """
        response = deepseek.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=30
        )
        result = response.choices[0].message.content.strip()
        if result:
            print(f"🧠 DeepSeek сказал искать: '{result}'")
            return result
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
                  "пожалуйста", "спасибо"]
    
    phrases = re.findall(r'[А-Яа-яёЁ]+\s+[А-Яа-яёЁ]+', text)
    for phrase in phrases:
        phrase_lower = phrase.lower()
        if any(word in phrase_lower for word in ["угольный", "разрез", "столица", "триатлон", "люди", "икс"]):
            print(f"🧠 Запасной (фраза): '{phrase}'")
            return phrase
    
    for word in words:
        if word[0].isupper() and word.lower() not in stop_words:
            print(f"🧠 Запасной (имя): '{word}'")
            return word
    
    for word in words:
        word_lower = word.lower()
        if word_lower not in stop_words and len(word) > 3 and word[-1] in 'аяоеиыьйнрл':
            print(f"🧠 Запасной (сущ): '{word}'")
            return word
    
    if words:
        print(f"🧠 Запасной (последнее): '{words[-1]}'")
        return words[-1]
    
    return text

def should_search_history(text):
    text_lower = text.lower()
    triggers = ["напомни", "что я говорил", "где я говорил", "найди в истории", "вспомни", 
                "что я писал", "загадк", "вопрос", "раньше", "прошлый", "прошлое", "помнишь", 
                "говорил", "писал", "рассказывал", "упоминал", "обсуждали", "было", "была", "ранее",
                "делал", "сказал", "спросил", "ответил", "написал", "вспомни", "помню",
                "выжимку", "первое", "всего общения"]
    return any(trigger in text_lower for trigger in triggers)

# ===== ФОРМАТИРОВАНИЕ В ЧЕЛОВЕЧЕСКИЙ ОТВЕТ =====

def format_results_to_human(results, original_query, search_word, user_name=None):
    if not results:
        return f"Что-то я подзабыл. Может, напомнишь, о чём именно шла речь? 😊"
    
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
    
    prompt = f"""
    Пользователь спросил: "{original_query}"
    
    Вот что нашлось в истории диалога:
    {history_text}
    
    {name_context}
    
    Задача: ответь пользователю КОРОТКО (максимум 500 символов).
    
    Правила:
    - Извлеки СУТЬ из истории
    - Не перечисляй все сообщения
    - Не пиши "в истории нашлось"
    - Говори как в обычном разговоре с другом
    - Используй эмодзи 😊🔥
    - Если ты НЕ УВЕРЕН, что это точно то, о чём спрашивают — скажи "Что-то я подзабыл, напомни, пожалуйста"
    - НЕ придумывай то, чего нет в истории
    - Если в истории нет точного ответа — честно скажи об этом
    - Имя пользователя используй ТОЛЬКО в начале разговора или для привлечения внимания, НЕ в каждом предложении
    
    Ответ:
    """
    
    try:
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
    
    return f"Что-то я подзабыл. Напомни, о чём мы говорили? 😊"

# ===== ГИБРИДНЫЙ ПОИСК =====

def hybrid_search(user_id, query, exclude_id=None, user_name=None):
    search_word = understand_query(query, last_search_topic)
    if not search_word or len(search_word) < 2:
        return None
    
    print(f"🔍 Бот ищет в Supabase: '{search_word}'")
    results = search_history(user_id, search_word, exclude_id)
    
    if results:
        print(f"✅ Бот нашёл: {len(results)} результатов")
        return format_results_to_human(results, query, search_word, user_name)
    
    print(f"🔍 Бот не нашёл, пробую DeepSeek...")
    all_history = get_all_history(user_id, limit=1000)
    
    if not all_history:
        return None
    
    user_messages = [h['content'] for h in all_history if h['role'] == 'user']
    if not user_messages:
        return None
    
    history_text = "\n".join(user_messages[-30:])
    
    prompt = f"""
    Найди в истории сообщения пользователя, которые относятся к теме "{query}".
    
    История пользователя:
    {history_text}
    
    Если нашёл - напиши краткую суть (2-3 предложения) как в разговоре с другом.
    Если ничего не найдено - напиши "ничего не найдено".
    """
    
    try:
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

        name = extract_name(text)
        if name:
            save_fact(user_id, "name", name)
        
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
        if "как меня зовут" in lower or "моё имя" in lower or "как зовут" in lower:
            if user_name:
                reply = f"Тебя зовут **{user_name}**."
            else:
                reply = "Ты ещё не представлялся! Можешь сказать: 'Меня зовут ...' и я запомню 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        if "где я живу" in lower or "мой город" in lower or "откуда я" in lower:
            if user_city:
                reply = f"Ты из **{user_city}**."
            else:
                reply = "Ты ещё не говорил, откуда ты. Можешь сказать: 'Я живу в ...' и я запомню 😊"
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== ПОИСК С ЧЕЛОВЕЧЕСКИМ ОТВЕТОМ =====
        if should_search_history(text):
            search_topic = understand_query(text, last_search_topic)
            
            if search_topic and search_topic.lower() not in ["говорили", "сказал", "писал", "правильно", "вспомнил", "давай"]:
                last_search_topic = search_topic
            
            if search_topic and len(search_topic) > 1:
                reply = hybrid_search(user_id, text, current_message_id, user_name)
                
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
        history = get_recent_history(user_id, limit=20)
        
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
• Пример хорошего использования: "Привет! Как дела?" → "Норм, у тебя?" → "Класс! А что нового?"
• Пример плохого использования: "Вадим, привет! Вадим, как дела? Вадим, а что нового?"

📌 ПРАВИЛА ЗНАКОМСТВА:
• Если пользователь ещё не представился — мягко спроси имя
• При знакомстве: "Приятно познакомиться! А ты из какого города?" 😊

🧠 О ПАМЯТИ:
• Используй последние 20 сообщений для контекста
• Всю историю помнишь, если тебя спросят

Ты общаешься с человеком, который хочет чувствовать себя комфортно. Будь таким. 😉"""

        if user_context:
            system_prompt += "\n\n" + "\n".join(user_context)

        messages = [{"role": "system", "content": system_prompt}]
        
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": text})

        try:
            print(f"🤖 DeepSeek: использую модель deepseek-v4-flash")
            response = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                temperature=0.8,
                max_tokens=600
            )
            reply = response.choices[0].message.content

            # Больше НЕ добавляем имя принудительно — DeepSeek сам решит, когда его использовать

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
