---
name: cualm
description: Use a sandboxed Linux desktop with Chromium (cualm MCP server) to look at and operate web UIs or GUI apps — verifying a frontend change in a real browser, reproducing a UI bug, filling forms, or extracting data from a page. Use when the task needs a real browser or screen rather than reading code.
---

# cualm — sandboxed desktop for GUI work

cualm runs a disposable Linux desktop (Xvfb + Chromium) in a container and
exposes it through the `cualm` MCP server. Nothing touches the user's own
desktop or browser profile.

## Before you start

- If `cualm` MCP tools are missing or fail to connect, the sandbox is not
  running. Start it with `./cualm up` from the cualm checkout (or ask the
  user where it lives), then check `./cualm status`.
- The sandbox cannot see the host's `localhost`. To open a dev server
  running on the host, use `http://host.docker.internal:<port>`, and make
  sure the dev server listens on `0.0.0.0`, not only `127.0.0.1`.
- The user can watch live over VNC at `localhost:5900`.

## Keep screenshots out of the main context

Screenshots are expensive. If you can spawn a subagent, run multi-step GUI
work in one and have it return a short text report (what it did, what it saw,
any values extracted, pass/fail). Only bring a screenshot back when the user
needs to see it.

## The observe → act loop

1. `navigate(url)`.
2. Observe:
   - `snapshot()` returns the accessibility tree as text, with `[ref=eN]` on
     interactive elements. It is cheap; prefer it.
     Its first line, `Page state: …`, comes from cualm, not the page:
     - scroll position and how many pixels are left below
     - page height in screens
     - `dom: changed just now / Ns ago / idle`, which shows whether your last
       action changed anything (cualm's own badges don't count)
     - the focused element
     - the full URL
   - `screenshot()` returns an image with the same refs drawn as red badges.
     Use it for layout and visual questions or when the tree is not enough.
     The badges follow the page as it scrolls, zooms or moves, after a short
     delay, but only for refs from the last snapshot. At most 300 badges are
     drawn (`CUA_MAX_BADGES`), and badges that would overlap are skipped. A
     red note in the top-right corner says how many refs weren't drawn. Those
     refs still work; find them in `snapshot()`.
3. Act, preferring refs:
   - `click_element("e15")`, `fill("e12", "text")`, `select_option("e9", "Label")`
   - Playwright selectors work too: `click_element("text=Sign in")`
   - Pixel fallbacks: `click(x, y)`, `type_text`, `press_key("ctrl+a")`,
     `scroll`, `drag`. Coordinates are screen pixels as seen in the screenshot.
4. Observe again to confirm the effect. Refs are renumbered on every
   snapshot or screenshot, so never reuse old refs.

Use `eval_js("...")` to read data directly, such as table rows, input values or
`document.title`. It is faster and more exact than reading pixels. Avoid
top-level `const`/`let`.

`shell(cmd)` runs inside the sandbox container, not on the host.

## Measuring code coverage

To find out how much of a page's code your exploration exercised:

1. `coverage_start(url)` opens the page, arms V8's profiler and reloads, so
   every script is counted from its first line. It discards any earlier
   recording.
2. Explore with the normal tools.
3. `coverage_report()` shows cumulative JS and CSS byte coverage, functions
   called out of the total, and the never-called functions with `file:line`.
   Use that list to find UI paths you haven't tried yet. `stop=True` ends the
   recording.

Only the page's own http(s)/file scripts are counted, so your `eval_js` and
`snapshot` calls don't inflate the numbers. Functions nested inside functions
that haven't run yet may not be listed, so the function total can grow as you
explore.

## Delegating a whole task (optional)

`run_task(task, start_url, max_steps)` hands the task to cualm's own agent
loop. That loop uses the OpenAI-compatible model configured for the
container, which is usually a cheaper or local model. It blocks and then
returns JSON with `status`, `summary`, `result` and `audit_dir`.

- Use it for long, routine flows where your own reasoning is not needed.
- Give a concrete goal and success criteria, and state what value to
  return in `result`.
- Check `status`. `completed` means the loop believes it finished. Verify
  with a `snapshot` if it matters.
- While `run_task` runs, the other tools refuse to act.
