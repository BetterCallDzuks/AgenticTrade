FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY research_bot.py replay_v10.py command_center.html analytics.html ./
COPY bettercalltrader ./bettercalltrader

# Mutable state (DB, logs, audit trail, ML model). docker-compose.yml mounts
# ./data here; creating it in the image keeps a plain `docker run` working too.
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "research_bot.py"]
