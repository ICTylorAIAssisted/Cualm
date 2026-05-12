FROM debian:bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV SCREEN_WIDTH=1280
ENV SCREEN_HEIGHT=720
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
    fonts-dejavu-core \
    python3 \
    python3-pip \
    python3-venv \
    dbus-x11 \
    procps \
    curl \
    ffmpeg \
    python3-pil \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --break-system-packages -r /app/requirements.txt

COPY start.sh /app/start.sh
COPY agent.py /app/agent.py
COPY cdp_a11y.py /app/cdp_a11y.py
COPY cua_config.py /app/cua_config.py
COPY plugin_host.py /app/plugin_host.py
COPY tool_discovery.py /app/tool_discovery.py
COPY config.ini /app/config.ini
COPY tools /app/tools
COPY plugins /app/plugins
COPY extensions /app/extensions
COPY calibration /app/calibration
COPY benchmark/coverage.py /app/benchmark/coverage.py
COPY benchmark/coverage-target /app/benchmark/coverage-target
COPY benchmark/coverage-test /app/benchmark/coverage-test
COPY coverage-entrypoint.sh /app/coverage-entrypoint.sh
COPY screenshots_to_video.py /app/screenshots_to_video.py

RUN chmod +x /app/tools/* && \
    chmod +x /app/agent.py && \
    chmod +x /app/start.sh && \
    chmod +x /app/coverage-entrypoint.sh && \
    chmod +x /app/screenshots_to_video.py && \
    mkdir -p /var/cua/calibration

# Tools import cua_config as a module — make sure /app is on PYTHONPATH
ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV PATH="/app/tools:${PATH}"
# Playwright: we connect to the existing Chromium via CDP, no browser download needed
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
# Suppress Node.js deprecation warnings (e.g. DEP0169 from Playwright's runtime)
ENV NODE_NO_WARNINGS=1

WORKDIR /app
ENTRYPOINT ["/app/start.sh"]
