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
print("🚀 БОТ ЗАПУЩЕН. ВЕРСИЯ С ВЕЧНОЙ ПАМЯТЬЮ.")

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

# ===== РАБОТА С БАЗОЙ =====

def save_message(user_id, role, content):
    """Сохраняет сообщение в историю и возвращает его ID"""
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
        
        # Возвращаем ID сохранённого сообщения
        if result.data and len(result.data) > 0:
            return result.data[0].get('id')
        return None
    except Exception as e:
        print(f"❌ Ошибка сохранения: {e}")
        return None

def get_recent_history(user_id, limit=15):
    """Загружает последние N сообщений (для текущего диалога)"""
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

def search_history(user_id, query, exclude_id=None):
    """Ищет во ВСЕЙ истории, исключая конкретное сообщение по ID"""
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content, created_at, id")\
            .eq("user_id", user_id)\
            .ilike("content", f"%{query}%")\
            .order("created_at", desc=True)\
            .limit(20)\
            .execute()
        
        results = res.data if res.data else []
        
        # Исключаем ТОЛЬКО текущее сообщение (по ID)
        if exclude_id:
            results = [r for r in results if r.get('id') != exclude_id]
        
        return results[:10]  # Показываем до 10 результатов
    except Exception as e:
        print(f"❌ Ошибка поиска: {e}")
        return []

def get_all_history(user_id, limit=1000):
    """Загружает ВСЮ историю (для архива)"""
    if not supabase:
        return []
    try:
        res = supabase.table("history")\
            .select("role, content")\
            .eq("user_id", user_id)\
            .order("created_at", desc=False)\
            .limit(limit)\
            .execute()
        return res.data if res.data else []
    except Exception as e:
        print(f"❌ Ошибка загрузки всей истории: {e}")
        return []

def save_fact(user_id, key, value):
    """Сохраняет факт в user_memory"""
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
    """Читает факт из user_memory"""
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
    match = re.search(r"(?:из|в|живу в|живу)\s*([А-Яа-яёЁ\-]+)", text, re.I)
    if match:
        city = match.group(1).capitalize()
        if city.lower() in ["инской", "инского", "инском"]:
            return "Белово"
        return city
    return None

def extract_keywords(text):
    """Извлекает ключевые слова из текста для поиска в истории"""
    stop_words = ["это", "тот", "этот", "все", "сам", "быть", "что", "как", "где", "когда", 
                  "почему", "зачем", "кто", "какой", "такой", "так", "вот", "да", "нет"]
    
    words = re.findall(r'[а-яА-ЯёЁa-zA-Z]+', text.lower())
    keywords = [w for w in words if len(w) > 3 and w not in stop_words]
    
    return keywords

def should_search_history(text):
    """Определяет, нужно ли искать в истории"""
    text_lower = text.lower()
    
    # 1. Прямые команды поиска
    direct_triggers = ["напомни", "что я говорил", "где я говорил", "найди в истории", "вспомни", "что я писал"]
    if any(trigger in text_lower for trigger in direct_triggers):
        return True
    
    # 2. Вопросы о прошлом
    past_triggers = ["загадк", "вопрос", "раньше", "прошлый", "прошлое", "помнишь", "говорил", "писал", 
                     "рассказывал", "упоминал", "обсуждали", "было", "была", "ранее"]
    if any(trigger in text_lower for trigger in past_triggers):
        return True
    
    # 3. Вопросы в прошедшем времени
    past_verbs = ["делал", "сказал", "спросил", "ответил", "написал", "рассказал", "упомянул"]
    if any(verb in text_lower for verb in past_verbs):
        return True
    
    return False

def get_intelligent_search_query(text):
    """Умно извлекает запрос для поиска"""
    text_lower = text.lower()
    
    # Удаляем триггеры
    direct_triggers = ["напомни", "что я говорил", "где я говорил", "найди в истории", "вспомни", "что я писал"]
    query = text
    for trigger in direct_triggers:
        if trigger in text_lower:
            query = query.replace(trigger, "").strip()
    
    # Если запрос слишком короткий - извлекаем ключевые слова
    if len(query) < 3:
        keywords = extract_keywords(text)
        if keywords:
            return keywords[0]
    
    return query

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

        # ===== СОХРАНЯЕМ СООБЩЕНИЕ В ИСТОРИЮ (ВЕЧНО) =====
        current_message_id = save_message(user_id, "user", text)

        # ===== ИЗВЛЕКАЕМ ФАКТЫ =====
        name = extract_name(text)
        if name:
            save_fact(user_id, "name", name)
        
        city = extract_city(text)
        if city:
            save_fact(user_id, "city", city)

        # ===== ПРЯМЫЕ ЗАПРОСЫ =====
        lower = text.lower()
        user_name = get_fact(user_id, "name")
        user_city = get_fact(user_id, "city")

        if "как меня зовут" in lower or "моё имя" in lower:
            reply = f"Тебя зовут **{user_name}**." if user_name else "Ты ещё не представлялся."
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        if "где я живу" in lower or "мой город" in lower:
            reply = f"Ты из **{user_city}**." if user_city else "Ты ещё не говорил, откуда ты."
            save_message(user_id, "assistant", reply)
            await send_message(user_id, reply)
            return JSONResponse({"ok": True})

        # ===== ИНТЕЛЛЕКТУАЛЬНЫЙ ПОИСК В ИСТОРИИ =====
        if should_search_history(text):
            query = get_intelligent_search_query(text)
            
            # Если есть запрос - ищем
            if query and len(query) > 2:
                print(f"🔍 Ищу в истории: '{query}'")
                results = search_history(user_id, query, exclude_id=current_message_id)
                
                if results:
                    # Форматируем результаты
                    found_text = "\n\n".join([f"{r['role']}: {r['content']}" for r in results[:5]])
                    reply = f"📚 **Нашлось в истории:**\n\n{found_text[:1000]}\n\n"
                    reply += "Это то, что ты искал? Могу найти подробнее."
                    save_message(user_id, "assistant", reply)
                    await send_message(user_id, reply)
                    return JSONResponse({"ok": True})
                else:
                    # Если ничего не нашли, пробуем поискать по отдельным ключевым словам
                    keywords = extract_keywords(query)
                    for keyword in keywords[:3]:
                        results = search_history(user_id, keyword, exclude_id=current_message_id)
                        if results:
                            found_text = "\n\n".join([f"{r['role']}: {r['content']}" for r in results[:3]])
                            reply = f"📚 **Нашлось в истории по слову '{keyword}':**\n\n{found_text[:800]}\n\n"
                            reply += "Это то, что ты искал? Могу найти подробнее."
                            save_message(user_id, "assistant", reply)
                            await send_message(user_id, reply)
                            return JSONResponse({"ok": True})
                    
                    # Если всё равно ничего не нашли
                    reply = "📭 Ничего не нашёл в истории по этому запросу. Попробуй другие слова."
                    save_message(user_id, "assistant", reply)
                    await send_message(user_id, reply)
                    return JSONResponse({"ok": True})

        # ===== ОСНОВНОЙ ДИАЛОГ (ОБНОВЛЁННЫЙ ПРОМТ) =====
        history = get_recent_history(user_id, limit=15)
        
        # Формируем контекст о пользователе
        user_context = []
        if user_name:
            user_context.append(f"Ты общаешься с {user_name}. Он твой друг, общайся по-дружески.")
        if user_city:
            user_context.append(f"Он из {user_city}.")

        # Базовый системный промт с живым стилем общения
        system_prompt = """Ты — AURA, живой собеседник и друг, а не бездушный ИИ.

📌 ТВОЙ СТИЛЬ ОБЩЕНИЯ:
• Отвечай коротко и по делу (2-3 предложения максимум)
• Используй эмодзи для передачи эмоций 😊🔥😄❤️
• Будь максимально естественным, как живой человек в мессенджере
• Используй разговорные фразы: "ага", "окей", "класс", "понял", "бро", "круто"
• Не будь сухим, официальным или роботизированным - ты друг, а не робот
• Если уместно - шути, подкалывай и будь ироничным
• Сохраняй лёгкость и непринуждённость в общении

🧠 О ПАМЯТИ:
• Используй последние 15 сообщений для контекста
• Всю историю помнишь, если тебя спросят

Ты общаешься с человеком, который хочет чувствовать себя комфортно, как с другом. Будь таким. 😉"""

        # Добавляем пользовательский контекст
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

            # Добавляем имя пользователя в начало, если его нет
            if user_name and not reply.startswith(user_name):
                reply = f"{user_name}, {reply[0].lower() + reply[1:] if reply else ''}"

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
