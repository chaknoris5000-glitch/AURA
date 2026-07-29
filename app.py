def extract_facts(text, user_id):
    """Извлекает факты из сообщения — ПРОСТО И НАДЁЖНО"""
    text_lower = text.lower()
    
    # ===== ИМЯ =====
    # Проверяем фразы "меня зовут X", "зовут X", "я X"
    if "меня зовут" in text_lower:
        parts = text_lower.split("меня зовут")
        if len(parts) > 1:
            name = parts[1].strip().split()[0].capitalize()
            if name and len(name) > 1:
                save_fact(user_id, "name", name)
                save_user(user_id, name=name)
                print(f"✅ Запомнил имя: {name}")
                return
    
    if "зовут" in text_lower and "меня" not in text_lower:
        parts = text_lower.split("зовут")
        if len(parts) > 1:
            name = parts[1].strip().split()[0].capitalize()
            if name and len(name) > 1:
                save_fact(user_id, "name", name)
                save_user(user_id, name=name)
                print(f"✅ Запомнил имя: {name}")
                return
    
    # Если в начале сообщения "я X" (где X — имя)
    if text_lower.startswith("я "):
        name = text_lower[2:].strip().split()[0].capitalize()
        if name and len(name) > 1 and name not in ["Хочу", "Могу", "Буду", "Скажи"]:
            save_fact(user_id, "name", name)
            save_user(user_id, name=name)
            print(f"✅ Запомнил имя: {name}")
            return
    
    # ===== ГОРОД =====
    if "из " in text_lower or "в " in text_lower or "живу" in text_lower:
        # Ищем город после предлогов
        words = text_lower.split()
        for i, word in enumerate(words):
            if word in ["из", "в", "живу", "городе", "посёлке"] and i + 1 < len(words):
                city = words[i + 1].capitalize()
                # Убираем знаки препинания
                city = re.sub(r'[.,!?]', '', city)
                if city and len(city) > 2 and city not in ["Москве", "Спб", "России"]:
                    # Инской → Белово
                    if city.lower() == "инского" or city.lower() == "инской":
                        city = "Белово"
                    save_fact(user_id, "city", city)
                    save_user(user_id, city=city)
                    print(f"✅ Запомнил город: {city}")
                    return
    
    # ===== ЛЮБИТ/НЕ ЛЮБИТ =====
    if "нравится" in text_lower:
        save_fact(user_id, "likes", text)
    if "не нравится" in text_lower:
        save_fact(user_id, "dislikes", text)
    
    # ===== ТЕМЫ =====
    words = re.findall(r'\b[а-яА-ЯёЁ]{4,}\b', text_lower)
    stop_words = ["привет", "здравствуй", "спасибо", "пока", "да", "нет", "хорошо", "плохо", "просто", "так", "ещё", "очень", "можно", "надо", "будет"]
    for word in words:
        if word not in stop_words and len(word) > 3:
            save_topic(user_id, word)
