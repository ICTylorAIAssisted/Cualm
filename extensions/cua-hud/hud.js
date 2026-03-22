(() => {
  "use strict";

  // Don't run in iframes, extension pages, or local files (calibration)
  if (window !== window.top) return;
  if (location.protocol === "chrome-extension:") return;
  if (location.protocol === "file:") return;

  const BAR_HEIGHT = 18;
  const UPDATE_INTERVAL = 400; // ms

  // ── Create the HUD bar ──
  const bar = document.createElement("div");
  bar.id = "__cua-hud__";
  Object.assign(bar.style, {
    position: "fixed",
    bottom: "0",
    left: "0",
    right: "0",
    height: BAR_HEIGHT + "px",
    background: "rgba(24, 24, 32, 0.92)",
    color: "#ccc",
    fontFamily: "'Consolas', 'Monaco', 'Courier New', monospace",
    fontSize: "11px",
    lineHeight: BAR_HEIGHT + "px",
    padding: "0 8px",
    zIndex: "2147483647",
    pointerEvents: "none",
    userSelect: "none",
    display: "flex",
    gap: "12px",
    alignItems: "center",
    borderTop: "1px solid rgba(255,255,255,0.08)",
    whiteSpace: "nowrap",
    overflow: "hidden",
  });

  // Segments: each is a span we update independently
  const segments = {};
  const segOrder = ["scroll", "page", "dom", "focus", "url"];

  for (const name of segOrder) {
    const span = document.createElement("span");
    span.dataset.seg = name;
    segments[name] = span;
    bar.appendChild(span);

    // Separator after each except last
    if (name !== segOrder[segOrder.length - 1]) {
      const sep = document.createElement("span");
      sep.textContent = "│";
      sep.style.color = "rgba(255,255,255,0.2)";
      bar.appendChild(sep);
    }
  }

  // Color coding for segment labels
  const LABEL_COLOR = "#888";
  const VALUE_COLOR = "#e0e0e0";
  const HIGHLIGHT_COLOR = "#f0a040";

  function makeLabel(label, value, color) {
    return `<span style="color:${LABEL_COLOR}">${label}</span> <span style="color:${color || VALUE_COLOR}">${value}</span>`;
  }

  // ── DOM change tracking ──
  let lastDomChangeTime = 0;
  let domChangeCount = 0;
  let domObserver = null;

  function startDomObserver() {
    if (domObserver) domObserver.disconnect();
    let debounceTimer = null;
    domObserver = new MutationObserver(() => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        lastDomChangeTime = Date.now();
        domChangeCount++;
      }, 150); // debounce rapid mutations into one event
    });
    domObserver.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      characterData: true,
    });
  }

  // ── Update functions ──
  function getScrollInfo() {
    const docH = Math.max(
      document.body.scrollHeight || 0,
      document.documentElement.scrollHeight || 0
    );
    const viewH = window.innerHeight;
    const scrollY = Math.round(window.scrollY);
    const maxScroll = docH - viewH;
    const pct = maxScroll > 0 ? Math.round((scrollY / maxScroll) * 100) : 0;
    const remaining = Math.max(0, docH - scrollY - viewH);

    if (docH <= viewH * 1.1) {
      return makeLabel("scroll:", "no scroll needed");
    }
    return makeLabel("scroll:", `${pct}%`, HIGHLIGHT_COLOR) +
      makeLabel(" ↓", `${remaining}px left`);
  }

  function getPageInfo() {
    const docH = Math.max(
      document.body.scrollHeight || 0,
      document.documentElement.scrollHeight || 0
    );
    const viewH = window.innerHeight;
    const pages = (docH / viewH).toFixed(1);
    return makeLabel("page:", `${docH}px (${pages} screens)`);
  }

  function getDomInfo() {
    if (lastDomChangeTime === 0) {
      return makeLabel("dom:", "idle");
    }
    const ago = Date.now() - lastDomChangeTime;
    let agoStr;
    if (ago < 1000) {
      agoStr = "just now";
    } else if (ago < 60000) {
      agoStr = `${Math.round(ago / 1000)}s ago`;
    } else {
      agoStr = `${Math.round(ago / 60000)}m ago`;
    }
    // Highlight in green if changed recently (< 3s), orange otherwise
    const color = ago < 3000 ? "#4ade80" : VALUE_COLOR;
    return makeLabel("dom:", `changed ${agoStr}`, color);
  }

  function getFocusInfo() {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) {
      return makeLabel("focus:", "none");
    }

    let desc = el.tagName.toLowerCase();

    // Add type for inputs
    if (el.type && el.type !== "text") {
      desc += `[${el.type}]`;
    }

    // Add identifying attribute
    if (el.id) {
      desc += `#${el.id}`;
    } else if (el.name) {
      desc += `[name=${el.name}]`;
    } else if (el.className && typeof el.className === "string") {
      const cls = el.className.split(/\s+/)[0];
      if (cls) desc += `.${cls}`;
    }

    // Add placeholder or value hint for inputs
    if (el.placeholder) {
      desc += ` "${el.placeholder}"`;
    } else if (el.value && el.value.length > 0 && el.value.length < 30) {
      desc += ` val="${el.value}"`;
    }

    // Truncate
    if (desc.length > 40) desc = desc.substring(0, 37) + "...";

    return makeLabel("focus:", desc, HIGHLIGHT_COLOR);
  }

  function getUrlInfo() {
    let url = location.pathname + location.search;
    if (url.length > 50) url = url.substring(0, 47) + "...";
    return makeLabel("url:", url);
  }

  function update() {
    segments.scroll.innerHTML = getScrollInfo();
    segments.page.innerHTML = getPageInfo();
    segments.dom.innerHTML = getDomInfo();
    segments.focus.innerHTML = getFocusInfo();
    segments.url.innerHTML = getUrlInfo();
  }

  // ── Mount and start ──
  function mount() {
    if (!document.body) return;
    document.documentElement.appendChild(bar);

    // Push page content up so the bar doesn't overlap the bottom
    document.body.style.marginBottom =
      (parseInt(getComputedStyle(document.body).marginBottom) || 0) + BAR_HEIGHT + "px";

    update();
    startDomObserver();
    setInterval(update, UPDATE_INTERVAL);

    // Also update on scroll and focus changes
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("focusin", update, { passive: true });
    window.addEventListener("focusout", update, { passive: true });
  }

  if (document.body) {
    mount();
  } else {
    document.addEventListener("DOMContentLoaded", mount);
  }
})();
