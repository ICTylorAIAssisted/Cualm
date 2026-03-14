# XMPP Messaging Plugin

Enables the CUA agent to exchange messages with a human user over
XMPP.  The human connects from a phone (or any XMPP client), and
the agent can send progress updates, ask questions, and receive
instructions in real time.

## Architecture

```
Phone (Conversations app)
  ↕ XMPP (port 5222)
Prosody server (separate container)
  ↕ XMPP (internal network)
XMPP daemon (inside CUA container)
  ↕ filesystem (inbox / outbox spool)
Agent tools (cua-xmpp-send, cua-xmpp-recv, ...)
  ↕ stdout markers
Agent loop
```

The daemon maintains a persistent connection and bridges messages
to/from JSON files in a spool directory.  The CLI tools read and
write those files.  This means the tools are instant (no network
round-trip) and the agent loop never blocks on XMPP I/O.

## Quick start

The easiest way is with Docker Compose from the project root:

```bash
# Start both Prosody and the agent
./run.sh up

# Run a task
./run.sh agent "your task"
```

The compose extension (`compose.d/xmpp/compose.yml`) is auto-discovered
by `run.sh` and merged with the base compose file.

### Connecting a phone

Install any XMPP client — [Conversations](https://conversations.im/)
(Android) or [Monal](https://monal-im.org/) (iOS) work well.

Configure the account:

| Field     | Value                               |
|-----------|-------------------------------------|
| JID       | `user@cua.local`                    |
| Password  | `user-secret` (or your override)    |
| Server    | Your host's IP address              |
| Port      | `5222`                              |

The server uses a self-signed certificate — the client will ask you
to trust it on first connection.

## Environment variables

Set these on the CUA container (not the Prosody container):

| Variable           | Default              | Purpose                        |
|--------------------|----------------------|--------------------------------|
| `XMPP_JID`        | `agent@localhost`    | Agent's full JID               |
| `XMPP_PASSWORD`   | *(empty)*            | Agent's XMPP password          |
| `XMPP_PEER_JID`   | `user@localhost`     | Human user's JID               |
| `XMPP_HOST`       | *(from JID)*         | Server hostname                |
| `XMPP_PORT`       | `5222`               | Server port                    |
| `XMPP_VERIFY_CERT`| `false`              | Verify server TLS certificate  |
| `XMPP_SPOOL_DIR`  | `/var/xmpp`          | Inbox/outbox spool directory   |
| `XMPP_AUTO_START`  | `true`              | Start daemon on plugin load    |

## Tools

All tools use the `cua-xmpp-` prefix and follow the same conventions
as core CUA tools (Typer-based, `--help` available).

| Tool              | Purpose                                        |
|-------------------|------------------------------------------------|
| `cua-xmpp-send`  | Send a message to the phone user               |
| `cua-xmpp-recv`  | Pop next queued message (or check inbox)        |
| `cua-xmpp-wait`  | Block until a message arrives (with heartbeat)  |
| `cua-xmpp-status`| Check daemon connection state and inbox count   |
| `cua-xmpp-stop`  | Stop the background daemon                     |
| `cua-xmpp-daemon`| Start/restart the daemon (usually automatic)    |

### Examples for the model

The agent can use these in its `run:` commands:

```
run: cua-xmpp-send "Found 3 results, here they are: ..."
run: cua-xmpp-wait --timeout 120
run: cua-xmpp-recv --all
run: cua-xmpp-status --inbox
```

## Markers

The tools emit structured markers that the agent loop can detect in
command output, similar to `@@CUA_TASK_COMPLETE@@`:

| Marker                    | Meaning                          |
|---------------------------|----------------------------------|
| `@@XMPP_MSG_RECEIVED@@`  | A message was received (JSON follows) |
| `@@XMPP_MSG_SENT@@`      | A message was queued for sending |
| `@@XMPP_INBOX_EMPTY@@`   | No messages pending              |
| `@@XMPP_DAEMON_STATUS@@` | Status info follows              |
| `@@XMPP_WAITING@@`       | Heartbeat while waiting          |

These are informational — the current agent loop does not need to
handle them specially.  They're designed for future plugins that
might want to react to incoming messages or daemon state changes.

## Disabling XMPP

Rename or remove either part:

```bash
mv compose.d/xmpp compose.d/.xmpp   # disable server
rm -rf plugins/xmpp                   # disable tools
```

## Files

```
plugins/xmpp/
  plugin.py              Plugin entry point (on_startup, on_shutdown)
  requirements.txt       slixmpp dependency
  xmpp_tools/
    __init__.py
    config.py            Shared configuration (env vars, paths, markers)
  tools/                 CLI tools (added to PATH by plugin)
    cua-xmpp-daemon      Long-running XMPP ↔ filesystem bridge
    cua-xmpp-send        Queue outgoing messages
    cua-xmpp-recv        Read incoming messages
    cua-xmpp-wait        Block-until-message-arrives
    cua-xmpp-status      Daemon health check
    cua-xmpp-stop        Graceful daemon shutdown
```
