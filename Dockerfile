FROM python:3.11-slim

# No .pyc writes (the container filesystem is mounted read-only), no bytecode
# staleness, no pip cache in the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY py/research_bot.py py/replay_v10.py ./py/
COPY html ./html
COPY bettercalltrader ./bettercalltrader

# Run as a non-root user. The default UID/GID matches the usual first Linux
# user so the ./data bind mount stays writable; on another host build with
#   docker compose build --build-arg BCT_UID=$(id -u) --build-arg BCT_GID=$(id -g)
ARG BCT_UID=1000
ARG BCT_GID=1000
RUN groupadd --gid "${BCT_GID}" bct \
    && useradd --uid "${BCT_UID}" --gid bct --home-dir /app \
        --shell /usr/sbin/nologin bct \
    && mkdir -p /app/data \
    && chown -R bct:bct /app

USER bct

# Mutable state (DB, logs, audit trail, ML model). docker-compose.yml mounts
# ./data here; creating it in the image keeps a plain `docker run` working too.
EXPOSE 8080

CMD ["python", "py/research_bot.py"]
