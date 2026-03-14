# Compose Extensions

Drop-in extensions that add services alongside the CUA agent.
Each subdirectory contains a `compose.yml` fragment that is
auto-discovered and merged by `./run.sh up`.

## How it works

When you run `./run.sh up`, the script:

1. Starts with the base `docker-compose.yml` (agent only)
2. Scans `compose.d/*/compose.yml` for fragments
3. Passes all files as `-f` flags to compose

Docker Compose merges them: new services are added, existing
services are extended (env vars, volumes, depends_on, etc.).

## Adding an extension

Create a directory with a `compose.yml`:

```
compose.d/my-extension/
  compose.yml       ← required — compose fragment
  my-service/       ← optional — Dockerfile + config for new services
    Dockerfile
    ...
```

The fragment can add new services and/or extend the `cua` service:

```yaml
# compose.d/my-extension/compose.yml
services:
  my-service:
    build: ./compose.d/my-extension/my-service
    ports:
      - "8080:8080"

  # Extend the agent with env vars for the Python plugin
  cua:
    depends_on:
      - my-service
    environment:
      MY_SERVICE_URL: http://my-service:8080
```

## Removing an extension

Delete or rename the directory:

```bash
rm -rf compose.d/xmpp        # removes XMPP support
mv compose.d/xmpp compose.d/.xmpp  # disables without deleting
```

Directories starting with `.` are ignored.

## Bundled extensions

### audit/

Mounts a host directory into the agent container for audit data
persistence.  Pairs with `plugins/audit.py` — if you remove the
plugin, remove this directory too.

### xmpp/

Adds a Prosody XMPP server so the agent can exchange messages
with a human user over a phone.  Works with the `plugins/xmpp/`
Python plugin that provides the CLI tools.

```
compose.d/xmpp/
  compose.yml           Prosody service + agent XMPP env vars
  prosody/
    Dockerfile          Prosody image
    prosody.cfg.lua     Server configuration
    entrypoint.sh       Cert generation + account creation
```

## Relationship to Python plugins

Compose extensions and Python plugins are complementary:

- **Compose extension** = infrastructure (adds containers, networks,
  ports, env vars).  Lives in `compose.d/`.
- **Python plugin** = agent behavior (hooks, tools, prompt changes).
  Lives in `plugins/`.

They often come in pairs.  The XMPP integration has both:
- `compose.d/xmpp/` — runs the Prosody server
- `plugins/xmpp/` — provides cua-xmpp-* tools and daemon lifecycle

But either can exist independently.  A compose extension might add a
database that the agent accesses via shell commands (no Python plugin
needed).  A Python plugin might swap the LLM backend (no extra
container needed).
