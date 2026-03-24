"""WebArena benchmark configuration.

URL templates, site credentials, and default settings.
"""

import os

# ── URL template resolution ──────────────────────────────────────
# WebArena sites have hardcoded base URLs like http://localhost:7770.
# socat port forwards inside the agent container map localhost:PORT
# to the actual site containers on webarena-net.

URL_TEMPLATES = {
    "__SHOPPING__": "http://localhost:7770",
    "__SHOPPING_ADMIN__": "http://localhost:7780/admin",
    "__REDDIT__": "http://localhost:9999",
    "__GITLAB__": "http://localhost:8023",
    "__MAP__": "http://localhost:3000",
    "__WIKIPEDIA__": (
        "http://localhost:8888/wikipedia_en_all_maxi_2022-05"
        "/A/User:The_other_Kiwix_guy/Landing"
    ),
    "__HOMEPAGE__": "http://localhost:4399",
}

# Host-facing URLs for the WA-Verified evaluator (runs on host).
# Same ports — compose maps them to the host.
URL_TEMPLATES_HOST = {
    "__SHOPPING__": "http://localhost:7770",
    "__SHOPPING_ADMIN__": "http://localhost:7780/admin",
    "__REDDIT__": "http://localhost:9999",
    "__GITLAB__": "http://localhost:8023",
    "__MAP__": "http://localhost:3000",
    "__WIKIPEDIA__": (
        "http://localhost:8888/wikipedia_en_all_maxi_2022-05"
        "/A/User:The_other_Kiwix_guy/Landing"
    ),
    "__HOMEPAGE__": "http://localhost:4399",
}

# Port forwards: localhost:PORT → container:internal_port
# Passed to agent container as CUA_PORT_FORWARDS env var.
SITE_PORT_FORWARDS = {
    7770: ("shopping", 80),
    7780: ("shopping_admin", 80),
    9999: ("reddit", 80),
    8023: ("gitlab", 8023),
    4399: ("homepage", 80),
    # Phase 2:
    # 8888: ("wikipedia", 80),
    # 3000: ("map", 3000),
}

# ── Site credentials ─────────────────────────────────────────────
# Used to build the login-hint section of the agent system prompt.

SITE_CREDENTIALS = {
    "shopping": {
        "url": "http://localhost:7770",
        "username": "emma.lopez@gmail.com",
        "password": "Password.123",
    },
    "shopping_admin": {
        "url": "http://localhost:7780/admin",
        "username": "admin",
        "password": "admin1234",
    },
    "reddit": {
        "url": "http://localhost:9999",
        "username": "MarvelsGrantworthy",
        "password": "test1234",
    },
    "gitlab": {
        "url": "http://localhost:8023",
        "username": "byteblaze",
        "password": "hello1234",
    },
}

# ── Sites available in Phase 1 ───────────────────────────────────

PHASE1_SITES = {"shopping", "shopping_admin", "reddit", "gitlab"}

# ── Docker / agent defaults ──────────────────────────────────────

DEFAULT_AGENT_IMAGE = os.environ.get("CUA_AGENT_IMAGE", "cua-agent")
DEFAULT_NETWORK = "webarena-net"
DEFAULT_TIMEOUT = int(os.environ.get("CUA_AGENT_TIMEOUT", "") or 600)
DEFAULT_MAX_STEPS = int(os.environ.get("CUA_MAX_STEPS", "") or 40)
DEFAULT_CALIBRATION_VOLUME = os.environ.get(
    "CUA_CALIBRATION_VOLUME", "cua-calibration"
)

# LLM settings (passed through to spawned agent containers)
LLM_BASE_URL = os.environ.get(
    "OPENAI_BASE_URL", "http://host.docker.internal:8000/v1"
)
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "not-needed")
LLM_MODEL = os.environ.get("CUA_MODEL", "your-model-name")
