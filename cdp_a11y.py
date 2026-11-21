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


def navigate(url: str, wait_sec: float = 10.0) -> str:
    """Navigate the active tab to *url* via CDP and wait for load.

    Works for any scheme (http, file, about:) regardless of the current
    page, unlike setting location.href from page JS.  Returns the final
    URL, or raises RuntimeError on navigation errors.
    """
    import time
    ws_url = _get_page_ws_url()
    ws = websocket.create_connection(ws_url, timeout=wait_sec + 5)
    try:
        resp = _cdp_call(ws, "Page.navigate", {"url": url}, msg_id=1)
        err = resp.get("error") or resp.get("result", {}).get("errorText")
        if err:
            raise RuntimeError(f"navigation to {url} failed: {err}")
        time.sleep(0.3)  # let the old document unload before polling
        deadline = time.monotonic() + wait_sec
        msg_id = 2
        while time.monotonic() < deadline:
            r = _cdp_call(ws, "Runtime.evaluate",
                          {"expression": "document.readyState + ' ' + location.href",
                           "returnByValue": True}, msg_id=msg_id)
            msg_id += 1
            val = r.get("result", {}).get("result", {}).get("value", "")
            state, _, href = val.partition(" ")
            if state == "complete":
                return href
            time.sleep(0.2)
        return url
    finally:
        ws.close()


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


# Most badges drawn at once. Refs are not capped (every interactive element
# stays usable by ref); this only limits on-screen clutter. 0 = no cap.
MAX_BADGES = int(os.environ.get("CUA_MAX_BADGES", "300"))

# Badge renderer, installed in the page once per snapshot/screenshot.
# Called as (function(refs, maxBadges) {...})(...). Design:
#  * Element references are collected once, so a redraw never re-queries
#    the DOM per ref; an IntersectionObserver keeps the set of elements
#    near the viewport, so redraws and position checks only touch those.
#  * Scroll/resize (browser zoom fires resize): hide at once, redraw
#    150 ms after the movement stops.
#  * Page DOM changes (layout shifts, content appearing/disappearing):
#    redraw, throttled to one per 250 ms. cualm's own changes are ignored.
#  * Every 400 ms, re-read positions of near-viewport elements and redraw
#    if any moved (CSS animations/transitions change no DOM). Paused while
#    the tab is hidden.
#  * At most maxBadges badges; beyond that, and for badges that would
#    overlap one already drawn, a corner note says how many were skipped.
# Only refs from the last snapshot are drawn; new elements get refs then.
_BADGE_RENDERER_JS = r"""
(function(refs, maxBadges) {
    if (window.__cuaRefsOff) window.__cuaRefsOff();
    var items = [];
    refs.forEach(function(ref) {
        var el = document.querySelector('[data-cua-ref="' + ref + '"]');
        if (el) items.push({ref: ref, el: el, near: false});
    });
    if (!items.length) return;

    var byEl = new Map(items.map(function(it) { return [it.el, it]; }));
    var io = new IntersectionObserver(function(entries) {
        entries.forEach(function(e) { byEl.get(e.target).near = e.isIntersecting; });
        schedule(0);
    }, {rootMargin: '200px'});
    items.forEach(function(it) { io.observe(it.el); });

    var lastSig = '';
    function clear() {
        var old = document.getElementById('cua-ref-overlay');
        if (old) old.remove();
        lastSig = '';
    }

    function visible() {
        // [item, rect] for near-viewport, connected, non-empty elements
        var out = [];
        items.forEach(function(it) {
            if (!it.near || !it.el.isConnected) return;
            var r = it.el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) return;
            if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return;
            out.push([it, r]);
        });
        return out;
    }

    function signature(vis) {
        return vis.map(function(v) {
            return v[0].ref + ':' + Math.round(v[1].left) + ',' + Math.round(v[1].top);
        }).join(';');
    }

    function draw() {
        clear();
        var vis = visible();
        var container = document.createElement('div');
        container.id = 'cua-ref-overlay';
        container.setAttribute('aria-hidden', 'true');
        container.setAttribute('role', 'presentation');
        container.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:999999;pointer-events:none;';
        var placed = [], skipped = 0, vw = innerWidth, vh = innerHeight;

        vis.forEach(function(v) {
            var it = v[0], rect = v[1], el = it.el;
            var style = getComputedStyle(el);
            if (style.visibility === 'hidden' || style.opacity === '0' ||
                style.display === 'none') return;
            // Skip elements covered by other content (modals, dropdowns)
            var cx = Math.max(0, Math.min(rect.left + rect.width / 2, vw - 1));
            var cy = Math.max(0, Math.min(rect.top + rect.height / 2, vh - 1));
            var topEl = document.elementFromPoint(cx, cy);
            if (topEl && topEl !== el && !el.contains(topEl)) return;

            if (maxBadges > 0 && placed.length >= maxBadges) { skipped++; return; }
            var bx = Math.max(0, rect.right - 20), by = Math.max(0, rect.top);
            var bw = 7 + 6 * it.ref.length, bh = 12;
            // Thin out badges that would sit on top of one already drawn
            var clash = placed.some(function(p) {
                var ox = Math.min(bx + bw, p[0] + p[2]) - Math.max(bx, p[0]);
                var oy = Math.min(by + bh, p[1] + p[3]) - Math.max(by, p[1]);
                return ox > bw / 2 && oy > bh / 2;
            });
            if (clash) { skipped++; return; }
            placed.push([bx, by, bw, bh]);

            var badge = document.createElement('div');
            badge.textContent = it.ref;
            badge.style.cssText = 'position:fixed;top:' + by + 'px;left:' + bx + 'px;'
                + 'background:rgba(220,50,50,0.85);'
                + 'color:white;font:bold 9px/12px monospace;'
                + 'padding:0 3px;border-radius:2px;'
                + 'pointer-events:none;white-space:nowrap;'
                + 'box-shadow:0 1px 2px rgba(0,0,0,0.3);';
            container.appendChild(badge);
        });

        if (skipped) {
            var note = document.createElement('div');
            note.textContent = '+' + skipped + ' refs not drawn (cap/overlap) - see snapshot';
            note.style.cssText = 'position:fixed;top:0;right:0;'
                + 'background:rgba(120,20,20,0.9);color:white;'
                + 'font:bold 10px/14px monospace;padding:1px 5px;'
                + 'pointer-events:none;white-space:nowrap;';
            container.appendChild(note);
        }
        document.body.appendChild(container);
        lastSig = signature(vis);
    }

    // Throttled redraw: at most one pending at a time.
    var timer = null;
    function schedule(delay) {
        if (timer !== null) return;
        timer = setTimeout(function() { timer = null; draw(); }, delay);
    }
    // Scroll/resize: hide now, redraw once movement stops (debounced).
    function onMove() {
        clear();
        clearTimeout(timer);
        timer = setTimeout(function() { timer = null; draw(); }, 150);
    }

    var isOurs = function(n) {
        return n && n.nodeType === 1 && (n.id === 'cua-ref-overlay' ||
            (n.closest && n.closest('#cua-ref-overlay')) || n.id === '__cua-hud__');
    };
    var mo = new MutationObserver(function(records) {
        var page = records.some(function(m) {
            if (m.type === 'attributes')
                return !isOurs(m.target) && m.attributeName !== 'data-cua-ref' &&
                       m.attributeName !== 'data-cua-active';
            if (m.type === 'childList') {
                if (isOurs(m.target)) return false;
                var nodes = Array.prototype.slice.call(m.addedNodes)
                    .concat(Array.prototype.slice.call(m.removedNodes));
                return !(nodes.length && nodes.every(isOurs));
            }
            return !isOurs(m.target && m.target.parentElement);
        });
        if (page) schedule(250);
    });
    mo.observe(document.body, {childList: true, subtree: true,
                               attributes: true, characterData: true});

    // Elements moving on their own (CSS animations/transitions)
    var poll = setInterval(function() {
        if (document.hidden || timer !== null) return;
        if (signature(visible()) !== lastSig) draw();
    }, 400);

    addEventListener('scroll', onMove, true);
    addEventListener('resize', onMove);
    window.__cuaRefsOff = function() {
        clearTimeout(timer); timer = null;
        clearInterval(poll);
        io.disconnect(); mo.disconnect();
        removeEventListener('scroll', onMove, true);
        removeEventListener('resize', onMove);
        clear();
        window.__cuaRefsOff = null;
    };
    // First draw happens once the IntersectionObserver reports (schedule(0)).
})
"""


def _cdp_pipeline(ws, calls: list[tuple[str, dict]], first_id: int) -> list[dict]:
    """Send many CDP commands at once, then collect all responses (in order).
    One round trip of latency instead of one per command."""
    for i, (method, params) in enumerate(calls):
        ws.send(json.dumps({"id": first_id + i, "method": method, "params": params}))
    pending = {first_id + i for i in range(len(calls))}
    results: dict[int, dict] = {}
    while pending:
        msg = json.loads(ws.recv())
        mid = msg.get("id")
        if mid in pending:
            pending.discard(mid)
            results[mid] = msg
    return [results[first_id + i] for i in range(len(calls))]


def _inject_ref_overlays(refs: dict) -> None:
    """Tag every ref'd element with data-cua-ref (cua-pw and the badges find
    elements by it) and install the badge renderer.

    Tagging is batched: all DOM.resolveNode calls are pipelined, then one
    Runtime.callFunctionOn per chunk tags the elements. (One-by-one tagging
    cost ~2 ms per element: 7 s per snapshot on a 3,000-button page.)
    """
    if not refs:
        return
    try:
        ws = websocket.create_connection(_get_page_ws_url(), timeout=30)
    except Exception:
        return
    group = "cua-ref-tagging"
    try:
        keys = [k for k, info in refs.items()
                if info.get("backendDOMNodeId") is not None]
        resolved = _cdp_pipeline(ws, [
            ("DOM.resolveNode", {"backendNodeId": refs[k]["backendDOMNodeId"],
                                 "objectGroup": group})
            for k in keys], first_id=1)
        tagged = []        # (ref, objectId)
        for k, resp in zip(keys, resolved):
            oid = resp.get("result", {}).get("object", {}).get("objectId")
            if oid:
                tagged.append((k, oid))

        next_id = len(keys) + 1
        CHUNK = 500        # keep each call's argument list modest
        calls = []
        for i in range(0, len(tagged), CHUNK):
            chunk = tagged[i:i + CHUNK]
            calls.append(("Runtime.callFunctionOn", {
                "objectId": chunk[0][1],
                "functionDeclaration": """function(keys) {
                    for (var i = 0; i < keys.length; i++)
                        arguments[i + 1].setAttribute('data-cua-ref', keys[i]);
                }""",
                "arguments": [{"value": [k for k, _ in chunk]}]
                             + [{"objectId": oid} for _, oid in chunk],
            }))
        _cdp_pipeline(ws, calls, first_id=next_id)
        next_id += len(calls)

        _cdp_pipeline(ws, [
            ("Runtime.releaseObjectGroup", {"objectGroup": group}),
            ("Runtime.evaluate", {"expression":
                f"{_BADGE_RENDERER_JS}({json.dumps([k for k, _ in tagged])}, {MAX_BADGES})"}),
        ], first_id=next_id)
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
                if (window.__cuaRefsOff) window.__cuaRefsOff();
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


# Reads the cua-hud status bar (a content script; the DOM is shared, so its
# text is visible from here) and adds the full URL, which the bar shortens
# to the path. Returns "" when the bar is absent (file: pages, iframes).
_PAGE_STATE_JS = r"""
(function() {
    var bar = document.getElementById('__cua-hud__');
    if (!bar) return '';
    var parts = [];
    bar.querySelectorAll('[data-seg]').forEach(function(seg) {
        var name = seg.dataset.seg;
        if (name === 'url') return;
        var t = seg.textContent.replace(/\s+/g, ' ').trim();
        if (name === 'dom') {
            // Fresh value instead of the bar's text, which lags ~0.5s.
            var last = Number(bar.dataset.lastChange || 0);
            if (!last) t = 'dom: idle';
            else {
                var ago = Date.now() - last;
                t = 'dom: changed ' + (ago < 1000 ? 'just now'
                    : ago < 60000 ? Math.round(ago / 1000) + 's ago'
                    : Math.round(ago / 60000) + 'm ago');
            }
        }
        if (t) parts.push(t);
    });
    var url = location.href;
    if (url.length > 120) url = url.slice(0, 117) + '...';
    parts.push('url: ' + url);
    return parts.join(' | ');
})()
"""


def get_page_state() -> str:
    """One line of page state from the HUD: scroll position, page height,
    time since the DOM last changed, focused element, URL. "" if unavailable."""
    try:
        ws = websocket.create_connection(_get_page_ws_url(), timeout=5)
        try:
            resp = _cdp_call(ws, "Runtime.evaluate",
                             {"expression": _PAGE_STATE_JS,
                              "returnByValue": True})
        finally:
            ws.close()
        return resp.get("result", {}).get("result", {}).get("value") or ""
    except Exception:
        return ""


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
        # Read before our own overlay cleanup touches the DOM.
        state = get_page_state()

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
        if state:
            result = f"Page state: {state}\n{result}"

        if len(lines) >= max_lines:
            result += f"\n... (truncated at {max_lines} lines)"

        return result
    except Exception as e:
        import sys
        print(f"   [a11y] {type(e).__name__}: {e}", file=sys.stderr)
        return ""
