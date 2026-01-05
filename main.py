import os
import time
import logging
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI
from fastapi.middleware.cors import CORSMiddleware 

# Настройка логирования (чтобы видеть ошибки в логах Render)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Получаем ключи
api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")

# Инициализация OpenAI
client = OpenAI(api_key=api_key)
app = FastAPI()

# Разрешения для браузера (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Модель данных (БРОНЕБОЙНАЯ: принимает null и прощает ошибки)
class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None 

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Thai Law Bot is running"}

@app.post("/chat")
def chat(request: ChatRequest):
    try:
        user_message = request.message
        thread_id = request.thread_id

        logger.info(f"Received message: {user_message}, thread_id: {thread_id}")

        # 1. Если thread_id пустой или null, создаем новый
        if not thread_id:
            logger.info("Creating new thread...")
            thread = client.beta.threads.create()
            thread_id = thread.id
        
        # 2. Добавляем сообщение
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_message
        )

        # 3. Запускаем бота
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )

        # 4. Ждем ответ (с тайм-аутом, чтобы не висеть вечно)
        start_time = time.time()
        while run.status in ['queued', 'in_progress', 'cancelling']:
            # Если ждем дольше 50 секунд, прерываем (Render Free limit)
            if time.time() - start_time > 50:
                logger.error("Timeout reached")
                return {"response": "Сервер долго думает. Попробуйте еще раз.", "thread_id": thread_id}
            
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )

        # 5. Получаем ответ
        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            # Ищем последнее сообщение бота
            for msg in messages.data:
                if msg.role == "assistant":
                    if hasattr(msg.content[0], 'text'):
                        bot_response = msg.content[0].text.value
                        return {"response": bot_response, "thread_id": thread_id}
        
        logger.error(f"Run ended with status: {run.status}")
        return {"response": "Не удалось получить ответ от ИИ.", "thread_id": thread_id}

    except Exception as e:
        logger.error(f"CRITICAL ERROR: {str(e)}")
        # Возвращаем JSON даже при ошибке, чтобы фронтенд не падал
        return {"response": "Произошла техническая ошибка. Повторите запрос.", "thread_id": request.thread_id}
