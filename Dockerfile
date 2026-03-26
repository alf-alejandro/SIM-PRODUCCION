FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY strategy_core_prod.py .
COPY hedge_live_prod.py .
COPY templates/ templates/

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV STATE_FILE=/app/data/state.json
ENV LOG_FILE=/app/data/hedge_log.json
ENV EVENTS_FILE=/app/data/events.log
ENV PORT=8080

EXPOSE 8080

CMD ["python", "hedge_live_prod.py"]
