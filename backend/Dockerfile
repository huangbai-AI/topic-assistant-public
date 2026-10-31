FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TA_DATA_DIR=/data \
    TA_COOKIE_DIR=/data/cookies \
    TA_DATABASE_URL=sqlite:////data/topic_assistant.db \
    TA_SCRAPE_HEADLESS=true

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
