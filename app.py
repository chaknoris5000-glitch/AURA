import os
import re
import tempfile
import json
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
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

print("🚀 БОТ — ПРЯМОЙ МОСТ К DEEPSEEK С ПОИСКОМ В ИНТЕРНЕТЕ")

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
# 1. РАБОТА С БАЗОЙ (ТОЛЬКО ДЛЯ ХРАНЕНИЯ)
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
        print(f"💾 {role}: {content[:30]}...")
    except Exception as e:
        print(f"❌ Ошибка сохранения: {e}")

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
# 2. ПОИСК В ИНТЕРНЕТЕ (TAVILY)
# ============================================================

def search_web(query, max_results=5):
    if not TAVILY_API_KEY:
        print("❌ Tavily API ключ не найден")
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
# 3. DEEPSEEK — МОЗГ (РЕШАЕТ И ОТВЕЧАЕТ)
# ============================================================

def get_deepseek_response(user_id, text, user_name=None):
    """
    Бот отправляет запрос DeepSeek, DeepSeek решает, что делать.
    Если нужен поиск — бот ищет и отправляет результат обратно DeepSeek.
    """
    
    # Загружаем историю
    history = get_recent_history(user_id, limit=20)
    history_text = "\n".join([f"{h['role']}: {h['content']}" for h in history])

    # Системный промт с инструкцией использовать поиск
    system_prompt = f"""Ты — AURA, живой собеседник и друг.

📌 ТВОЙ СТИЛЬ ОБЩЕНИЯ:
• Отвечай коротко и по делу (2-3 предложения)
• Используй эмодзи 😊🔥😄
• Говори как живой человек в мессенджере
• Используй разговорные фразы: "ага", "окей", "класс", "бро"
• Не будь сухим или роботизированным - ты друг

📌 ПРАВИЛА ИСПОЛЬЗОВАНИЯ ИМЕНИ:
• Используй имя ТОЛЬКО в начале диалога или при обращении
• НЕ начинай КАЖДЫЙ ответ с имени

📌 О ПАМЯТИ:
• Используй последние 20 сообщений для контекста
• Всю историю помнишь, если тебя спросят

📌 О ПОИСКЕ В ИНТЕРНЕТЕ:
• Если тебе нужно найти информацию в интернете — используй Tavily
• Для этого напиши в ответе: [SEARCH: запрос]
• Бот выполнит поиск и вернёт результат
• После получения результата — сформулируй ответ пользователю
• Дай краткую выжимку с указанием источника

Пользователь: {user_name or "Незнакомец"}

История диалога:
{history_text}
"""
    
    # Формируем сообщения
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": text})

    # Первый запрос к DeepSeek
    print(f"🧠 Отправляю DeepSeek: {text[:50]}...")
    response = deepseek.chat.completions.create(
        model="deepseek-v4-flash",
        messages=messages,
        temperature=0.8,
        max_tokens=600
    )
    reply = response.choices[0].message.content

    # Проверяем, хочет ли DeepSeek выполнить поиск
    search_match = re.search(r'\[SEARCH:\s*(.+?)\]', reply)
    if search_match:
        search_query = search_match.group(1).strip()
        print(f"🔍 DeepSeek запросил поиск: '{search_query}'")
        
        # Бот ищет в интернете
        search_data = search_web(search_query)
        
        if search_data and search_data.get("results"):
            # Формируем результат для DeepSeek
            results_text = ""
            for i, r in enumerate(search_data.get("results", [])[:5], 1):
                title = r.get("title", "Без названия")
                content = r.get("content", "")[:400]
                url = r.get("url", "")
                results_text += f"\n{i}. {title}\n   {content}\n   Источник: {url}\n"
            
            # Отправляем результат DeepSeek
            search_prompt = f"""
Ты запросил поиск в интернете по запросу: "{search_query}"

Вот что нашлось:
{results_text}

Ответь пользователю КОРОТКО (2-3 предложения) как в обычном разговоре.
Используй найденную информацию.
Дай ссылку на источник.
Используй эмодзи 😊🔥
"""
            
            search_response = deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": search_prompt}],
                temperature=0.7,
                max_tokens=250
            )
            reply = search_response.choices[0].message.content
        else:
            reply = "Не удалось найти информацию в интернете по этому запросу. Попробуй переформулировать вопрос 😊"

    return reply

# ============================================================
# 4. ГОЛОС
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
# 5. ОТПРАВКА
# ============================================================

async def send_chat_action(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    try:
        requests.post(url, json={"chat_id": chat_id, "action": "typing"})
    except:
        pass

async def send_message(chat_id, text):
    await send_chat_action(chat_id)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")

# ============================================================
# 6. WEBHOOK
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

        # Голос
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

        # Сохраняем сообщение
        save_message(user_id, "user", text)

        # Получаем имя пользователя
        user_name = get_fact(user_id, "name")

        # Получаем ответ от DeepSeek
        reply = get_deepseek_response(user_id, text, user_name)

        # Сохраняем и отправляем ответ
        save_message(user_id, "assistant", reply)
        await send_message(user_id, reply)

        return JSONResponse({"ok": True})

    except Exception as e:
        print(f"❌ Ошибка: {e}")
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
