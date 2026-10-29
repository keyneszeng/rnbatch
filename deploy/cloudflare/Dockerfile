FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Unprivileged runtime and persistent /data for the SQLite account store.
RUN groupadd --system --gid 10001 rnbatch \
    && useradd --system --uid 10001 --gid 10001 \
        --home-dir /home/rnbatch --create-home rnbatch \
    && mkdir -p /data \
    && chown rnbatch:rnbatch /data

COPY . /app
RUN python -m pip install --no-cache-dir ".[docs]"

USER rnbatch
EXPOSE 8766
CMD ["rnbatch", "web", "--port", "8766", "--db", "/data/users.sqlite3"]
