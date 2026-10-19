# The app server and bot in one container: python -m server
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY server ./server
COPY web ./web

# Run as a normal user; state, logs and price data live in mounted folders.
RUN useradd --create-home --uid 1000 bot && mkdir -p state logs data && chown -R bot /app
USER bot

ENV APP_HOST=0.0.0.0 APP_PORT=8000 STATE_DIR=state LOG_DIR=logs KILL_SWITCH_FILE=state/STOP
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/session')"
CMD ["python", "-m", "server"]
