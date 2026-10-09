FROM python:3.12-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="Owen Bot" \
      org.opencontainers.image.description="Telegram-бот уведомлений OwenCloud" \
      org.opencontainers.image.source="https://github.com/WhiteManPrk/Owen_Bot" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    APP_VERSION=${VERSION}

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]
