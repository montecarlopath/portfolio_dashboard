FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    gfortran \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

# Startup script — writes config.json from PD_CONFIG_JSON env var on first boot
RUN printf '#!/bin/sh\nset -e\nCONFIG_PATH="${PD_CONFIG_PATH:-/app/data/config.json}"\nif [ ! -f "$CONFIG_PATH" ] && [ -n "$PD_CONFIG_JSON" ]; then\n  echo "$PD_CONFIG_JSON" > "$CONFIG_PATH"\nfi\nexport PD_CONFIG_PATH="$CONFIG_PATH"\nexec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"\n' > /app/start.sh && chmod +x /app/start.sh

EXPOSE 8000
CMD ["/app/start.sh"]
