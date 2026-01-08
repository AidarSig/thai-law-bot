import os
import re
import asyncio
import requests
from typing import Optional, Set, Tuple
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI

# ==========================================
# 1. КОНФИГУРАЦИЯ И НАСТРОЙКИ
# ==========================================

# Получение ключей из переменных окружения (Render Environment)
api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID")
tg_token = os.environ.get("TELEGRAM_TOKEN")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

# Инициализация OpenAI клиента
client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Тайм-аут ожидания ответа от AI (110 секунд), чтобы пережить "холодный старт" Render
ATTEMPT_TIMEOUT = 110 

# Локальная база данных (в памяти) для предотвращения дублей уведомлений по одному треду
leads_db: Set[str] = set()

# Настройка CORS (разрешаем запросы с любых доменов, включая Tilda)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Модель данных входящего запроса (валидация Pydantic)
class UserRequest(BaseModel):
    message: str
    # Optional нужен, чтобы Tilda могла присылать null в первом запросе без ошибки 422
    thread_id: Optional[str] = None

# ==========================================
# 2. СЛОВАРИ КАТЕГОРИЙ (SEGMENTATION)
# ==========================================

CATEGORIES = {
    "🔴 КРИМИНАЛ/SOS": [
        "полици", "тюрьм", "арест", "задержа", "участок", "суд", "депорт", 
        "нарко", "драка", "авари", "дтп", "police", "jail", "arrest", "sos", 
        "prison", "urgent", "help", "срочно"
    ],
    "🛂 БИЗНЕС/ВИЗЫ": [
        "виза", "визу", "visa", "компани", "бизнес", "счет", "банк", "work permit", 
        "ворк пермит", "открыть", "bank", "company", "лицензи", "license", 
        "weed", "каннабис", "dispensary", "конопл"
    ],
    "🏡 НЕДВИЖИМОСТЬ": [
        "вилл", "квартир", "земл", "участ", "недвиж", "condo", "villa", "land", 
        "buy", "rent", "аренд", "покуп", "chanote", "чанот", "estate"
    ],
    "💍 ГРАЖДАНСКОЕ": [
        "развод", "жен", "муж", "ребен", "дите", "брак", "divorce", "marriage", 
        "wife", "husband", "child", "долг", "займ", "наследств", "family"
    ],
    "⚠️ НЕДОВЕРИЕ": [
        "развод", "скам", "настоящий", "человек", "робот", "бот", "гаранти", 
        "офис", "живой", "scam", "real", "human", "отзывы", "review"
    ]
}

# Ключевые слова для определения запроса контактов
CONTACT_KEYWORDS = [
    "контакт", "телефон", "номер", "позвонить", "связ", "адрес", "почта", 
    "contact", "phone", "number", "call", "address", "whatsapp", "telegram", 
    "line", "email"
]

# ==========================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def clean_text(text: str) -> str:
    """Очищает текст от технических сносок OpenAI и Markdown."""
    if not text: return ""
    # Удаление сносок вида 【4:0†source】
    text = re.sub(r'【.*?】', '', text)
    # Удаление жирного шрифта и заголовков Markdown
    text = text.replace("###", "").replace("**", "")
    # Удаление лишних пробелов
    text = re.sub(r' +', ' ', text)
    return text.strip()

async def get_history_data(thread_id: str) -> Tuple[str, int]:
    """
    Скачивает историю переписки и форматирует её для Telegram.
    Возвращает: (текст_истории, количество_сообщений_клиента).
    """
    try:
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=30)
        # OpenAI отдает от новых к старым, разворачиваем
        history_list = list(reversed(messages.data))
        
        formatted_text = ""
        user_msg_count = 0
        
        for msg in history_list:
            role = msg.role
            # Безопасное получение текста
            if hasattr(msg.content[0], 'text'):
                content = clean_text(msg.content[0].text.value)
            else:
                content = "[Изображение или файл]"

            if role == "user":
                user_msg_count += 1
                formatted_text += f"👤 Клиент: {content}\n\n"
            elif role == "assistant":
                formatted_text += f"🤖 Юрист: {content}\n\n"
                
        return formatted_text, user_msg_count
    except Exception as e:
        print(f"History Fetch Error: {e}")
        return "(История недоступна)", 0

def detect_category(text: str) -> str:
    """Определяет категорию обращения по ключевым словам."""
    text_lower = text.lower()
    
    # Приоритетная проверка на SOS
    for kw in CATEGORIES["🔴 КРИМИНАЛ/SOS"]:
        if kw in text_lower: return "🔴 КРИМИНАЛ/SOS"
        
    # Проверка остальных категорий
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in text_lower:
                return cat
    
    return "" # Категория не найдена

async def send_to_tg(text: str):
    """Отправляет сообщение в Telegram (в отдельном потоке)."""
    if not tg_token or not tg_chat_id:
        return

    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = {
        "chat_id": tg_chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    
    try:
        loop = asyncio.get_event_loop()
        # Используем run_in_executor, чтобы requests не блокировал асинхронный цикл
        await loop.run_in_executor(None, lambda: requests.post(url, json=payload))
    except Exception as e:
        print(f"Telegram Send Error: {e}")

# ==========================================
# 4. ЛОГИКА УВЕДОМЛЕНИЙ (CRM)
# ==========================================

async def handle_telegram_notification(text: str, thread_id: str):
    """
    Анализирует сообщение и решает, отправлять ли уведомление в Telegram.
    Запускается как фоновая задача.
    """
    if not tg_token or not tg_chat_id: return

    # Очистка текста для поиска номера
    clean_msg = re.sub(r'[\s\-]', '', text)
    # Поиск: 7+ цифр подряд ИЛИ наличие @ (ник в телеграм)
    has_contact = re.search(r'\d{7,}', clean_msg) or ("@" in text and len(text) < 50)
    
    category = detect_category(text)

    # --- СЦЕНАРИЙ 1: ПОЛУЧЕН КОНТАКТ (ГОРЯЧИЙ ЛИД) ---
    if has_contact:
        header = f"🔥 <b>НОВЫЙ ЛИД!</b> {category}"
        
        # Если этот тред еще не присылал контакты
        if thread_id not in leads_db:
            leads_db.add(thread_id)
            history_text, _ = await get_history_data(thread_id)
            msg = (f"{header}\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"{history_text}"
                   f"➖➖➖➖➖➖➖\n"
                   f"🆔 <code>{thread_id}</code>")
            await send_to_tg(msg)
        # Если контакт уже был, но клиент пишет дополнение
        else:
            msg = (f"📝 <b>ДОП. ИНФО</b> {category}\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"👤 Клиент: {text}\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"🔗 <code>{thread_id}</code>")
            await send_to_tg(msg)
        return

    # --- СЦЕНАРИЙ 2: НЕТ КОНТАКТА -> АНАЛИЗ ПОВЕДЕНИЯ ---

    # A. Криминал / SOS (Шлем сразу, даже без номера)
    if "КРИМИНАЛ" in category and thread_id not in leads_db:
        leads_db.add(thread_id)
        history_text, _ = await get_history_data(thread_id)
        msg = (f"{category}\n"
               f"<i>🚨 ТРЕВОГА (Без контакта)!</i>\n"
               f"➖➖➖➖➖➖➖\n"
               f"{history_text}"
               f"➖➖➖➖➖➖➖\n"
               f"🆔 <code>{thread_id}</code>")
        await send_to_tg(msg)
        return

    # B. Активный запрос контактов (Интерес)
    is_asking_contacts = any(word in text.lower() for word in CONTACT_KEYWORDS)
    
    if is_asking_contacts and thread_id not in leads_db:
        # Проверяем, не спам ли это (диалог должен быть > 2 сообщений)
        history_text, user_count = await get_history_data(thread_id)
        
        if user_count > 2:
            leads_db.add(thread_id)
            msg = (f"👀 <b>ЗАПРОС КОНТАКТОВ</b> {category}\n"
                   f"<i>Клиент просит связь, но номер не дал.</i>\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"{history_text}"
                   f"➖➖➖➖➖➖➖\n"
                   f"🆔 <code>{thread_id}</code>")
            await send_to_tg(msg)

# ==========================================
# 5. ЛОГИКА OPENAI ASSISTANT
# ==========================================

async def run_assistant_with_timeout(thread_id: str, assistant_id: str, timeout: int) -> bool:
    """
    Запускает выполнение (Run) ассистента и ждет завершения.
    Возвращает True, если успешно, False, если ошибка или таймаут.
    """
    try:
        run = await client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        start_time = asyncio.get_event_loop().time()
        
        # Цикл опроса (Polling)
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                # Таймаут: пытаемся отменить ран, чтобы не висел
                try: 
                    await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                except: 
                    pass
                return False 

            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            if run_status.status == 'completed':
                return True
            elif run_status.status in ['failed', 'cancelled', 'expired']:
                print(f"Run Failed Status: {run_status.status}")
                return False
            
            # Ждем 1 секунду перед следующей проверкой
            await asyncio.sleep(1)
            
    except Exception as e:
        print(f"Run Error: {e}")
        return False

# ==========================================
# 6. ГЛАВНЫЙ ЭНДПОИНТ (API)
# ==========================================

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    # Проверка конфигурации
    if not api_key or not assistant_id:
        return {"response": "Server Config Error (Keys Missing)", "thread_id": request.thread_id}
    
    # Проверка на пустое сообщение
    if not request.message.strip():
        return {"response": "...", "thread_id": request.thread_id}

    try:
        # 1. Работа с тредом (диалогом)
        if not request.thread_id:
            thread = await client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        # 2. Добавление сообщения пользователя
        await client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=request.message
        )

        # 3. Асинхронный запуск анализатора для Telegram
        # (Запускается параллельно, не тормозит ответ пользователю)
        asyncio.create_task(handle_telegram_notification(request.message, thread_id))

        # 4. Запуск генерации ответа AI
        success = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
        
        final_answer = ""
        if success:
            # Получение последнего сообщения
            messages = await client.beta.threads.messages.list(thread_id=thread_id)
            if messages.data:
                raw_answer = messages.data[0].content[0].text.value
                final_answer = clean_text(raw_answer)
            else:
                final_answer = "Ошибка получения ответа."
        else:
            # Ответ-заглушка при таймауте (чтобы не пугать ошибкой)
            final_answer = "Связь с базой данных устанавливается. Я анализирую ваш запрос, это займет еще пару секунд..."

        return {"response": final_answer, "thread_id": thread_id}

    except Exception as e:
        print(f"Global Endpoint Error: {e}")
        # Возвращаем нейтральный ответ вместо 500 Server Error
        return {"response": "Секунду, уточняю информацию...", "thread_id": request.thread_id}

# Простой эндпоинт для проверки статуса (Health Check)
@app.get("/")
def home():
    return {"status": "ThaiBot v15.0 (Clean Categories) is Running"}
