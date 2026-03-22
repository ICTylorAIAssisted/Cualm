"""Extract and format a truncated accessibility tree via CDP.

Connects to Chromium's CDP endpoint, fetches the full accessibility tree,
then formats it as indented text with smart truncation:
  - Large child lists are truncated with "... (showing 5/42 children)"
  - Total output is capped at a token budget
  - Uninteresting nodes (generic containers, empty text) are skipped
"""

import json
import urllib.request

import websocket

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

# ── Tunables ──
MAX_CHILDREN_SHOWN = 8       # per parent, before truncation
MAX_TOTAL_LINES = 150        # hard cap on output lines
MAX_DEPTH = 8                # don't recurse deeper than this
INDENT = "  "

# Roles that are usually just layout noise
SKIP_ROLES = frozenset({
    "generic", "none", "presentation", "InlineTextBox",
    "LineBreak", "ignored",
})

# Roles worth showing even without a name
INTERESTING_ROLES = frozenset({
    "link", "button", "textbox", "combobox", "checkbox", "radio",
    "menuitem", "tab", "heading", "img", "table", "row", "cell",
    "columnheader", "rowheader", "listitem", "option", "switch",
    "searchbox", "slider", "spinbutton", "treeitem",
})


def _get_page_ws_url() -> str:
    """Get the websocket URL for the first real page tab."""
    url = f"http://{CDP_HOST}:{CDP_PORT}/json"
    with urllib.request.urlopen(url, timeout=3) as resp:
        tabs = json.loads(resp.read())
    for tab in tabs:
        if tab.get("type") == "page" and not tab.get("url", "").startswith(
            ("chrome-extension://", "devtools://", "chrome://")
        ):
            ws_url = tab.get("webSocketDebuggerUrl")
            if ws_url:
                return ws_url
    for tab in tabs:
        if tab.get("type") == "page":
            ws_url = tab.get("webSocketDebuggerUrl")
            if ws_url:
                return ws_url
    raise RuntimeError("No browser tab found for a11y tree")


def _cdp_call(ws, method: str, params: dict = None, msg_id: int = 1) -> dict:
    """Send a CDP command and wait for the response."""
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json.dumps(msg))
    while True:
        raw = ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == msg_id:
            return resp


def fetch_a11y_tree() -> list[dict]:
    """Fetch the full accessibility tree from CDP."""
    ws_url = _get_page_ws_url()
    ws = websocket.create_connection(ws_url, timeout=10)
    try:
        # Enable the Accessibility domain first
        _cdp_call(ws, "Accessibility.enable", msg_id=1)
        resp = _cdp_call(ws, "Accessibility.getFullAXTree", msg_id=2)
        if "error" in resp:
            import sys
            print(f"   [a11y] CDP error: {resp['error']}", file=sys.stderr)
            return []
        nodes = resp.get("result", {}).get("nodes", [])
        return nodes
    finally:
        ws.close()


def _build_tree(nodes: list[dict]) -> dict:
    """Convert flat CDP node list into a nested tree structure."""
    by_id = {}
    for node in nodes:
        nid = node.get("nodeId", "")
        by_id[nid] = {
            "role": node.get("role", {}).get("value", ""),
            "name": node.get("name", {}).get("value", ""),
            "value": node.get("value", {}).get("value", ""),
            "properties": {
                p["name"]: p.get("value", {}).get("value", "")
                for p in node.get("properties", [])
            },
            "children": [],
            # childIds is a flat list of string IDs in CDP
            "child_ids": node.get("childIds", []),
            "_id": nid,
        }

    # Link children
    for nid, info in by_id.items():
        for cid in info["child_ids"]:
            if cid in by_id:
                info["children"].append(by_id[cid])

    # Find root (first node, or node with role "RootWebArea")
    if not nodes:
        return {}
    root_id = nodes[0].get("nodeId", "")
    return by_id.get(root_id, {})


def _should_show(node: dict) -> bool:
    """Decide if a node is worth including in the output."""
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    if role in SKIP_ROLES:
        return False
    if role in INTERESTING_ROLES:
        return True
    # Show if it has a meaningful name or value
    if name and name.strip():
        return True
    if value and value.strip():
        return True
    # Show if it has interesting children (will be determined by recursion)
    return bool(node.get("children"))


def _format_node(node: dict) -> str:
    """Format a single node as a compact string."""
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    props = node.get("properties", {})

    parts = [role]

    if name:
        # Truncate long names (e.g. paragraph text)
        display_name = name if len(name) <= 60 else name[:57] + "..."
        parts.append(f"'{display_name}'")

    if value:
        display_val = value if len(value) <= 40 else value[:37] + "..."
        parts.append(f"value='{display_val}'")

    # Add key properties
    if props.get("focused") == True or props.get("focused") == "true":
        parts.append("[focused]")
    if props.get("checked") == "true":
        parts.append("[checked]")
    if props.get("selected") == "true":
        parts.append("[selected]")
    if props.get("disabled") == "true":
        parts.append("[disabled]")
    if props.get("expanded") == "true":
        parts.append("[expanded]")
    if props.get("expanded") == "false":
        parts.append("[collapsed]")
    if props.get("required") == "true":
        parts.append("[required]")

    return " ".join(parts)


def _render_tree(node: dict, depth: int, lines: list[str],
                 max_children: int, max_depth: int, max_lines: int) -> None:
    """Recursively render the tree with truncation."""
    if len(lines) >= max_lines:
        return
    if depth > max_depth:
        return

    if not _should_show(node):
        # Skip this node but still recurse into children
        for child in node.get("children", []):
            _render_tree(child, depth, lines,
                        max_children, max_depth, max_lines)
        return

    prefix = INDENT * depth
    formatted = _format_node(node)
    lines.append(f"{prefix}{formatted}")

    children = node.get("children", [])
    # Filter to interesting children
    visible_children = [c for c in children if _should_show(c)]
    # But also include children that have interesting descendants
    if len(visible_children) < len(children):
        for c in children:
            if c not in visible_children and c.get("children"):
                visible_children.append(c)

    total = len(visible_children)
    show = min(total, max_children)

    for i, child in enumerate(visible_children[:show]):
        if len(lines) >= max_lines:
            remaining = total - i
            lines.append(f"{prefix}{INDENT}... ({remaining} more children not shown, {total} total)")
            return
        _render_tree(child, depth + 1, lines,
                    max_children, max_depth, max_lines)

    if total > show:
        lines.append(f"{prefix}{INDENT}... (showing {show}/{total} children)")


def get_a11y_text(max_children: int = MAX_CHILDREN_SHOWN,
                  max_lines: int = MAX_TOTAL_LINES,
                  max_depth: int = MAX_DEPTH) -> str:
    """Fetch and format the accessibility tree as truncated text.

    Returns empty string on any failure (CDP not available, etc.).
    """
    try:
        nodes = fetch_a11y_tree()
        if not nodes:
            return ""
        root = _build_tree(nodes)
        if not root:
            return ""
        lines: list[str] = []
        _render_tree(root, 0, lines, max_children, max_depth, max_lines)
        if not lines:
            return ""

        # Add header
        result = "Accessibility tree:\n" + "\n".join(lines)

        if len(lines) >= max_lines:
            result += f"\n... (truncated at {max_lines} lines)"

        return result
    except Exception as e:
        # CDP not available, page not loaded, etc. — log but don't crash
        import sys
        print(f"   [a11y] {type(e).__name__}: {e}", file=sys.stderr)
        return ""
