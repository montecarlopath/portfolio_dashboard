FROM python:3.11-slim

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    gfortran \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (better caching)
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy full app
COPY . .

# Create data directory
RUN mkdir -p /app/data

EXPOSE 8080

# Start server
CMD ["sh", "-c", "if [ -n \"$PD_CONFIG_JSON\" ] && [ ! -f \"${PD_CONFIG_PATH:-/app/data/config.json}\" ]; then echo \"$PD_CONFIG_JSON\" > \"${PD_CONFIG_PATH:-/app/data/config.json}\"; fi && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]