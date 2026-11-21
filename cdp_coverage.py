"""JS + CSS code coverage of the browser page via the Chrome DevTools Protocol.

The MCP server is long-lived, so one CoverageRecorder holds a dedicated CDP
websocket for the whole recording. That matters: V8's profiler state belongs
to the CDP session that started it, so it must stay open between calls.

Two quirks of the protocol shape the design:

* Profiler.takePreciseCoverage resets V8's counters, so each snapshot only
  shows code run since the previous one. We union covered byte ranges across
  snapshots so coverage only ever grows.
* Scripts loaded before startPreciseCoverage are only partly visible, so
  start() arms the profiler and then reloads the page.
* After a reset, V8 only reports functions that ran since, so a script's
  top-level range is often missing. Script lengths (and sources, for line
  numbers) come from Debugger.scriptParsed instead.
"""

from __future__ import annotations

import bisect
import json
import threading
import urllib.parse
from dataclasses import dataclass, field

import websocket

from cdp_a11y import _get_page_ws_url, navigate as cdp_navigate

Range = tuple[int, int]


# ── Pure helpers (unit-tested) ────────────────────────────


def merge_ranges(ranges: list[Range]) -> list[Range]:
    """Union (start, end) ranges into a sorted, non-overlapping list."""
    out: list[Range] = []
    for s, e in sorted(r for r in ranges if r[1] > r[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def range_size(ranges: list[Range]) -> int:
    return sum(e - s for s, e in ranges)


def covered_segments(ranges: list[tuple[int, int, int]]) -> list[Range]:
    """Byte segments with count > 0, from V8 (start, end, count) ranges.

    V8 ranges are properly nested; the innermost range covering an offset
    gives its count. One sweep with a stack of enclosing ranges, O(n log n).
    """
    out: list[Range] = []
    stack: list[tuple[int, int]] = []  # (end, count), innermost last
    pos = 0

    def emit(a: int, b: int, count: int) -> None:
        if b > a and count > 0:
            out.append((a, b))

    for s, e, c in sorted(ranges, key=lambda r: (r[0], -r[1])):
        while stack and stack[-1][0] <= s:
            end, cnt = stack.pop()
            emit(pos, end, cnt)
            pos = end
        if stack:
            emit(pos, s, stack[-1][1])
        pos = s
        stack.append((e, c))
    while stack:
        end, cnt = stack.pop()
        emit(pos, end, cnt)
        pos = end
    return merge_ranges(out)


def _is_page_source(url: str) -> bool:
    """Skip eval'd snippets (our own eval_js/snapshot helpers have no URL),
    extensions (the cualm HUD) and browser internals."""
    return urllib.parse.urlsplit(url).scheme in ("http", "https", "file")


@dataclass
class FileCov:
    kind: str                     # "JS" or "CSS"
    url: str
    total: int = 0
    covered: list[Range] = field(default_factory=list)
    # JS only: function start offset -> [name, ever_called]
    functions: dict[int, list] = field(default_factory=dict)
    script_id: str = ""           # latest V8 scriptId, for fetching source
    start_line: int = 0           # inline <script> position in its HTML file

    @property
    def used(self) -> int:
        return min(range_size(self.covered), self.total)

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


class CoverageState:
    """Cumulative coverage across snapshots. No I/O — fed raw CDP results."""

    def __init__(self) -> None:
        self.files: dict[tuple, FileCov] = {}
        self.snapshots = 0

    def add_js(self, entries: list[dict], scripts: dict[str, dict]) -> None:
        """entries: Profiler.takePreciseCoverage result; scripts: scriptId ->
        Debugger.scriptParsed params (url, length, startLine)."""
        for entry in entries:
            url = entry.get("url", "")
            funcs = [f for f in entry.get("functions") or [] if f.get("ranges")]
            if not _is_page_source(url) or not funcs:
                continue
            info = scripts.get(entry.get("scriptId"), {})
            total = info.get("length") or max(
                r["endOffset"] for f in funcs for r in f["ranges"])
            # Key by (url, size): a page reload re-creates scripts with the same
            # URL and size (merge them); several inline <script>s in one HTML
            # file share the URL but usually differ in size (keep them apart).
            key = ("JS", url, total)
            fc = self.files.setdefault(key, FileCov("JS", url, total))
            fc.script_id = entry.get("scriptId", fc.script_id)
            fc.start_line = info.get("startLine", fc.start_line)
            raw = [(r["startOffset"], r["endOffset"], r["count"])
                   for f in funcs for r in f["ranges"]]
            fc.covered = merge_ranges(fc.covered + covered_segments(raw))
            for f in funcs:
                r0 = f["ranges"][0]
                if r0["startOffset"] == 0 and r0["endOffset"] >= total:
                    continue                      # the script's top level
                rec = fc.functions.setdefault(
                    r0["startOffset"], [f.get("functionName") or "(anonymous)", False])
                rec[1] = rec[1] or r0["count"] > 0

    def add_css(self, usage: list[dict], sheets: dict[str, dict]) -> None:
        """usage: CSS.takeCoverageDelta rules; sheets: styleSheetId -> header."""
        for rule in usage:
            hdr = sheets.get(rule.get("styleSheetId"))
            if not hdr:
                continue
            url = hdr.get("sourceURL", "")
            if not _is_page_source(url):
                continue
            total = int(hdr.get("length") or 0)
            key = ("CSS", url, total)
            fc = self.files.setdefault(key, FileCov("CSS", url, total))
            if rule.get("used"):
                fc.covered = merge_ranges(
                    fc.covered + [(int(rule["startOffset"]), int(rule["endOffset"]))])

    def report(self, top_files: int = 15, list_functions: int = 40,
               line_of=None) -> str:
        """line_of(file, offset) -> 1-based line or None, for function lists."""
        if not self.files:
            return ("No page scripts or stylesheets recorded yet. Was the page "
                    "loaded after coverage_start?")
        lines = [f"Coverage (cumulative, {self.snapshots} snapshot(s)):"]
        for kind in ("JS", "CSS"):
            fs = [f for f in self.files.values() if f.kind == kind]
            total = sum(f.total for f in fs)
            used = sum(f.used for f in fs)
            pct = 100.0 * used / total if total else 0.0
            line = (f"  {kind:<4}{pct:5.1f}%  ({used:,}/{total:,} bytes, "
                    f"{len(fs)} file(s))")
            if kind == "JS":
                fn_all = [r for f in fs for r in f.functions.values()]
                called = sum(1 for r in fn_all if r[1])
                if fn_all:
                    line += f", functions called {called}/{len(fn_all)}"
            lines.append(line)

        files = sorted(self.files.values(), key=lambda f: f.pct)
        lines += ["", "Per file (least covered first):"]
        for f in files[:top_files]:
            bar = "█" * int(f.pct / 5) + "░" * (20 - int(f.pct / 5))
            lines.append(f"  {f.pct:5.1f}% {bar} [{f.kind}] {f.url}"
                         f"  ({f.used:,}/{f.total:,} B)")
        if len(files) > top_files:
            lines.append(f"  … {len(files) - top_files} more file(s)")

        uncalled = [(f, off, name) for f in files if f.kind == "JS"
                    for off, (name, hit) in sorted(f.functions.items()) if not hit]
        if uncalled and list_functions > 0:
            lines += ["", f"Functions never called ({len(uncalled)}) — "
                          "UI paths still unexplored:"]
            for f, off, name in uncalled[:list_functions]:
                where = f.url.rsplit("/", 1)[-1] or f.url
                line = line_of(f, off) if line_of else None
                lines.append(f"  {name}  ({where}:{line})" if line
                             else f"  {name}  ({where} @byte {off})")
            if len(uncalled) > list_functions:
                lines.append(f"  … {len(uncalled) - list_functions} more")
        return "\n".join(lines)


# ── CDP session ───────────────────────────────────────────


class CdpError(RuntimeError):
    pass


class _Session:
    """Websocket to the page target with a reader thread, so CDP events
    (styleSheetAdded, …) are consumed continuously and never pile up."""

    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self.sheets: dict[str, dict] = {}
        self.scripts: dict[str, dict] = {}
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if "id" in msg:
                    with self._lock:
                        waiter = self._pending.pop(msg["id"], None)
                    if waiter:
                        waiter[1].append(msg)
                        waiter[0].set()
                elif msg.get("method") == "Debugger.scriptParsed":
                    p = msg["params"]
                    if _is_page_source(p.get("url", "")):
                        self.scripts[p["scriptId"]] = p
                elif msg.get("method") == "CSS.styleSheetAdded":
                    hdr = msg["params"]["header"]
                    self.sheets[hdr["styleSheetId"]] = hdr
        except Exception:
            pass
        finally:
            self.closed = True
            with self._lock:
                for ev, _ in self._pending.values():
                    ev.set()
                self._pending.clear()

    def call(self, method: str, params: dict | None = None,
             timeout: float = 30) -> dict:
        if self.closed:
            raise CdpError("coverage CDP session was closed (tab closed or "
                           "browser restarted) — call coverage_start again")
        ev, box = threading.Event(), []
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            self._pending[mid] = (ev, box)
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params or {}}))
        if not ev.wait(timeout) or not box:
            raise CdpError(f"{method}: no response (session closed or timeout)")
        if "error" in box[0]:
            raise CdpError(f"{method}: {box[0]['error'].get('message')}")
        return box[0].get("result", {})

    def close(self) -> None:
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass


class CoverageRecorder:
    def __init__(self) -> None:
        self.session: _Session | None = None
        self.state = CoverageState()
        self.css_ok = False
        self._line_starts: dict[str, list[int]] = {}

    @property
    def recording(self) -> bool:
        return self.session is not None and not self.session.closed

    def start(self, url: str | None) -> str:
        self.stop_session()
        self.state = CoverageState()
        # Navigate first: a cross-site navigation can swap renderer processes,
        # so arm the profiler only once we're on the target site, then reload.
        if url:
            cdp_navigate(url)
        s = self.session = _Session(_get_page_ws_url())
        s.call("Debugger.enable")             # scriptParsed: lengths + sources
        s.call("Debugger.setSkipAllPauses", {"skip": True})  # never stop on `debugger;`
        s.call("Profiler.enable")
        s.call("Profiler.startPreciseCoverage",
               {"callCount": True, "detailed": True})
        try:
            s.call("DOM.enable")          # CSS domain requires DOM
            s.call("CSS.enable")
            s.call("CSS.startRuleUsageTracking")
            self.css_ok = True
        except CdpError:
            self.css_ok = False
        href = s.call("Runtime.evaluate", {"expression": "location.href",
                                           "returnByValue": True})
        href = href.get("result", {}).get("value") or url
        s.sheets.clear()
        s.scripts.clear()
        final = cdp_navigate(href)        # reload so every script is instrumented
        msg = f"Coverage recording started (JS{' + CSS' if self.css_ok else ''}); reloaded {final}"
        return msg

    def snapshot(self) -> None:
        if not self.recording:
            raise CdpError("coverage is not recording — call coverage_start first")
        s = self.session
        js = s.call("Profiler.takePreciseCoverage").get("result", [])
        self.state.add_js(js, s.scripts)
        if self.css_ok:
            css = s.call("CSS.takeCoverageDelta").get("coverage", [])
            self.state.add_css(css, s.sheets)
        self.state.snapshots += 1

    def line_of(self, f: FileCov, offset: int) -> int | None:
        """1-based line of a byte offset in a script, via Debugger.getScriptSource."""
        starts = self._line_starts.get(f.script_id)
        if starts is None and self.recording:
            try:
                src = self.session.call("Debugger.getScriptSource",
                                        {"scriptId": f.script_id}, timeout=5)
                text = src.get("scriptSource", "")
                starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
            except CdpError:
                starts = []
            self._line_starts[f.script_id] = starts
        if not starts:
            return None
        return f.start_line + bisect.bisect_right(starts, offset)

    def report(self, top_files: int, list_functions: int) -> str:
        return self.state.report(top_files, list_functions, self.line_of)

    def stop_session(self) -> None:
        if self.session:
            if not self.session.closed:
                for m in ("Profiler.stopPreciseCoverage",
                          "CSS.stopRuleUsageTracking", "Debugger.disable"):
                    try:
                        self.session.call(m, timeout=5)
                    except CdpError:
                        pass
            self.session.close()
        self.session = None
