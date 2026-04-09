#!/bin/bash
python3 demo/xmpp_client.py \
    --jid "user@cua.local" \
    --password "${XMPP_HUMAN_PASSWORD:-user-secret}" \
    --to "orchestrator@cua.local" \
    --host "127.0.0.1" \
    --port 5222 \
    --message "Go to wikipedia.org, click on the English Wikipedia link, then click on today's featured article and tell me its title" \
    --wait 300
