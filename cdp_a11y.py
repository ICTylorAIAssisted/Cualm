"""Extract and format a truncated accessibility tree via CDP.

Connects to Chromium's CDP endpoint, fetches the full accessibility tree,
then formats it as indented text with smart truncation:
  - Large child lists are truncated with "... (showing 5/42 children)"
  - Total output is capped at a token budget
  - Uninteresting nodes (generic containers, empty text) are skipped
"""

import json
import os
import urllib.request

import websocket

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

# ── Tunables ──
MAX_CHILDREN_SHOWN = 15       # per parent, before truncation
MAX_TOTAL_LINES = 250        # hard cap on output lines
MAX_DEPTH = 10               # don't recurse deeper than this
INDENT = "  "

# Show ref badges on page elements (visible in screenshots + VNC)
# Set CUA_SHOW_REFS=0 to disable
SHOW_REFS = os.environ.get("CUA_SHOW_REFS", "1") != "0"

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

# Roles that get a ref ID for element-based interaction
INTERACTIVE_ROLES = frozenset({
    "link", "button", "textbox", "combobox", "checkbox", "radio",
    "menuitem", "tab", "option", "switch", "searchbox", "slider",
    "spinbutton", "treeitem",
})

# Ref registry saved here for cua-pw to read
REF_FILE = "/tmp/cua_a11y_refs.json"


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
            "child_ids": node.get("childIds", []),
            "_id": nid,
            "_backend_dom_node_id": node.get("backendDOMNodeId"),
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
    name = str(node.get("name", ""))
    value = str(node.get("value", ""))

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


def _assign_all_refs(node: dict, ref_state: dict) -> None:
    """Walk the FULL tree (no truncation) and assign ref IDs to all
    interactive elements. This ensures every interactive element gets
    a ref and a visual badge, even if it's truncated from the text output."""
    role = node.get("role", "")
    if role in INTERACTIVE_ROLES:
        backend_id = node.get("_backend_dom_node_id")
        if backend_id is not None:
            ref_id = ref_state["counter"]
            ref_state["counter"] += 1
            ref_key = f"e{ref_id}"
            node["_ref"] = ref_key
            ref_state["refs"][ref_key] = {
                "backendDOMNodeId": backend_id,
                "role": role,
                "name": str(node.get("name", ""))[:60],
            }
    for child in node.get("children", []):
        _assign_all_refs(child, ref_state)


def _format_node(node: dict) -> str:
    """Format a single node as a compact string.

    If the node was assigned a ref by _assign_all_refs, includes it.
    """
    role = node.get("role", "")
    name = str(node.get("name", ""))
    value = str(node.get("value", ""))
    props = node.get("properties", {})

    parts = [role]

    if name:
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

    # Include pre-assigned ref
    ref_key = node.get("_ref")
    if ref_key:
        parts.append(f"[ref={ref_key}]")

    return " ".join(parts)


def _render_tree(node: dict, depth: int, lines: list[str],
                 max_children: int, max_depth: int, max_lines: int) -> None:
    """Recursively render the tree with truncation."""
    if len(lines) >= max_lines:
        return
    if depth > max_depth:
        return

    if not _should_show(node):
        for child in node.get("children", []):
            _render_tree(child, depth, lines,
                        max_children, max_depth, max_lines)
        return

    prefix = INDENT * depth
    formatted = _format_node(node)
    lines.append(f"{prefix}{formatted}")

    children = node.get("children", [])
    visible_children = [c for c in children if _should_show(c)]
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


def _inject_ref_overlays(refs: dict) -> None:
    """Inject floating ref badges positioned at each element's upper-right corner.

    Uses a single overlay container with absolutely positioned badges,
    calculated via getBoundingClientRect(). This avoids modifying the
    DOM structure of target elements (which breaks layouts).
    """
    if not refs:
        return
    try:
        ws_url = _get_page_ws_url()
        ws = websocket.create_connection(ws_url, timeout=10)
    except Exception:
        return

    msg_id = 1

    def _call(method, params=None):
        nonlocal msg_id
        msg_id += 1
        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        ws.send(json.dumps(msg))
        while True:
            raw = ws.recv()
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                return resp

    # Collect ref→backendDOMNodeId, resolve each to objectId,
    # then use a single JS call to position all badges at once.
    ref_object_ids = {}  # ref_key → objectId
    try:
        for ref_key, info in refs.items():
            backend_id = info.get("backendDOMNodeId")
            if backend_id is None:
                continue
            try:
                resp = _call("DOM.resolveNode", {
                    "backendNodeId": backend_id,
                })
                object_id = resp.get("result", {}).get("object", {}).get("objectId")
                if object_id:
                    # Tag the element so we can find it in JS
                    _call("Runtime.callFunctionOn", {
                        "objectId": object_id,
                        "functionDeclaration": f"""function() {{
                            this.setAttribute('data-cua-ref', '{ref_key}');
                        }}""",
                    })
                    ref_object_ids[ref_key] = object_id
            except Exception:
                continue

        # Build the ref list for the JS overlay
        ref_keys_json = json.dumps(list(ref_object_ids.keys()))

        # Single JS call: create overlay container with badges for visible elements only
        _call("Runtime.evaluate", {"expression": f"""
            (function() {{
                // Remove old overlay
                var old = document.getElementById('cua-ref-overlay');
                if (old) old.remove();

                var refs = {ref_keys_json};
                if (!refs.length) return;

                var container = document.createElement('div');
                container.id = 'cua-ref-overlay';
                container.setAttribute('aria-hidden', 'true');
                container.setAttribute('role', 'presentation');
                container.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:999999;pointer-events:none;';

                var vw = window.innerWidth;
                var vh = window.innerHeight;

                refs.forEach(function(ref) {{
                    var el = document.querySelector('[data-cua-ref="' + ref + '"]');
                    if (!el) return;
                    var rect = el.getBoundingClientRect();
                    if (rect.width === 0 && rect.height === 0) return;

                    // Skip elements outside the viewport
                    if (rect.bottom < 0 || rect.top > vh ||
                        rect.right < 0 || rect.left > vw) return;

                    // Skip hidden elements
                    var style = getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.opacity === '0' ||
                        style.display === 'none') return;

                    // Skip elements occluded by higher z-index content
                    // (modals, overlays, dropdowns on top). Check if
                    // the element at its center point is itself or a child.
                    var cx = rect.left + rect.width / 2;
                    var cy = rect.top + rect.height / 2;
                    // Clamp to viewport
                    cx = Math.max(0, Math.min(cx, vw - 1));
                    cy = Math.max(0, Math.min(cy, vh - 1));
                    var topEl = document.elementFromPoint(cx, cy);
                    if (topEl && topEl !== el && !el.contains(topEl) && !topEl.closest('[data-cua-ref="' + ref + '"]')) return;

                    var badge = document.createElement('div');
                    badge.textContent = ref;
                    badge.style.cssText = 'position:fixed;'
                        + 'top:' + Math.max(0, rect.top) + 'px;'
                        + 'left:' + Math.max(0, rect.right - 20) + 'px;'
                        + 'background:rgba(220,50,50,0.85);'
                        + 'color:white;font:bold 9px/12px monospace;'
                        + 'padding:0 3px;border-radius:2px;'
                        + 'pointer-events:none;white-space:nowrap;'
                        + 'box-shadow:0 1px 2px rgba(0,0,0,0.3);';
                    container.appendChild(badge);
                }});

                document.body.appendChild(container);
            }})();
        """})

    except Exception:
        pass
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _cleanup_ref_overlays() -> None:
    """Remove old ref overlay and data-cua-ref attributes from the page.

    Called before fetching the a11y tree so stale badges don't pollute it.
    """
    try:
        ws_url = _get_page_ws_url()
        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            msg = {"id": 1, "method": "Runtime.evaluate", "params": {"expression": """
                var old = document.getElementById('cua-ref-overlay');
                if (old) old.remove();
                document.querySelectorAll('[data-cua-ref]').forEach(
                    function(el) { el.removeAttribute('data-cua-ref'); }
                );
            """}}
            ws.send(json.dumps(msg))
            ws.recv()
        finally:
            ws.close()
    except Exception:
        pass  # best-effort cleanup


def get_a11y_text(max_children: int = MAX_CHILDREN_SHOWN,
                  max_lines: int = MAX_TOTAL_LINES,
                  max_depth: int = MAX_DEPTH) -> str:
    """Fetch and format the accessibility tree as truncated text.

    Two passes:
      1. Walk the FULL tree to assign ref IDs to ALL interactive elements
         (so every button/link/input gets a badge, even if truncated).
      2. Render a truncated text version for the prompt.

    The ref→element mapping is saved to REF_FILE for cua-pw.

    Returns empty string on any failure (CDP not available, etc.).
    """
    try:
        # Clean up old ref overlays BEFORE fetching the tree, so stale
        # badges from the previous step don't appear as a11y nodes.
        _cleanup_ref_overlays()

        nodes = fetch_a11y_tree()
        if not nodes:
            return ""
        root = _build_tree(nodes)
        if not root:
            return ""

        # Pass 1: assign refs to ALL interactive elements (full tree)
        ref_state = {"counter": 1, "refs": {}}
        _assign_all_refs(root, ref_state)

        # Pass 2: render truncated text (refs already on nodes)
        lines: list[str] = []
        _render_tree(root, 0, lines, max_children, max_depth, max_lines)
        if not lines:
            return ""

        # Save ref registry for cua-pw
        try:
            with open(REF_FILE, "w") as f:
                json.dump(ref_state["refs"], f)
        except Exception:
            pass

        # Inject visual ref badges on the page
        if SHOW_REFS and ref_state["refs"]:
            _inject_ref_overlays(ref_state["refs"])

        result = "Accessibility tree:\n" + "\n".join(lines)

        if len(lines) >= max_lines:
            result += f"\n... (truncated at {max_lines} lines)"

        return result
    except Exception as e:
        import sys
        print(f"   [a11y] {type(e).__name__}: {e}", file=sys.stderr)
        return ""
