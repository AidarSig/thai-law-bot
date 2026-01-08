import os
import re
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APIError

# --- 1. НАСТРОЙКИ ---
api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")

if not api_key or not assistant_id:
    raise ValueError("CRITICAL: Проверь ключи в Environment Variables!")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Тайм-ауты и лимиты
ATTEMPT_TIMEOUT = 30
MAX_RETRIES = 2

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserRequest(BaseModel):
    message: str
    thread_id: str = None

# --- 2. ФУНКЦИИ ПОМОЩНИКИ ---

def clean_text(text):
    """
    Чистит текст от служебных аннотаций OpenAI (по ТЗ),
    но сохраняет полезное форматирование (жирный текст, списки).
    """
    if not text: return ""
    
    # 1. Удаляем аннотации типа 【4:0†source】 (Требование ТЗ)
    # Этот паттерн находит все, что находится внутри скобок 【 и 】
    text = re.sub(r'【.*?】', '', text)
    
    # 2. Удаляем возможные двойные пробелы, которые могли появиться после удаления сносок
    text = re.sub(r' +', ' ', text)
    
    return text.strip()

async def validate_answer_quality(answer_text):
    """ФУНКЦИЯ-КОНТРОЛЕР (ОТК)"""
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": (
                    "Ты строгий контролер качества. Проверь текст."
                    "Критерии ПРОВАЛА (отвечай 'BAD'):"
                    "1. Текст НЕ на русском."
                    "2. Текст содержит код, HTML или ошибки (Error 404)."
                    "3. Текст грубый."
                    "4. Текст бессвязный."
                    "Иначе отвечай 'GOOD'."
                )},
                {"role": "user", "content": f"Текст:\n{answer_text}"}
            ],
            temperature=0,
            max_tokens=5
        )
        verdict = response.choices[0].message.content.strip()
        print(f"🔎 JUDGE VERDICT: {verdict}") # ЛОГ ВЕРДИКТА
        
        return "GOOD" in verdict
            
    except Exception as e:
        print(f"Validator Error: {e}")
        return True 

async def run_assistant_with_timeout(thread_id, assistant_id, timeout):
    run = await client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id
    )
    start_time = asyncio.get_event_loop().time()
    
    while True:
        if (asyncio.get_event_loop().time() - start_time) > timeout:
            print(f"⏳ Time is up! Cancelling run {run.id}...")
            try:
                await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
            except Exception: pass
            raise asyncio.TimeoutError("Run took too long")

        run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

        if run_status.status == 'completed':
            return True
        elif run_status.status in ['failed', 'cancelled', 'expired']:
            print(f"❌ Run failed status: {run_status.status}")
            return False
        
        await asyncio.sleep(1)

# --- 3. ГЛАВНЫЙ ЭНДПОИНТ ---

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    try:
        # ЛОГИРУЕМ ВОПРОС ПОЛЬЗОВАТЕЛЯ
        print(f"\n📩 NEW MESSAGE [Thread: {request.thread_id}]")
        print(f"👤 USER: {request.message}")

        if not request.message.strip():
            return {"response": "...", "thread_id": request.thread_id}

        # А. Тред
        if not request.thread_id:
            thread = await client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        # Б. Сообщение
        await client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=request.message
        )

        # В. ЦИКЛ ПОПЫТОК
        raw_answer = ""
        success = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print(f"🔄 Attempt #{attempt} started...")
                is_finished = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
                
                if is_finished:
                    messages = await client.beta.threads.messages.list(thread_id=thread_id)
                    raw_answer = messages.data[0].content[0].text.value
                    
                    if not raw_answer or len(raw_answer) < 5:
                        continue

                    # ОТК
                    is_valid = await validate_answer_quality(raw_answer)
                    
                    if is_valid:
                        success = True
                        break 
                    else:
                        print(f"⛔ JUDGE REJECTED ANSWER: {raw_answer[:50]}...")
                        continue 
                
                if attempt == MAX_RETRIES: break 

            except asyncio.TimeoutError:
                print(f"⏰ Timeout attempt #{attempt}")
                continue

        # Д. РЕЗУЛЬТАТ
        if success:
            final_answer = clean_text(raw_answer)
            # ЛОГИРУЕМ ОТВЕТ БОТА
            print(f"🤖 BOT: {final_answer}")
            return {"response": final_answer, "thread_id": thread_id}
        else:
            print("💀 ALL ATTEMPTS FAILED")
            return {
                "response": "Извините, сейчас я не могу дать точный ответ на основании базы данных. Чтобы не вводить вас в заблуждение, прошу связаться с нашим менеджером напрямую.",
                "thread_id": thread_id
            }

    except RateLimitError:
        print("💸 RATE LIMIT HIT (Check Balance)")
        return {"response": "Сервис перегружен, попробуйте через 5 минут.", "thread_id": request.thread_id}
    except Exception as e:
        print(f"💥 SERVER ERROR: {e}")
        return {"response": "Техническая заминка. Повторите вопрос.", "thread_id": request.thread_id}

@app.get("/")
def home():
    return {"status": "Legal Bot (Logs Enabled) is active"}
