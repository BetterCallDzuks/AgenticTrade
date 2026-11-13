FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY research_bot.py replay_v10.py command_center.html analytics.html ./
COPY bettercalltrader ./bettercalltrader

EXPOSE 8080

CMD ["python", "research_bot.py"]