FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    gfortran \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["sh", "-c", "if [ -n \"$PD_CONFIG_JSON\" ] && [ ! -f \"${PD_CONFIG_PATH:-/app/data/config.json}\" ]; then echo \"$PD_CONFIG_JSON\" > \"${PD_CONFIG_PATH:-/app/data/config.json}\"; fi && export PD_CONFIG_PATH=\"${PD_CONFIG_PATH:-/app/data/config.json}\" && exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
