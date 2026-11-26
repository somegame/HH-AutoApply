FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
WORKDIR /opt/autoapply
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium-headless-shell fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip --disable-pip-version-check --no-cache-dir --timeout 20 --retries 2 \
    install -r requirements.txt
COPY bot.py .
COPY app ./app
CMD ["python", "bot.py"]
