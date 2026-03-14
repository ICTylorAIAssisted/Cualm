FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        prosody \
        prosody-modules \
        lua-dbi-sqlite3 \
        openssl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY prosody.cfg.lua /etc/prosody/prosody.cfg.lua
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Data persistence
VOLUME ["/var/lib/prosody"]

# XMPP client-to-server + HTTP
EXPOSE 5222 5280

ENTRYPOINT ["/entrypoint.sh"]
