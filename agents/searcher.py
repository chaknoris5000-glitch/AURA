import base64
import xml.etree.ElementTree as ET
import logging
import httpx
import os

logger = logging.getLogger(__name__)

class Searcher:
    def __init__(self):
        self.api_key = os.getenv("YANDEX_API_KEY")
        self.folder_id = os.getenv("YANDEX_FOLDER_ID")
        self.url = "https://searchapi.api.cloud.yandex.net/v2/web/search"
    
    async def search(self, query: str, max_results: int = 10) -> list:
        if not self.api_key or not self.folder_id:
            logger.warning("⚠️ Нет ключа или папки Яндекса")
            return []
        
        logger.info(f"🔍 Поиск: {query}")
        
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "query": {
                "searchType": "SEARCH_TYPE_RU",
                "queryText": query,
            },
            "folderId": self.folder_id,
            "responseFormat": "FORMAT_XML",
            "maxpassages": 5,
            "lr": 213
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(self.url, json=payload, headers=headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    raw_data = data.get("rawData", "")
                    if not raw_data:
                        return []
                    
                    raw_data = raw_data.strip().strip('"').strip()
                    missing_padding = len(raw_data) % 4
                    if missing_padding:
                        raw_data += '=' * (4 - missing_padding)
                    xml_string = base64.b64decode(raw_data).decode('utf-8')
                    root = ET.fromstring(xml_string)
                    
                    results = []
                    for doc in root.findall(".//doc"):
                        title = doc.findtext("title", "Без названия")
                        link = doc.findtext("url", "#")
                        snippet = doc.findtext("snippet", "")
                        price = self._extract_price(snippet + " " + title)
                        results.append({
                            "title": title,
                            "url": link,
                            "snippet": snippet,
                            "price": price,
                            "position": len(results) + 1
                        })
                    logger.info(f"✅ Найдено {len(results)} результатов")
                    return results[:max_results]
                else:
                    logger.error(f"❌ Ошибка поиска: {response.status_code}")
                    return []
        except Exception as e:
            logger.error(f"❌ Ошибка запроса: {e}")
            return []
    
    def _extract_price(self, text: str) -> int:
        import re
        patterns = [
            r'(\d+[\s.,]*\d*)\s*₽',
            r'(\d+[\s.,]*\d*)\s*руб',
            r'(\d+[\s.,]*\d*)\s*р\.',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                price_str = match.group(1).replace(" ", "").replace(",", ".")
                try:
                    return int(float(price_str))
                except:
                    pass
        return 0
