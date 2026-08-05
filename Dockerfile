# ---- Stage 1: build the bgutil PO-token provider ---------------------------
# YouTube now requires a "PO token" from web clients (the cause of
# "Requested format is not available" on datacenter IPs). This provider
# generates them. Built in a separate stage so the compile toolchain
# (canvas' native build) never bloats the final image.
FROM node:20-bookworm AS bgutil-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config python3 \
        libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*
ARG BGUTIL_VERSION=1.3.1
RUN curl -fsSL https://codeload.github.com/Brainicism/bgutil-ytdlp-pot-provider/tar.gz/refs/tags/${BGUTIL_VERSION} -o /tmp/bgutil.tar.gz \
    && mkdir -p /opt/bgutil-ytdlp-pot-provider \
    && tar -xzf /tmp/bgutil.tar.gz -C /opt/bgutil-ytdlp-pot-provider --strip-components=1 \
    && rm /tmp/bgutil.tar.gz \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && npm ci \
    && npx tsc \
    && npm prune --omit=dev \
    && npm cache clean --force

# ---- Stage 2: the bot -------------------------------------------------------
FROM python:3.12-slim

# ffmpeg: merge/convert media — deno: JS runtime yt-dlp needs for full YouTube
# format extraction. node + canvas runtime libs: run the PO-token generator.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip ca-certificates libstdc++6 \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 fontconfig \
    && curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && curl -fsSL https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.gz -o /tmp/node.tar.gz \
    && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.gz \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bgutil-builder /opt/bgutil-ytdlp-pot-provider /opt/bgutil-ytdlp-pot-provider

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
