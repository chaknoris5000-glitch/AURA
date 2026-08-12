import re
from datetime import datetime, timedelta

def extract_numbers(text: str) -> list:
    return [int(x) for x in re.findall(r'\d+', text)]

def format_price(price: int) -> str:
    return f"{price:,} ₽".replace(",", " ")

def get_current_time():
    now = datetime.utcnow() + timedelta(hours=7)
    return f"Сейчас **{now.strftime('%H:%M')}**, {now.strftime('%d.%m.%Y')} 😊"

def extract_city_from_query(query: str) -> str:
    cities = ["москва", "спб", "питер", "сочи", "казань", "новосибирск", "екатеринбург", "нижний", "ростов", "белово"]
    for city in cities:
        if city in query.lower():
            return city
    return None
