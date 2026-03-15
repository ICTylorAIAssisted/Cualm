#!/bin/bash
set -e

DOMAIN="${XMPP_DOMAIN:-cua.local}"
AGENT_USER="${XMPP_AGENT_USER:-agent}"
AGENT_PASS="${XMPP_AGENT_PASSWORD:-agent-secret}"
HUMAN_USER="${XMPP_HUMAN_USER:-user}"
HUMAN_PASS="${XMPP_HUMAN_PASSWORD:-user-secret}"

CERT_DIR="/etc/prosody/certs"

# ── Generate self-signed cert if missing ─────────────────────
if [ ! -f "$CERT_DIR/$DOMAIN.crt" ]; then
    echo "==> Generating self-signed certificate for $DOMAIN"
    mkdir -p "$CERT_DIR"
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$CERT_DIR/$DOMAIN.key" \
        -out "$CERT_DIR/$DOMAIN.crt" \
        -days 3650 -nodes \
        -subj "/CN=$DOMAIN" \
        -addext "subjectAltName=DNS:$DOMAIN,DNS:upload.$DOMAIN,DNS:conference.$DOMAIN"
    chown prosody:prosody "$CERT_DIR/$DOMAIN.key" "$CERT_DIR/$DOMAIN.crt"
    chmod 640 "$CERT_DIR/$DOMAIN.key"
    echo "==> Certificate generated"
fi

# ── Start Prosody briefly to create accounts ─────────────────
# (prosodyctl needs the server config but not a running instance)
echo "==> Ensuring accounts exist"

# Register accounts (prosodyctl register is idempotent-ish: errors if exists)
prosodyctl register "$AGENT_USER" "$DOMAIN" "$AGENT_PASS" 2>/dev/null || true
prosodyctl register "$HUMAN_USER" "$DOMAIN" "$HUMAN_PASS" 2>/dev/null || true

# Register orchestrator account if configured
ORCH_USER="${XMPP_ORCH_USER:-}"
ORCH_PASS="${XMPP_ORCH_PASSWORD:-}"
if [ -n "$ORCH_USER" ] && [ -n "$ORCH_PASS" ]; then
    prosodyctl register "$ORCH_USER" "$DOMAIN" "$ORCH_PASS" 2>/dev/null || true
    echo "==> Accounts ready: ${AGENT_USER}@${DOMAIN}, ${HUMAN_USER}@${DOMAIN}, ${ORCH_USER}@${DOMAIN}"
else
    echo "==> Accounts ready: ${AGENT_USER}@${DOMAIN}, ${HUMAN_USER}@${DOMAIN}"
fi

# ── Start Prosody in foreground ──────────────────────────────
echo "==> Starting Prosody"
exec prosody -F
