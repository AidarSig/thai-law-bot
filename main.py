import os
import re
import asyncio
import requests
from typing import Optional, Set, Tuple
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI

# --- 1. НАСТРОЙКИ ---
api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID")
tg_token = os.environ.get("TELEGRAM_TOKEN")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

ATTEMPT_TIMEOUT = 110 
leads_db: Set[str] = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None

# --- 2. УПРОЩЕННЫЕ КАТЕГОРИИ ---

CATEGORIES = {
    "🔴 КРИМИНАЛ/SOS": [
        "полици", "тюрьм", "арест", "задержа", "участок", "суд", "депорт", 
        "нарко", "драка", "авари", "дтп", "police", "jail", "arrest", "sos", "prison"
    ],
    "🛂 БИЗНЕС/ВИЗЫ": [
        "виза", "визу", "visa", "компани", "бизнес", "счет", "банк", "work permit", 
        "ворк пермит", "открыть", "bank", "company", "лицензи", "license", "weed", "каннабис"
    ],
    "🏡 НЕДВИЖИМОСТЬ": [
        "вилл", "квартир", "земл", "участ", "недвиж", "condo", "villa", "land", 
        "buy", "rent", "аренд", "покуп", "chanote", "чанот"
    ],
    "💍 ГРАЖДАНСКОЕ": [
        "развод", "жен", "муж", "ребен", "дите", "брак", "divorce", "marriage", 
        "wife", "husband", "child", "долг", "займ", "наследств"
    ],
    "⚠️ НЕДОВЕРИЕ": [
        "развод", "скам", "настоящий", "человек", "робот", "бот", "гаранти", 
        "офис", "живой", "scam", "real", "human", "отзывы"
    ]
}

CONTACT_KEYWORDS = [
    "контакт", "телефон", "номер", "позвонить", "связ", "адрес", "почта", 
    "contact", "phone", "number", "call", "address", "whatsapp", "telegram"
]

# --- 3. ЛОГИКА ---

def clean_text(text):
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    return text.strip()

async def get_history_data(thread_id) -> Tuple[str, int]:
    try:
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=30)
        history_list = list(reversed(messages.data))
        formatted_text = ""
        user_msg_count = 0
        for msg in history_list:
            role = msg.role
            content = clean_text(msg.content[0].text.value)
            if role == "user":
                user_msg_count += 1
                formatted_text += f"👤 Клиент: {content}\n\n"
            elif role == "assistant":
                formatted_text += f"🤖 Юрист: {content}\n\n"
        return formatted_text, user_msg_count
    except Exception as e:
        print(f"History Error: {e}")
        return "(Ошибка истории)", 0

def detect_category(text) -> str:
    """Возвращает только ОДНУ, самую приоритетную категорию"""
    text_lower = text.lower()
    
    # Приоритет 1: Криминал (самое важное)
    for kw in CATEGORIES["🔴 КРИМИНАЛ/SOS"]:
        if kw in text_lower: return "🔴 КРИМИНАЛ/SOS"
        
    # Приоритет 2: Остальные
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in text_lower:
                return cat
    
    return "" # Если категория не определена

async def handle_telegram_notification(text, thread_id):
    if not tg_token or not tg_chat_id: return

    clean_msg = re.sub(r'[\s\-]', '', text)
    has_contact = re.search(r'\d{7,}', clean_msg) or ("@" in text and len(text) < 50)
    category = detect_category(text)

    # 1. ЕСТЬ КОНТАКТ -> ЭТО ЛИД
    if has_contact:
        header = f"🔥 <b>НОВЫЙ ЛИД!</b> {category}"
        
        if thread_id not in leads_db:
            leads_db.add(thread_id)
            history_text, _ = await get_history_data(thread_id)
            msg = (f"{header}\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"{history_text}"
                   f"➖➖➖➖➖➖➖\n"
                   f"🆔 <code>{thread_id}</code>")
            await send_to_tg(msg)
        else:
            msg = (f"📝 <b>ДОП. ИНФО</b> {category}\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"👤 Клиент: {text}\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"🔗 <code>{thread_id}</code>")
            await send_to_tg(msg)
        return

    # 2. НЕТ КОНТАКТА -> СМОТРИМ ПОВЕДЕНИЕ

    # A. Криминал (SOS) шлем сразу
    if "КРИМИНАЛ" in category and thread_id not in leads_db:
        leads_db.add(thread_id)
        history_text, _ = await get_history_data(thread_id)
        msg = (f"{category}\n"
               f"<i>ТРЕВОГА (Без контакта)!</i>\n"
               f"➖➖➖➖➖➖➖\n"
               f"{history_text}"
               f"➖➖➖➖➖➖➖\n"
               f"🆔 <code>{thread_id}</code>")
        await send_to_tg(msg)
        return

    # B. Запрос контактов
    is_asking_contacts = any(word in text.lower() for word in CONTACT_KEYWORDS)
    if is_asking_contacts and thread_id not in leads_db:
        history_text, user_count = await get_history_data(thread_id)
        if user_count > 2:
            leads_db.add(thread_id)
            msg = (f"👀 <b>ЗАПРОС КОНТАКТОВ</b> {category}\n"
                   f"<i>Клиент просит связь</i>\n"
                   f"➖➖➖➖➖➖➖\n"
                   f"{history_text}"
                   f"➖➖➖➖➖➖➖\n"
                   f"🆔 <code>{thread_id}</code>")
            await send_to_tg(msg)

async def send_to_tg(text):
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = { "chat_id": tg_chat_id, "text": text, "parse_mode": "HTML" }
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: requests.post(url, json=payload))
    except Exception as e:
        print(f"TG Error: {e}")

# --- 4. ASSISTANT ---

async def run_assistant_with_timeout(thread_id, assistant_id, timeout):
    try:
        run = await client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)
        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                try: await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                except: pass
                return False 
            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run_status.status == 'completed': return True
            elif run_status.status in ['failed', 'cancelled', 'expired']: return False
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Run Error: {e}")
        return False

# --- 5. ENDPOINT ---

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    if not api_key or not assistant_id:
        return {"response": "Server Config Error", "thread_id": request.thread_id}
    if not request.message.strip():
        return {"response": "...", "thread_id": request.thread_id}

    try:
        if not request.thread_id:
            thread = await client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        await client.beta.threads.messages.create(
            thread_id=thread_id, role="user", content=request.message
        )

        asyncio.create_task(handle_telegram_notification(request.message, thread_id))

        success = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
        
        final_answer = ""
        if success:
            messages = await client.beta.threads.messages.list(thread_id=thread_id)
            raw_answer = messages.data[0].content[0].text.value
            final_answer = clean_text(raw_answer)
        else:
            final_answer = "Связь установлена. Подбираю ответ..."

        return {"response": final_answer, "thread_id": thread_id}

    except Exception as e:
        print(f"Error: {e}")
        return {"response": "Секунду...", "thread_id": request.thread_id}

@app.get("/")
def home():
    return {"status": "ThaiBot v15 (Clean Categories)"}
