import os
import re
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APIError

# --- 1. НАСТРОЙКИ ---
# Берем ключи из переменных окружения Render
# Исправлено согласно вашему скриншоту:
api_key = os.environ.get("OPENAI_API_KEY")
assistant_id = os.environ.get("ASSISTANT_ID") 

# Проверка ключей при старте
if not api_key:
    print("CRITICAL ERROR: OPENAI_API_KEY not found in env!")
if not assistant_id:
    print("CRITICAL ERROR: ASSISTANT_ID not found in env!")

client = AsyncOpenAI(api_key=api_key)
app = FastAPI()

# Увеличил таймаут ожидания ответа от ИИ до 60 секунд
ATTEMPT_TIMEOUT = 60 

# Настройка CORS (чтобы Тильда не блокировала запросы)
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

# --- 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def clean_text(text):
    """
    Очищает текст от технических сносок OpenAI вида 【4:0†source】.
    """
    if not text: return ""
    # Удаляем конструкции в скобках 【...】
    text = re.sub(r'【.*?】', '', text)
    # Удаляем лишние markdown символы, если нужно
    text = text.replace("###", "").replace("**", "")
    # Убираем двойные пробелы
    text = re.sub(r' +', ' ', text)
    return text.strip()

async def run_assistant_with_timeout(thread_id, assistant_id, timeout):
    """
    Запускает ассистента и ждет ответ не дольше timeout секунд.
    """
    try:
        run = await client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        start_time = asyncio.get_event_loop().time()
        
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                print(f"⏳ Timeout reached ({elapsed}s). Cancelling run...")
                try:
                    await client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                except: pass
                return False 

            run_status = await client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            if run_status.status == 'completed':
                return True
            elif run_status.status in ['failed', 'cancelled', 'expired']:
                print(f"❌ Run failed with status: {run_status.status}")
                return False
            
            # Ждем 1 секунду перед следующей проверкой
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Run Execution Error: {e}")
        return False

# --- 3. ГЛАВНЫЙ ЭНДПОИНТ ---

@app.post("/chat")
async def chat_endpoint(request: UserRequest):
    # Логирование входящего запроса (видно в Render Logs)
    print(f"\n📩 INCOMING: {request.message[:50]}... [Thread: {request.thread_id}]")

    if not api_key or not assistant_id:
        return {"response": "Ошибка сервера: Отсутствуют API ключи.", "thread_id": request.thread_id}

    if not request.message.strip():
        return {"response": "...", "thread_id": request.thread_id}

    try:
        # 1. Создание или восстановление треда
        if not request.thread_id:
            thread = await client.beta.threads.create()
            thread_id = thread.id
        else:
            thread_id = request.thread_id

        # 2. Отправка сообщения пользователя
        await client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=request.message
        )

        # 3. Запуск и ожидание (с таймаутом)
        success = await run_assistant_with_timeout(thread_id, assistant_id, ATTEMPT_TIMEOUT)
        
        if success:
            messages = await client.beta.threads.messages.list(thread_id=thread_id)
            # OpenAI возвращает сообщения в обратном порядке (последнее - первое в списке)
            raw_answer = messages.data[0].content[0].text.value
            
            # 4. Очистка текста
            final_answer = clean_text(raw_answer)
            print(f"🤖 RESPONSE SENT: {final_answer[:50]}...")
            
            return {"response": final_answer, "thread_id": thread_id}
        else:
            # Если не успели за таймаут (сервер просыпался)
            print("⚠️ Response too slow (Cold Start)")
            return {
                "response": "Сервер запускается из безопасного режима. Пожалуйста, отправьте сообщение еще раз — сейчас я отвечу мгновенно.",
                "thread_id": thread_id
            }

    except Exception as e:
        print(f"💥 GLOBAL ERROR: {e}")
        return {"response": "Техническая заминка. Пожалуйста, повторите вопрос.", "thread_id": request.thread_id}

# Простой route для проверки жизни сервера
@app.get("/")
def home():
    return {"status": "FastAPI ThaiBot is Running"}
