SERPAPI_KEY = os.getenv("SERPAPI_KEY")

def search_web(query):
    if not SERPAPI_KEY:
        return "⚠️ Ключ SerpAPI не настроен"
    try:
        params = {"q": query, "hl": "ru", "gl": "ru", "api_key": SERPAPI_KEY, "num": 3}
        resp = requests.get("https://serpapi.com/search", params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("organic_results", [])
            if results:
                reply = ""
                for r in results[:3]:
                    reply += f"**{r.get('title', '')}**\n{r.get('snippet', '')}\n🔗 {r.get('link', '')}\n\n"
                return reply.strip()
        return None
    except:
        return None

def search_weather(city):
    if not SERPAPI_KEY:
        return None
    try:
        params = {"q": f"погода {city}", "hl": "ru", "gl": "ru", "api_key": SERPAPI_KEY}
        resp = requests.get("https://serpapi.com/search", params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            weather = data.get("weather_results", {})
            if weather:
                return f"🌡️ {weather.get('temperature', '')}, {weather.get('description', '')}"
        return None
    except:
        return None
