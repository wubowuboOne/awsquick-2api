
FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    ca-certificates curl git tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir -r requirements.txt

# Node.js gateway dependencies
COPY vendor/quick-gateway/package.json vendor/quick-gateway/package.json
RUN cd vendor/quick-gateway \
    && npm install --omit=dev

# Application source
COPY . .

# Browser dependencies
RUN python -m patchright install --with-deps chromium

# Persistent data directory
RUN mkdir -p /data/oauth \
    && ln -s /data /app/data

EXPOSE 8787 3426

CMD ["/bin/bash", "-lc", "cd /app/vendor/quick-gateway && HOST=0.0.0.0 PORT=3426 node bin/serve-oauth.mjs & cd /app && python app.py"]
