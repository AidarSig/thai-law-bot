import os
import re
import asyncio
import time
import datetime
import httpx  # Используем httpx для асинхронных запросов
from typing import Optional, Dict, Tuple, List
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI

# ==========================================
# 1. НАСТРОЙКИ
# ==========================================

api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID")
tg_token = os.environ.get("TELEGRAM_TOKEN")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
APP_DOMAIN = os.environ.get("APP_DOMAIN", "")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Таймер тишины (3 минуты)
ANALYSIS_DELAY_SECONDS = 180
ATTEMPT_TIMEOUT = 110

# ХРАНИЛИЩА ДАННЫХ
# Когда была активность
threads_last_activity: Dict[str, float] = {}
# Сами задачи мониторинга
threads_monitoring_tasks: Dict[str, asyncio.Task] = {}
# СКОЛЬКО СООБЩЕНИЙ УЖЕ ОТПРАВЛЕНО В ТГ (Для дельта-обновлений)
threads_msg_counts: Dict[str, int] = {}

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

# ==========================================
# 2. ФУНКЦИИ ОБРАБОТКИ ТЕКСТА
# ==========================================

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'【.*?】', '', text)
    text = text.replace("###", "").replace("**", "")
    text = text.replace("<", "&lt;").replace(">", "&gt;") 
    return text.strip()

async def get_raw_messages(thread_id: str) -> List:
    """Получает ВСЕ сообщения из OpenAI в хронологическом порядке (от старых к новым)."""
    try:
        # Берем с запасом (100), чтобы точно охватить контекст
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=100)
        # OpenAI отдает от новых к старым. Разворачиваем -> [Старое, ..., Новое]
        return list(reversed(messages.data))
    except Exception as e:
        print(f"Error fetching messages: {e}")
        return []

def format_messages_for_tg(messages: List) -> Tuple[str, str, str]:
    """Формирует текст только из переданного списка сообщений."""
    user_blob = "" 
    bot_blob = ""
    temp_buffer = []

    for msg in messages:
        if hasattr(msg.content[0], 'text'):
            content = clean_text(msg.content[0].text.value)
            chunk = ""
            if msg.role == "user":
                chunk = f"👤 <b>Клиент:</b>\n{content}\n\n"
                user_blob += content + " "
            elif msg.role == "assistant":
                chunk = f"🤖 <b>Бот:</b>\n{content}\n\n"
                bot_blob += content + " "
            temp_buffer.append(chunk)

    final_history_str = "".join(temp_buffer)
    return final_history_str, user_blob, bot_blob

# ==========================================
# 3. ОТПРАВКА В ТЕЛЕГРАМ (АСИНХРОННО)
# ==========================================

async def send_tg_safe(text: str):
    if not tg_token or not tg_chat_id: return
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    
    # HTML Mode
    payload = {"chat_id": tg_chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    
    # ИСПОЛЬЗУЕМ HTTPX ВМЕСТО REQUESTS ДЛЯ АСИНХРОННОСТИ
    async with httpx.AsyncClient() as http_client:
        try:
            await http_client.post(url, json=payload)
        except Exception:
            # Plain Text Fallback
            clean_msg = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "").replace('<a href="', '').replace('">', ' ')
            fallback_payload = {"chat_id": tg_chat_id, "text": clean_msg}
            try:
                await http_client.post(url, json=fallback_payload)
            except Exception as e:
                print(f"TG Send Error: {e}")

async def check_and_send_notification(thread_id: str, new_messages: List, is_update: bool):
    """
    Отправляет уведомление.
    is_update = True -> Это дополнение к старому диалогу.
    is_update = False -> Это новый диалог.
    """
    
    # 1. Формируем тело сообщения (только новые сообщения)
    formatted_body, user_blob, _ = format_messages_for_tg(new_messages)
    
    # Если в новых сообщениях нет текста от юзера (только бот), можно не слать, 
    # но лучше слать для контекста. Оставим проверку на пустоту.
    if not formatted_body: return

    # 2. Формируем заголовок
    if is_update:
        header_title = "🔔 <b>ДОПОЛНЕНИЕ К ДИАЛОГУ</b>"
    else:
        header_title = "💬 <b>НОВЫЙ ДИАЛОГ</b>"

    # Проверка на контакты (ищем в НОВОЙ части переписки)
    contact_info = ""
    if re.search(r'\d{7,}', user_blob.replace(' ', '')) or ("@" in user_blob):
        contact_info = " (Клиент оставил контакт 📞)"

    # Ссылка на полную историю
    web_link = f"{APP_DOMAIN}/history/{thread_id}" if APP_DOMAIN else f"/history/{thread_id}"

    # 3. Сборка итогового сообщения
    # Структура:
    # ЗАГОЛОВОК
    # ID: thread_...
    # ----------------
    # (Только новые сообщения)
    # ----------------
    # Ссылка
    
    msg = (
        f"{header_title}{contact_info}\n"
        f"🆔 <code>{thread_id}</code>\n"
        f"➖➖➖➖➖➖➖\n\n"
        f"{formatted_body}"
        f"➖➖➖➖➖➖➖\n"
        f"🔗 <a href='{web_link}'>Открыть ВЕСЬ диалог (Веб)</a>"
    )
    
    await send_tg_safe(msg)

# ==========================================
# 4. УМНЫЙ МОНИТОРИНГ (DELTA LOGIC)
# ==========================================

async def monitor_chat_activity(thread_id: str):
    try:
        while True:
            # ВАЖНО: asyncio.sleep вместо time.sleep, чтобы не блокировать сервер
            await asyncio.sleep(5)
            last_time = threads_last_activity.get(thread_id, 0)
            
            # Таймер сработал (3 минуты тишины)
            if time.time() - last_time > ANALYSIS_DELAY_SECONDS:
                
                # 1. Получаем ВЕСЬ список сообщений (старые + новые)
                all_messages = await get_raw_messages(thread_id)
                total_count = len(all_messages)
                
                # 2. Вспоминаем, сколько мы уже отправляли
                sent_count = threads_msg_counts.get(thread_id, 0)
                
                # 3. Если появились новые сообщения
                if total_count > sent_count:
                    # Берем срез: от sent_count до конца
                    # Пример: было 5, стало 8. Берем с 5-го по 8-й.
                    messages_to_send = all_messages[sent_count:]
                    
                    # Определяем тип: это новый диалог или апдейт?
                    is_update = (sent_count > 0)
                    
                    # Отправляем
                    await check_and_send_notification(thread_id, messages_to_send, is_update)
                    
                    # 4. Обновляем счетчик отправленных
                    threads_msg_counts[thread_id] = total_count
                
                # Выходим из цикла мониторинга (пока юзер снова не напишет)
                break
                
    except asyncio.CancelledError:
        pass
    finally:
        threads_monitoring_tasks.pop(thread_id, None)

# ==========================================
# 5. AI LOGIC
# ==========================================

async def run_assistant(thread_id, assistant_id):
    try:
        # ОБНОВЛЕННАЯ ИНСТРУКЦИЯ v10.0 (Полная версия + Приоритеты)
        instructions = """
РОЛЬ И НАЗВАНИЕ: Ты — ИИ-ассистент «Центра правовой помощи соотечественникам в Таиланде». Твоя задача — консультировать граждан, опираясь СТРОГО на предоставленные материалы.

ГЛАВНОЕ ПРАВИЛО ОТВЕТА (ПРИОРИТЕТЫ):
1. ВСЕГДА старайся сначала найти информацию в базе и дать ответ. Если вопрос простой или справочный (например, "какие нужны документы", "сколько стоит виза", "правила въезда") — ТЫ ОБЯЗАН ОТВЕТИТЬ САМ. Не давай контакты сразу.
2. Если вопрос сложный (например, про бордер-раны, риски отказов), поищи в базе ОБЩИЕ ПРАВИЛА на эту тему и процитируй их.
3. ЗАПРЕЩЕНО писать "свяжитесь с нами" ПЕРВЫМ сообщением, если в базе есть хоть какая-то информация по теме вопроса.

ГЛАВНЫЕ ЗАПРЕТЫ:
1. НЕ называй себя LexPrime.
2. НЕ проси номер телефона первым (если можешь ответить сам).
3. ЗАПРЕТ НА ТЕРМИНЫ: Никогда не говори клиенту фразы "в моей базе знаний", "в файле", "согласно базе". Заменяй их на "согласно законодательству", "по правилам" или "информация следующая".

ГЕОГРАФИЯ: Твоя юрисдикция — ТОЛЬКО ТАИЛАНД.

АНТИ-ГАЛЛЮЦИНАЦИИ: 
Если ответа НЕТ в материалах ИЛИ вопрос требует оценки личных рисков (например "пустят ли меня"):
Начни ответ со второго предложения строго с этой фразы:
"Этот вопрос требует индивидуального юридического анализа ситуации и оценки рисков. Пожалуйста, свяжитесь с нами для консультации:"

КОНТАКТНАЯ ИНФОРМАЦИЯ: Давай их только если вопрос сложный/нет ответа в базе (см. правило выше). Формат строго такой:
Контакты:
Телефон: +66 96-004-9705
Email: pravothai@lexprimethailand.com

ТВОЯ ЛИЧНОСТЬ:
Если спрашивают "Ты робот?" или "Кто ты?":
Отвечай: "Я специализированный ИИ-ассистент Центра правовой помощи. Я помогаю сориентироваться в вопросах законодательства Таиланда, а сложные случаи передаю нашим юристам."

Стиль: Спокойный, уверенный, профессиональный. Без лишней "воды" и маркетинговых клише.

АЛГОРИТМ ДИАЛОГА:
1. Проверь наличие информации в знаниях.
2. Если есть: ответь четко и по делу (соблюдая приоритеты).
3. Если нет или риск: используй скрипт перевода на контакты.
4. Если ситуация критическая (полиция, тюрьма): сразу дай контакты.

ПРИМЕРЫ ДИАЛОГОВ:

User: "Кто ты?" 
Assistant: "Я специализированный ИИ-ассистент Центра правовой помощи. Я помогаю сориентироваться в вопросах законодательства Таиланда, а сложные случаи передаю нашим юристам."

User: "Что с визами?" 
Assistant: "Граждане РФ получают штамп по прибытии на 60 дней. При необходимости его можно продлить в иммиграционном офисе еще на 30 дней."
"""

        run = await client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id,
            additional_instructions=instructions
        )
        
        start = time.time()
        while time.time() - start < ATTEMPT_TIMEOUT:
            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run_status.status == 'completed':
                msgs = await client.beta.threads.messages.list(thread_id=thread_id)
                if msgs.data: return msgs.data[0].content[0].text.value
                return ""
            elif run_status.status in ['failed', 'expired', 'cancelled']:
                return "Пожалуйста, свяжитесь с нами по телефону +66 96-004-9705."
            # ВАЖНО: Асинхронное ожидание, чтобы не блокировать сервер
            await asyncio.sleep(1)
        
        try: await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
        except: pass
        return "Связь нестабильна. Свяжитесь с нами по телефону +66 96-004-9705 или напишите нам на почту pravothai@lexprimethailand.com"

    except Exception as e:
        print(f"Run Error: {e}")
        return "Внутренняя ошибка сервера."

# ==========================================
# 6. ENDPOINTS
# ==========================================

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    if not api_key or not assistant_id:
        return {"response": "Config Error", "thread_id": request.thread_id}

    thread_id = request.thread_id
    if not thread_id:
        thread = await client.beta.threads.create()
        thread_id = thread.id

    threads_last_activity[thread_id] = time.time()

    if thread_id not in threads_monitoring_tasks:
        task = asyncio.create_task(monitor_chat_activity(thread_id))
        threads_monitoring_tasks[thread_id] = task

    await client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=request.message
    )
    
    response_text = await run_assistant(thread_id, assistant_id)
    
    return {
        "response": clean_text(response_text),
        "thread_id": thread_id
    }

@app.get("/history/{thread_id}", response_class=HTMLResponse)
async def get_history_page(thread_id: str):
    # Здесь мы всегда показываем ПОЛНУЮ историю для браузера
    raw_msgs = await get_raw_messages(thread_id)
    html_content = ""
    for msg in raw_msgs:
        if hasattr(msg.content[0], 'text'):
            text = clean_text(msg.content[0].text.value)
            role_cls = "user" if msg.role == "user" else "assistant"
            role_name = "👤 Клиент" if msg.role == "user" else "🤖 Бот"
            msg_time = datetime.datetime.fromtimestamp(msg.created_at).strftime('%Y-%m-%d %H:%M')
            html_content += f"""
            <div class="message {role_cls}">
                <div class="meta">{role_name} | {msg_time}</div>
                <div class="text">{text}</div>
            </div>
            """

    full_page = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Диалог {thread_id}</title>
        <style>
            body {{ font-family: sans-serif; max-width: 800px; margin: 20px auto; padding: 20px; background: #f4f6f8; }}
            .message {{ padding: 15px; margin-bottom: 15px; border-radius: 10px; background: white; border-left: 5px solid #ccc; }}
            .user {{ border-left-color: #007bff; }}
            .assistant {{ border-left-color: #28a745; }}
            .meta {{ font-weight: bold; font-size: 0.85em; color: #555; margin-bottom: 8px; }}
            .text {{ white-space: pre-wrap; }}
        </style>
    </head>
    <body>
        <h2>📁 Полный архив диалога</h2>
        <div style="background: #e2e6ea; padding: 10px; margin-bottom: 20px; border-radius: 5px;">ID: <code>{thread_id}</code></div>
        {html_content}
    </body>
    </html>
    """
    return HTMLResponse(content=full_page)

@app.get("/")
def home():
    return {"status": "ThaiLawBot v10.0 (Full Instructions + Priorities)"}
