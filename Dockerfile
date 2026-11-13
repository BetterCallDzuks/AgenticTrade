FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY py/research_bot.py py/replay_v10.py ./py/
COPY html ./html
COPY bettercalltrader ./bettercalltrader

# Mutable state (DB, logs, audit trail, ML model). docker-compose.yml mounts
# ./data here; creating it in the image keeps a plain `docker run` working too.
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "py/research_bot.py"]
