import re
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class Analyzer:
    def __init__(self):
        pass
    
    def analyze(self, results: list, query: str) -> dict:
        if not results:
            return {"error": "Нет результатов для анализа"}
        
        prices = []
        for res in results:
            if res.get("price", 0) > 0:
                prices.append({
                    "url": res["url"],
                    "title": res["title"],
                    "price": res["price"],
                    "snippet": res.get("snippet", "")
                })
        
        prices.sort(key=lambda x: x["price"])
        query_type = self._detect_query_type(query)
        dates = self._extract_dates(results)
        
        return {
            "type": query_type,
            "total": len(results),
            "prices": prices,
            "cheapest": prices[0] if prices else None,
            "dates": dates,
            "all_results": results
        }
    
    def _detect_query_type(self, query: str) -> str:
        query_lower = query.lower()
        if any(w in query_lower for w in ["дешёв", "дешев", "самый низк", "скидк"]):
            return "cheapest"
        if any(w in query_lower for w in ["дорог", "самый высок"]):
            return "expensive"
        if any(w in query_lower for w in ["новост", "сегодня", "событи"]):
            return "news"
        if any(w in query_lower for w in ["погод", "температур"]):
            return "weather"
        if any(w in query_lower for w in ["билет", "авиа", "поезд"]):
            return "tickets"
        return "general"
    
    def _extract_dates(self, results: list) -> list:
        dates = []
        date_patterns = [
            r'(\d{2})\.(\d{2})\.(\d{4})',
            r'(\d{2})-(\d{2})-(\d{4})',
            r'(\d{4})-(\d{2})-(\d{2})'
        ]
        for res in results:
            text = res.get("snippet", "") + " " + res.get("title", "")
            for pattern in date_patterns:
                match = re.search(pattern, text)
                if match:
                    dates.append({
                        "date": match.group(0),
                        "context": text[:100]
                    })
        return dates
