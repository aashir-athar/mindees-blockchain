FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ruff.toml ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Chain data (genesis.json + blocks.jsonl) lives here; mount a volume to persist it.
VOLUME /data
EXPOSE 9000

ENTRYPOINT ["docker-entrypoint.sh"]
