import logging

logger = logging.getLogger(__name__)

class Responder:
    def __init__(self, deepseek_client):
        self.client = deepseek_client
    
    async def generate_response(self, analysis: dict, query: str, user_name: str, user_city: str) -> str:
        if "error" in analysis:
            return "😅 Не нашёл ничего по этому запросу. Попробуй переформулировать."
        
        context = self._build_context(analysis, query)
        
        system_prompt = f"""Ты — AURA, супер-агент и аналитик. Пользователь: {user_name or "Незнакомец"}

ПРАВИЛА:
1. Отвечай живым, дружелюбным языком.
2. Структурируй ответ: основная информация → детали → ссылки.
3. Если запрос про цены — выдели самое выгодное предложение.
4. Если про новости — дай краткий обзор и ссылки.
5. Если про погоду — скажи температуру и рекомендации.
6. Используй эмодзи 😊🔥💰📰☀️.
7. Ответ должен быть завершённым и умещаться в 700 токенов.

КОНТЕКСТ:
{context}
"""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query}
        ]
        
        try:
            response = self.client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                temperature=0.85,
                max_tokens=700,
                timeout=30
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"❌ Ошибка генерации ответа: {e}")
            return self._generate_fallback_response(analysis, query)
    
    def _build_context(self, analysis: dict, query: str) -> str:
        context_lines = []
        query_type = analysis.get("type", "general")
        context_lines.append(f"Тип запроса: {query_type}")
        context_lines.append(f"Всего найдено: {analysis.get('total', 0)} результатов")
        
        prices = analysis.get("prices", [])
        if prices:
            cheapest = prices[0]
            context_lines.append(f"Самое дешёвое: {cheapest['title']} за {cheapest['price']} ₽")
            if len(prices) > 1:
                context_lines.append(f"Средняя цена: {sum(p['price'] for p in prices[:5]) // len(prices[:5])} ₽")
        
        dates = analysis.get("dates", [])
        if dates:
            context_lines.append(f"Найдены даты: {', '.join([d['date'] for d in dates[:3]])}")
        
        context_lines.append("\nПодробные результаты:")
        for i, res in enumerate(analysis.get("all_results", [])[:5], 1):
            price_text = f" ({res['price']} ₽)" if res.get("price", 0) > 0 else ""
            context_lines.append(f"{i}. {res['title']}{price_text} — {res.get('snippet', '')[:150]}...")
        
        return "\n".join(context_lines)
    
    def _generate_fallback_response(self, analysis: dict, query: str) -> str:
        response = "🔍 Нашёл! Вот лучшие результаты:\n\n"
        for i, res in enumerate(analysis.get("all_results", [])[:5], 1):
            price_text = f" — {res['price']} ₽" if res.get("price", 0) > 0 else ""
            response += f"{i}. [{res['title']}]({res['url']}){price_text}\n"
        return response
