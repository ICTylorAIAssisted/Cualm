FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV SCREEN_WIDTH=1280
ENV SCREEN_HEIGHT=800
ENV SCREEN_DEPTH=24

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    fluxbox \
    xdotool \
    scrot \
    x11vnc \
    chromium \
    chromium-sandbox \
    fonts-liberation \
    fonts-noto-color-emoji \
    python3 \
    python3-pip \
    python3-venv \
    dbus-x11 \
    procps \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /app/venv && \
    /app/venv/bin/pip install -r /app/requirements.txt

COPY start.sh /app/start.sh
COPY agent.py /app/agent.py
RUN chmod +x /app/start.sh

WORKDIR /app
ENTRYPOINT ["/app/start.sh"]
