# Building a Computer Use Agent from Scratch

A practical course based on building Cualm, a real system that controls a Linux desktop through a screenshot → LLM → command loop, evaluated against web benchmarks.

---

## Course Overview

This course walks you through building a complete Computer Use Agent (CUA) — a system where a vision-language model sees a desktop screenshot, decides what to do, and executes commands to accomplish tasks. You will build every component yourself: the sandboxed environment, the agent loop, the tools, the prompt engineering, the calibration system, and two benchmark suites.

By the end you will have a working system that can log into websites, fill forms, extract data, generate reports, and explore applications — all driven by an LLM interpreting pixels.

**Prerequisites:** Comfortable with Python, Docker, basic shell scripting. Familiarity with LLM APIs (OpenAI-compatible). No specialized ML knowledge required — the LLM is a black box you call via API.

**What you will build:**

1. A containerized desktop environment with browser automation tools
2. An agent loop that converts screenshots into actions
3. A prompt engineering framework that actually works on real web tasks
4. A coordinate calibration system for vision models
5. An accessibility tree extractor for structured page understanding
6. A planning system that survives multi-step tasks
7. A WebArena benchmark runner with network isolation
8. A code coverage benchmark with CDP profiling

---

## Module 1: The Sandboxed Desktop

### 1.1 Why Containers?

The agent needs to click, type, scroll, and navigate a browser — but you don't want it doing this on your actual machine. Docker gives us a disposable Linux desktop where the worst case is "rebuild and try again."

The stack:

- **Xvfb** — Virtual framebuffer. Creates a fake display (`:99`) that renders to memory instead of a monitor.
- **Fluxbox** — Minimal window manager. Chromium needs *something* to manage windows.
- **Chromium** — The browser, launched with `--remote-debugging-port=9222` for CDP access.
- **x11vnc** — VNC server so you can watch what the agent sees. Essential for debugging.
- **xdotool** — Command-line tool for simulating keyboard and mouse input.
- **scrot** — Screenshot capture.

### 1.2 The Dockerfile

Start from `debian:bookworm-slim` (not Alpine — Chromium needs glibc). Install the full stack:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb fluxbox xdotool scrot x11vnc \
    chromium fonts-liberation fonts-noto-color-emoji \
    python3 python3-pip socat curl procps
```

Key decisions:

- **fonts-liberation** and **fonts-noto-color-emoji**: Without fonts, Chromium renders squares. Liberation provides standard web fonts; Noto provides emoji.
- **socat**: Used later for port forwarding in benchmark mode.
- **procps**: Provides `ps`, useful for debugging.

### 1.3 The Startup Script

`start.sh` orchestrates the boot sequence:

```bash
# Virtual display
Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset &
sleep 1

# Window manager
fluxbox &
sleep 1

# VNC (no password for debugging convenience)
x11vnc -display :99 -forever -nopw -rfbport 5900 -bg -q

# Chromium
chromium --start-fullscreen --no-first-run --no-sandbox \
    --remote-debugging-port=9222 --remote-allow-origins=* \
    "$START_URL" &
```

**Critical flag:** `--remote-allow-origins=*` is required since Chromium 112+. Without it, CDP websocket connections get 403 Forbidden — a silent, maddening failure that produces no error message in the Chromium console.

**Resolution choice:** 1280×720 is a good default. Higher resolution means more tokens per screenshot (vision models process patches). Lower means the model can't read small text. 720p is the sweet spot for most web UIs.

### 1.4 Exercise

Build the Docker image and verify you can:

1. `docker run -p 5900:5900 your-image` and connect via VNC
2. See Chromium running fullscreen
3. Run `xdotool` commands from `docker exec` and watch them work in VNC
4. Take a screenshot with `scrot` and verify it captures the screen

---

## Module 2: The Agent Loop

### 2.1 The Core Cycle

Every computer use agent follows the same fundamental loop:

```
for each step:
    1. Observe  (screenshot + any structured data)
    2. Think    (send to LLM, get response)
    3. Act      (parse command from response, execute it)
    4. Repeat   (until task done or step limit reached)
```

This is deceptively simple. The complexity is in every detail.

### 2.2 Observation: Screenshots

```python
def take_screenshot() -> str:
    result = subprocess.run(["scrot", "-o", "/tmp/shot.png"],
                          capture_output=True)
    with open("/tmp/shot.png", "rb") as f:
        return base64.b64encode(f.read()).decode()
```

The screenshot is base64-encoded and sent as an image in the LLM message. At 1280×720, this costs roughly 300 tokens with most vision models (Qwen2.5-VL uses 28px patches with 2×2 merge, yielding ~299 tokens).

**Key insight: screenshots are expensive.** Each step adds ~300 tokens of image data. Over a 20-step task, that's 6000 tokens of screenshots alone. This drives the entire context management strategy (Module 5).

### 2.3 Thinking: LLM Call

Use the OpenAI-compatible API — this makes the agent model-agnostic:

```python
client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL"),
    api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
)

response = client.chat.completions.create(
    model=MODEL,
    messages=messages,
    max_tokens=1024,
    temperature=0.7,
)
```

This works with local models (vLLM, llama.cpp, Ollama), cloud APIs (OpenAI, Anthropic via proxy), or anything that speaks the same protocol.

**Handling thinking tokens:** Some backends (llama.cpp, vLLM) return reasoning in a separate `reasoning_content` field instead of inline in `content`. Check for it:

```python
msg = response.choices[0].message
reasoning = getattr(msg, "reasoning_content", None)
if not reasoning:
    extra = getattr(msg, "model_extra", None) or {}
    reasoning = extra.get("reasoning_content")
if reasoning:
    content = f"<think>{reasoning}</think>\n{content}"
```

### 2.4 Acting: Command Parsing

The LLM outputs free-form text. You need to extract an executable command. The simplest format: lines starting with `run: `.

```python
def extract_command(text: str) -> str:
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("run: "):
            return line[5:].strip()
    raise ValueError("No 'run: ' command found")
```

Why `run: ` and not JSON or function calling?

- **Simplicity**: Every model can output `run: cua-click 450 300`. Not every model does structured output reliably.
- **Composability**: `run: cua-click 450 300 && cua-type "hello" && cua-key Return` — shell chaining for free.
- **Debugging**: You can read the logs and immediately understand what happened.

### 2.5 Executing: Shell Commands

```python
def run_command(command: str) -> str:
    result = subprocess.run(
        command, shell=True, capture_output=True,
        text=True, timeout=30,
    )
    return result.stdout + result.stderr
```

The output is fed back to the LLM on the next step. This closes the feedback loop — the model sees what its command actually did.

### 2.6 Step Budget and Termination

Three ways a task ends:

1. **Success**: The model calls `cua-done "Task complete" --result "answer"`
2. **Step limit**: Configurable max steps (default: 50)
3. **Error limit**: Too many consecutive API errors or parse failures

### 2.7 Exercise

Implement the basic loop. Verify that:

1. The agent takes a screenshot
2. Sends it to an LLM with a simple prompt ("Describe what you see")
3. Parses a command from the response
4. Executes it
5. Takes the next screenshot showing the result

---

## Module 3: Tools and Interactions

### 3.1 Why Dedicated Tools?

You could have the model type `xdotool` commands directly. But:

- `xdotool mousemove --sync 450 300 && xdotool click 1` is verbose and error-prone
- Coordinates need calibration mapping (Module 6)
- You need consistent output formats for the feedback loop
- Some operations (CDP JavaScript, planning) are complex multi-step actions

So you build small wrapper scripts that present a clean interface.

### 3.2 The Tool Set

| Tool | What it wraps | Why it exists |
|------|--------------|---------------|
| `cua-click X Y` | xdotool mousemove + click | Applies calibration mapping |
| `cua-type "text"` | xdotool type | Handles quoting and special chars |
| `cua-key combo` | xdotool key | Normalizes key names |
| `cua-scroll X Y` | xdotool mousemove + click 4/5 | Directional scrolling |
| `cua-wait` | sleep | Capped at 3s to prevent waste |
| `cua-screenshot` | scrot + base64 | Consistent format |
| `cua-cdp-js "expr"` | CDP Runtime.evaluate | JS execution without DevTools |
| `cua-plan "steps"` | File-based plan management | Structured planning |
| `cua-done "msg"` | Write result + signal exit | Clean task completion |

### 3.3 Tool Discovery

Rather than hardcoding tool descriptions in the prompt, auto-discover them:

```python
def discover_tools(tools_dir):
    descriptions = []
    for tool in sorted(os.listdir(tools_dir)):
        result = subprocess.run(
            [os.path.join(tools_dir, tool), "--help"],
            capture_output=True, text=True, timeout=5,
        )
        descriptions.append(result.stdout)
    return "\n\n".join(descriptions)
```

This means adding a new tool is: write the script with `--help` support, put it in `tools/`, done. No prompt changes needed.

### 3.4 JavaScript Execution via CDP

This is one of the most powerful tools. Instead of scrolling through a 200-row table trying to read it from a screenshot, the model can:

```bash
cua-cdp-js "[...document.querySelectorAll('table tbody tr')]
    .slice(0,5).map(r => r.innerText).join('\n')"
```

Implementation uses Chrome DevTools Protocol over websocket:

```python
def execute_js(expression):
    ws_url = get_page_ws_url()  # http://localhost:9222/json
    ws = websocket.create_connection(ws_url)
    ws.send(json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": expression, "returnByValue": True}
    }))
    result = json.loads(ws.recv())
    return result["result"]["result"]["value"]
```

**Pitfall: `const`/`let` re-declaration.** The CDP execution context persists. If the model runs `const rows = [...]` and then retries, it gets `SyntaxError: Identifier 'rows' has already been declared`. Solution: tell the model in the prompt to use `var` or inline expressions. This is a real bug that costs benchmark runs.

### 3.5 Exercise

Build the click tool with calibration support (identity mapping for now). Verify you can:

```bash
docker exec <container> cua-click 640 360    # click center of screen
docker exec <container> cua-type "hello"     # type text
docker exec <container> cua-key Return       # press enter
docker exec <container> cua-cdp-js "document.title"  # read page title
```

---

## Module 4: Prompt Engineering for Computer Use

### 4.1 The System Prompt

The system prompt is the most important file in the project. It teaches the model how to operate in this environment. It must cover:

1. **Identity and constraints**: "You control a computer by running shell commands."
2. **Output format**: "Write exactly one command on a line starting with `run: `."
3. **Efficiency**: "Chain actions with `&&`. Minimize steps."
4. **Tools**: Auto-discovered descriptions.
5. **Planning**: How to use the plan tool.
6. **Page awareness**: Status bar, scrolling, viewport limits.
7. **Form interaction**: Clear-first, verify-after pattern.
8. **Data extraction**: When to use JS vs. visual inspection.
9. **Error recovery**: "Never repeat a failed approach."
10. **Completion**: How and when to call `cua-done`.

### 4.2 The Efficiency Principle

Every step costs ~300 image tokens + prompt overhead. A 20-step task might use 150K tokens. So the prompt must aggressively teach the model to batch:

```
WRONG (3 steps):
  run: cua-click 450 250
  run: cua-type "admin"
  run: cua-key Tab

RIGHT (1 step):
  run: cua-click 450 250 && cua-type "admin" && cua-key Tab && cua-type "pass" && cua-key Return
```

This single prompt instruction can halve the step count for many tasks.

### 4.3 Teaching Form Interaction

Forms are where CUA agents fail most often. The model clicks a field, types "03/15/2024", but the field transforms it to "03152024" (stripped slashes) or appends to existing text ("old03/15/2024"). The prompt must teach a rigid protocol:

```
1. Click the field
2. Ctrl+A (select all)
3. Delete (clear)
4. Type the value
5. Verify with cua-cdp-js: document.activeElement.value
```

This pattern eliminates entire categories of failures: stale values, format mismatches, focus errors. Without it, form tasks have a ~30% failure rate from input errors alone.

### 4.4 Examples and Generic Language

The prompt must be **generic** — no task-specific terms. If you include Magento-specific examples, the model overfits to those patterns and fails on Reddit or GitLab tasks.

Bad: `run: cua-click 450 300  # click the 'Add to Cart' button`

Good: `run: cua-click 450 300`

Audit your prompt regularly for domain leakage.

### 4.5 Per-Step Prompt Structure

Each step assembles a user message from parts:

```
Step 0 (first step):
  Task: <task description>
  Create a plan using cua-plan with the steps needed.
  [Accessibility tree]
  [Screenshot]

Step N (subsequent):
  Task: <task description>
  Previous observation: <summary from model's last <think> block>
  Command: <what was run>
  Output: <what it produced>
  [Current plan status]
  [Accessibility tree]
  [Any warnings (loop detection, coverage status)]
  Here is the updated screenshot. Update your plan and continue.
  [Screenshot]
```

This gives the model everything it needs without overwhelming it. The "previous observation" is crucial — it replaces keeping old screenshots in context.

### 4.6 Exercise

Write a system prompt for your agent. Test it on three tasks:

1. Navigate to a URL and report the page title
2. Fill in a login form
3. Extract data from a table

Iterate on the prompt based on failure patterns.

---

## Module 5: Context Management — The Token Budget

### 5.1 The Problem

Vision models have context limits (8K–128K tokens). Each step adds:

- ~300 tokens for the screenshot
- ~100–400 tokens for the accessibility tree
- ~200 tokens for the prompt wrapper (task, plan, output)
- ~200 tokens for the model's response

A 30-step task can easily burn through 30K tokens — and that's without history.

### 5.2 Strategy: Discard Images, Keep Summaries

The key insight: **the model's own thinking contains a compressed summary of what it saw.** The `<think>` block typically starts with "I can see a login page with a username field and..." — this is a 50-80 token text description that captures the essential state.

Extract it:

```python
def extract_summary(reply: str) -> str:
    match = re.search(r"<think>(.*?)</think>", reply, re.DOTALL)
    if not match:
        return ""
    think = match.group(1).strip()
    return think[:300]  # truncate to ~80 tokens
```

Inject it on the next step as `Previous observation: <summary>`.

This replaces multi-turn image history. Instead of keeping N screenshots in context (~300 tokens each), you keep one text summary (~80 tokens). With `max_history_pairs=0`, the context never grows — each step is: system prompt + one user message with one screenshot.

### 5.3 Message Trimming

```python
def trim_messages(messages):
    # Always keep system prompt
    sys_count = sum(1 for m in messages if m["role"] == "system")
    if MAX_HISTORY_PAIRS == 0:
        return messages[:sys_count]  # only system prompt
    keep = MAX_HISTORY_PAIRS * 2
    return messages[:sys_count] + messages[-keep:]
```

With `history_pairs=0`, the model operates step-by-step with no conversational memory — just the system prompt, current observation, and injected summary. This is counter-intuitive but works because the plan file and text summary carry forward the essential state.

### 5.4 Token Accounting

Track token usage to understand your budget:

```python
usage = response.usage
print(f"Tokens: {usage.prompt_tokens}→{usage.completion_tokens} "
      f"({elapsed:.1f}s, {usage.completion_tokens/elapsed:.1f} tok/s)")
```

This helps you spot prompt bloat early. If prompt tokens jump from 3K to 8K, something is wrong.

### 5.5 Exercise

Implement the summary extraction and message trimming. Run a 20-step task and compare:

- With `history_pairs=3`: Total tokens, task success
- With `history_pairs=0` + text summaries: Total tokens, task success

You should see significant token savings with minimal accuracy loss.

---

## Module 6: Coordinate Calibration

### 6.1 The Problem

When you show a vision model a 1280×720 screenshot and ask "click the button at pixel (640, 360)", different models interpret coordinates differently:

- Some use the actual pixel space of the image
- Some use a normalized 0–1000 coordinate system
- Some are biased toward certain regions
- The mapping may not be linear

If the model says "click (450, 300)" but means the physical pixel (400, 168), every click misses.

### 6.2 The Calibration Protocol

Show a page with a series of known target dots at specific screen positions. Ask the model to click them. Compare where the model said to click vs. where the dots actually were.

```
Calibration page:
  Step 1: Dot at screen center (640, 360)
  Step 2: Dot at (200, 150)
  Step 3: Dot at (900, 500)
```

From these 3 points, compute a linear mapping: `screen_x = model_x * scale_x + offset_x`.

### 6.3 Implementation

The calibration page is a simple HTML file that:

1. Shows a dot at a known position
2. Uses `onclick` to record where the model clicked
3. Computes the mapping from model→screen coordinates
4. Sets the window title to "PASS" or "FAIL"

The agent checks the window title after calibration to verify success:

```python
title = run_command("xdotool getactivewindow getwindowname")
if "PASS" in title:
    calibrated = True
```

### 6.4 Caching

Calibration is cached per model in a Docker volume:

```python
cal_path = f"/var/cua/calibration/{model_name}.json"
```

First run: calibration takes 3 LLM calls (~45 seconds). Subsequent runs: skip calibration, navigate directly to the task.

### 6.5 Failure Recovery

If calibration fails (model clicks the wrong spots), retry up to 3 times. If all attempts fail, fall back to identity mapping (1:1 coordinates). This is wrong but better than crashing — the model may still complete some tasks if it happens to use coordinates close to the actual pixel values.

### 6.6 The Coverage Benchmark Calibration Bug

A real bug we encountered: in coverage mode, Chromium opened the coverage target page instead of the calibration page. The model tried to calibrate against a complex web app, clicking random buttons, getting insane scale factors like `(2.78, 0.87)`, and failing all 3 attempts.

**Lesson:** The order of operations matters. Calibration must happen on the calibration page, before any navigation to the target.

**The fix:** Override `CUA_START_URL` just for the `start.sh` process:

```bash
CUA_START_URL=file:///app/calibration/index.html /app/start.sh &
```

This scoping means `start.sh` opens the calibration page, but the agent still sees the real target URL in its environment.

### 6.7 Exercise

Build a calibration page with 3 dots. Implement the 3-point calibration, caching, and retry logic. Test with your model and verify clicks land accurately after calibration.

---

## Module 7: Structured Page Understanding

### 7.1 Why Screenshots Aren't Enough

Screenshots fail when:

- **Text is small**: Model misreads "3,451" as "3,461"
- **Content is off-screen**: A dropdown has 20 options but only 5 are visible
- **Values need precision**: "What's in the date field?" — the model guesses wrong
- **Tables are dense**: Many rows of similar numbers

The accessibility tree gives you the structured data without visual ambiguity.

### 7.2 Chrome DevTools Protocol (CDP)

Chromium exposes its accessibility tree via CDP. Connect over websocket:

```python
# Get the websocket URL
resp = urllib.request.urlopen("http://localhost:9222/json")
tabs = json.loads(resp.read())
ws_url = tabs[0]["webSocketDebuggerUrl"]

# Fetch the full tree
ws = websocket.create_connection(ws_url)
ws.send(json.dumps({
    "id": 1, "method": "Accessibility.enable"
}))
ws.recv()
ws.send(json.dumps({
    "id": 2, "method": "Accessibility.getFullAXTree"
}))
tree = json.loads(ws.recv())
```

### 7.3 Smart Truncation

A full accessibility tree can be thousands of nodes. You need to truncate intelligently:

- **Skip noise roles**: `generic`, `none`, `presentation`, `InlineTextBox`
- **Limit children**: Show 8 per parent, with `... (showing 8/42 children)`
- **Limit total lines**: Hard cap at 150 lines
- **Limit depth**: Stop at depth 8
- **Show interesting roles** even without names: `button`, `link`, `textbox`, `combobox`
- **Show states**: `[focused]`, `[selected]`, `[expanded]`, `[checked]`

### 7.4 Type Safety Pitfall

CDP can return numbers for `name` and `value` fields. If you call `len()` on them:

```python
name = node.get("name", "")       # might be int!
display_name = name if len(name) <= 60 else ...  # TypeError!
```

**Fix:** Always coerce: `name = str(node.get("name", ""))`.

This was a real bug that produced `TypeError: object of type 'int' has no len()` and silently broke the accessibility tree for the entire step.

### 7.5 Example Output

```
Accessibility tree:
RootWebArea 'Bestsellers Report'
  navigation 'Main Menu'
    link 'Dashboard'
    link 'Reports' [focused]
  main
    heading 'Bestsellers Report'
    group 'Filter'
      combobox 'Period' value='Day'
        option 'Day' [selected]
        option 'Month'
        option 'Year'
      textbox 'From' value='01/01/2022'
    table 'Report'
      row
        columnheader 'Product'
        columnheader 'Qty'
      ... (showing 8/200 children)
```

The model can now see all dropdown options without clicking to open them, read exact form values without OCR errors, and understand the table structure without scrolling.

### 7.6 Exercise

Implement the accessibility tree fetcher with smart truncation. Test on a complex page (e.g., an admin dashboard). Verify that the output is useful and stays within your token budget (~100–400 tokens).

---

## Module 8: Planning and Loop Detection

### 8.1 Why Planning Matters

Without a plan, the model forgets what it was doing. On step 12 of a 20-step task, it might:

- Repeat step 3
- Try an approach that already failed
- Miss the remaining steps entirely

A persistent plan file acts as external memory.

### 8.2 The Full-Rewrite Approach

Instead of incremental updates (`--complete step 3`), the model rewrites the entire plan each step:

```bash
cua-plan "[DONE] Login | [DONE] Navigate | Filter data | Read results | Report"
```

Why full rewrite?

- **Restructuring is natural**: "Step 3 failed, replace it with two new steps"
- **No desync**: The plan always reflects the model's current understanding
- **Simpler parsing**: Just split on `|` and check for markers
- **Models prefer it**: LLMs are better at generating a complete list than surgically editing one element

### 8.3 Plan Parsing

Models produce messy output. Your parser must handle:

```
# All of these are valid:
[DONE] Login | Navigate | Report
✓ 1. Login | ○ 2. Navigate | ○ 3. Report
- [DONE] Login
- Navigate
- Report
```

Strip: status icons (`○ ✓ ✗ –`), number prefixes (`1.`, `2)`, `3a.`), bullet prefixes (`- `, `* `), whitespace.

### 8.4 Gating Completion

`cua-done` checks the plan before accepting:

```python
def done(message, result=None, force=False):
    plan = load_plan()
    pending = [s for s in plan["steps"] if s["status"] == "pending"]
    if pending and not force:
        print(f"Error: {len(pending)} steps still pending.")
        print("Mark them [DONE] or [FAIL], or use --force.")
        sys.exit(1)
```

This prevents premature completion — the model can't just say "done" without finishing its own plan.

### 8.5 Loop Detection

The agent can get stuck: clicking the same button 3 times because it doesn't realize the result is below the viewport. Or alternating between click and wait forever.

```python
def detect_loop(command_history):
    # 3 identical actions in a row
    last_3 = [normalize(c) for c in command_history[-3:]]
    if last_3[0] == last_3[1] == last_3[2]:
        return "WARNING: You repeated the same action 3 times..."

    # 6-step click-wait alternation
    if len(command_history) >= 6:
        last_6 = [normalize(c) for c in command_history[-6:]]
        if len(set(last_6)) <= 2:
            return "WARNING: You are stuck in a click-wait loop..."
```

The warning is injected into the prompt, not the model's action — so it reads the warning alongside the next screenshot and (hopefully) tries something different.

Normalization collapses coordinates to "N": `cua-click 450 300` and `cua-click 451 301` become the same pattern. Otherwise, slight coordinate variations defeat loop detection.

### 8.6 Exercise

Implement the plan tool and loop detection. Create a task that reliably triggers a loop (e.g., "click a button that loads content below the viewport") and verify the warning injection causes the model to change strategy.

---

## Module 9: Evaluation — The WebArena Benchmark

### 9.1 What WebArena Tests

WebArena provides 812 tasks across 5 self-hosted websites:

- **Magento** (shopping + admin): E-commerce, order management, reports
- **Reddit**: Post, comment, search, moderate
- **GitLab**: Code repos, issues, merge requests
- **Wikipedia**: Search, read, cross-reference
- **OpenStreetMap**: Find locations, directions

Tasks range from simple ("search for wireless mouse") to complex ("find the best-selling product in Q3 2022 from the admin reports").

### 9.2 Network Isolation

The agent must not reach the real internet — it should only see the self-hosted sites and the LLM endpoint. Use Docker networks:

```
┌─── internal network (no internet) ───┐
│                                       │
│  shopping  reddit  gitlab  ...        │
│       │                               │
│  nginx gateway  ←──→  agent           │
│       │                               │
└───────┼───────────────────────────────┘
        │
┌───────┼──── bridge network ───────────┐
│  nginx gateway → host LLM             │
└───────────────────────────────────────┘
```

The nginx gateway sits on both networks. It forwards site traffic internally and LLM traffic to the host. The agent only connects to the internal network.

### 9.3 Port Forwarding with socat

WebArena's task descriptions use hardcoded URLs like `http://localhost:7770`. Inside the container, these must reach the actual site containers. `socat` handles this:

```bash
socat TCP-LISTEN:7770,fork,reuseaddr TCP:shopping:80 &
socat TCP-LISTEN:7780,fork,reuseaddr TCP:shopping_admin:80 &
```

### 9.4 Evaluation Methods

Each task specifies how to evaluate the agent's answer:

- **string_match**: Fuzzy comparison — `must_include` strings, `exact_match`
- **url_match**: Compare the final URL to an expected pattern
- **program_html**: Run JavaScript against the DOM to check the result

String match is the simplest and most common. The others require HAR capture for full evaluation.

### 9.5 HAR Capture

For WebArena-Verified evaluation, you need a HAR (HTTP Archive) trace of all network requests the agent made. Use mitmproxy as a sidecar:

```python
har_proxy = HarProxy(
    client=docker_client,
    task_id=task_id,
    network="webarena-net",
    output_dir=output_dir,
)
har_proxy.start()
env["CUA_HTTP_PROXY"] = har_proxy.proxy_url
```

**Pitfall:** Chromium's `--proxy-bypass-list=<-loopback>` is needed to force localhost traffic through the proxy. Without it, requests to `localhost:7770` bypass the proxy entirely and the HAR trace is empty.

**Pitfall:** The mitmproxy addon must rewrite URLs in the HAR. The agent sends requests to `localhost:7770`, but the evaluator expects `shopping:80`. A custom addon translates between the two coordinate systems.

### 9.6 Exercise

Set up the WebArena shopping site. Run 5 tasks and evaluate them with string_match. Calculate your pass rate.

---

## Module 10: The Coverage Benchmark

### 10.1 What Coverage Measures

Code coverage asks: "What percentage of the application's JavaScript and CSS did the agent actually trigger?" This is a proxy for how thoroughly the agent explored the application.

Unlike WebArena (did you get the right answer?), coverage is continuous (0–100%) and doesn't require task-specific evaluation criteria.

### 10.2 CDP Coverage Profiling

Chrome exposes code coverage through two CDP domains:

```python
# JavaScript
ws.send({"method": "Profiler.enable"})
ws.send({"method": "Profiler.startPreciseCoverage",
         "params": {"callCount": True, "detailed": True}})

# CSS
ws.send({"method": "CSS.enable"})
ws.send({"method": "CSS.startRuleUsageTracking"})
```

### 10.3 The Persistent Connection Problem

**Obstacle:** CDP profiler state is per-session — tied to the websocket connection. If you open a connection, start the profiler, close the connection, then open a new connection for a snapshot... the new connection has no profiler. You get `0/0 bytes, 0 files`.

**Solution:** A background daemon that keeps the websocket alive:

```python
def daemon_main():
    ws = websocket.create_connection(ws_url)

    # Start profiler (lives as long as this connection)
    cdp_call(ws, "Profiler.startPreciseCoverage", {...})

    # Poll for commands via file system
    while running:
        if os.path.isfile(CMD_FILE):
            cmd = json.load(open(CMD_FILE))
            if cmd["cmd"] == "snapshot":
                result = cdp_call(ws, "Profiler.takePreciseCoverage")
                # process and write result
```

The CLI tool (`start`, `snapshot`, `report`, `stop`) communicates with the daemon through `/tmp` files.

### 10.4 The Counter Reset Problem

**Obstacle:** `Profiler.takePreciseCoverage` resets all execution counters to zero. Snapshot 1 shows the page load code (count>0). Snapshot 2 shows all that code as count=0 because it wasn't re-executed since snapshot 1. If you just report each snapshot independently, coverage fluctuates wildly.

**Solution:** Cumulative byte-range tracking. Each snapshot produces `covered_segments` — a list of `(start_offset, end_offset)` pairs. The daemon unions these across all snapshots:

```python
cumulative[url] = {
    "total": max(old_total, new_total),
    "covered_ranges": merge_ranges(old_ranges, new_ranges),
}
```

Coverage can only grow, never shrink.

### 10.5 V8's Nested Range Format

**Obstacle:** V8 doesn't just say "bytes 100-200 were executed." It returns a tree of nested ranges where inner ranges override outer ones:

```
function foo() {          // range 0-100, count=1
  if (rare) {             // range 20-50, count=0  ← overrides!
    ...
  }
}
```

Naively summing all ranges with count>0 gives 100 bytes. But 20-50 has count=0, so the correct answer is 70 bytes.

**Solution:** A segment algorithm. Split the script at every range boundary, then for each segment find the innermost (narrowest) enclosing range:

```python
def covered_segments(ranges):
    points = sorted(set(s for s, e, c in ranges) |
                    set(e for s, e, c in ranges))
    segments = []
    for i in range(len(points) - 1):
        seg_start, seg_end = points[i], points[i + 1]
        # Find narrowest enclosing range
        best_count, best_span = 0, float("inf")
        for rs, re, count in ranges:
            if rs <= seg_start and re >= seg_end:
                span = re - rs
                if span < best_span:
                    best_span, best_count = span, count
        if best_count > 0:
            segments.append((seg_start, seg_end))
    return segments
```

### 10.6 The Function Scope Problem

**Obstacle:** V8's `functions[0]` is always the top-level script (range 0 → end-of-file). Every named function is nested inside it. If you process functions independently and sum their totals, you count every byte 2-3 times (once for the script, once for the function, once for inner functions). This produced coverage readings of 218%.

**Solution:** Flatten all ranges from all functions into a single list per script. `file_total` = the top-level range (= file size). Run the segment algorithm once.

### 10.7 The Start-Before-Navigate Pattern

**Obstacle:** CDP profiler only instruments scripts loaded after `startPreciseCoverage`. If you navigate to the page first, then start the profiler, all scripts are already loaded and invisible.

**Solution:** Start the profiler before navigating:

```
1. Calibration completes (browser on calibration page)
2. Start coverage profiler ← profiler is armed
3. Navigate to target URL ← scripts load under profiler
```

### 10.8 Verifying Your Coverage Implementation

Build a simple test page with known behavior:

```javascript
// test.js — 4 functions, ~equal size
function funcA() { ... }
function funcB() { ... }
function funcC() { ... }
function funcD() { ... }
```

Then a verification script that:

1. Starts coverage
2. Navigates to the page
3. Snapshots → expect ~20-30% (top-level code)
4. Calls `funcA()` → expect increase
5. Calls `funcB()` → expect increase
6. Calls `funcC()` → expect increase
7. Calls `funcD()` → expect ~100%
8. Asserts coverage never decreases

Add a complex test with nesting, branches, closures, try/catch, switch, IIFE, and dead code. Dead code should keep final coverage below 100%.

### 10.9 Exercise

Implement the coverage daemon, the segment algorithm, and the cumulative tracking. Run the verification suite. Then run the agent against a real web app and watch coverage increase as it explores.

---

## Module 11: The Status Bar (HUD)

### 11.1 Why a HUD?

The model sees a screenshot but doesn't inherently know:

- How far down the page it has scrolled
- Whether the page has more content below
- Which element has keyboard focus
- Whether the page changed after an action
- The current URL (address bar might be hidden in fullscreen)

### 11.2 Implementation as a Chrome Extension

A simple content script injects a fixed bar at the bottom of every page:

```javascript
// content script
const bar = document.createElement("div");
bar.style.cssText = "position:fixed; bottom:0; width:100%; height:18px; ...";
document.body.appendChild(bar);

setInterval(() => {
    const scrollPct = Math.round(scrollY / (document.body.scrollHeight - innerHeight) * 100);
    const focused = document.activeElement?.tagName + "#" + document.activeElement?.id;
    bar.textContent = `scroll: ${scrollPct}% | page: ${document.body.scrollHeight}px | focus: ${focused} | url: ${location.pathname}`;
}, 500);
```

### 11.3 DOM Change Tracking

The "dom: changed 2s ago" indicator uses a MutationObserver:

```javascript
let lastChange = 0;
new MutationObserver(() => lastChange = Date.now())
    .observe(document.body, {childList: true, subtree: true, attributes: true});
```

This tells the model whether its action had an effect. "changed just now" = the click worked. "idle" = nothing happened, try something else.

### 11.4 Exercise

Build the HUD extension. Verify that the model can read scroll position, focus state, and DOM change timing from screenshots.

---

## Module 12: Running at Scale

### 12.1 Container Lifecycle

Each benchmark task runs in a fresh container:

```python
container = client.containers.run(
    image="cua-agent",
    command=["agent", instruction],
    network="webarena-net",
    mem_limit="2g",
    shm_size="256m",
    detach=True,
)
```

`shm_size="256m"` prevents Chromium crashes — it uses shared memory for rendering.

### 12.2 Parallel Execution

Tasks are independent, so you can run N containers simultaneously. But watch for:

- **VNC port conflicts**: Each container needs its own VNC port
- **Memory**: Each container uses ~500MB–1GB
- **LLM throughput**: Local models may bottleneck on concurrent requests

### 12.3 Cost Estimation

With a 20-step average:

- ~150K prompt tokens + ~3K completion tokens per task
- At GPT-4V pricing (~$10/1M input, $30/1M output): ~$1.60/task
- 812 tasks: ~$1,300 for a full run

With local models (Qwen2.5-VL 72B): electricity + GPU time only.

### 12.4 Failure Analysis

Categories of failure:

1. **Calibration failures** (~5%): Clicks miss targets. Look for erratic coordinates.
2. **Form input errors** (~15%): Wrong format, stale values, wrong field. Look for verification mismatches.
3. **Navigation failures** (~10%): Can't find the right page. Look for plan stalls.
4. **Misread data** (~10%): OCR-style errors from screenshots. Look for close-but-wrong answers.
5. **Step limit reached** (~20%): Task was too complex or agent got stuck. Look for loop warnings.
6. **Correct approach, wrong execution** (~15%): Knew what to do but fumbled. Look for repeated retries.

Build logging and audit trails for each task so you can categorize failures systematically.

### 12.5 Exercise

Run 50 tasks, categorize failures, and identify which prompt or tool changes would address the top 3 failure categories.

---

## Module 13: Debugging Containerized Agents

### 13.1 VNC: Watch in Real Time

Always have VNC open when debugging. Seeing what the agent sees is 10× faster than reading logs.

### 13.2 Common Pitfalls

**Empty environment variables:** Docker passes `-e VAR=""` which is different from the variable being unset. `os.environ.get("CUA_TEMPERATURE", default)` returns `""` (not the default), and `float("")` crashes.

**Fix:** Use the `or` pattern:

```python
float(os.environ.get("CUA_TEMPERATURE", "") or 0.7)
```

**Docker ENTRYPOINT vs CMD:** If your Dockerfile has `ENTRYPOINT ["/app/start.sh"]`, then `docker run image bash -c 'command'` doesn't run bash — it runs `/app/start.sh bash -c 'command'`. Use `--entrypoint bash` to override.

**CDP connection timing:** Chromium's CDP port may not be ready immediately after launch. Add a retry loop:

```python
for _ in range(20):
    try:
        return urllib.request.urlopen("http://localhost:9222/json")
    except:
        time.sleep(0.5)
```

### 13.3 The Audit Trail

Build a plugin that logs every step:

- Screenshot (saved as PNG)
- Accessibility tree (saved as text)
- Full prompt (saved as JSON)
- Model response
- Command executed
- Command output

This creates a complete replay of every task, invaluable for understanding failures.

### 13.4 Exercise

Enable the audit plugin, run a failing task, and use the audit trail to identify the exact step where it went wrong.

---

## Module 14: Advanced Topics

### 14.1 Model-Specific Quirks

Different models need different handling:

- **Qwen2.5-VL**: Good at coordinates, sometimes generates empty responses. Needs `presence_penalty=1.5` to avoid repetition.
- **GPT-4V**: Excellent OCR but expensive. Sometimes refuses to interact with "real websites" (thinks it's browsing the actual internet).
- **Claude**: Strong reasoning but coordinate accuracy varies. Benefits from more explicit spatial descriptions.
- **Local 7B models**: Can handle simple tasks but struggle with multi-step planning. May need `max_steps` increased and expectations lowered.

### 14.2 Plugin System

A hook-based plugin system lets you extend the agent without modifying core code:

```python
class Plugin:
    def on_pre_screenshot(self, ctx): ...
    def on_post_screenshot(self, ctx, img_b64): ...
    def on_pre_llm_call(self, ctx, messages): ...
    def on_post_llm_call(self, ctx, messages, reply, usage): ...
    def on_pre_command(self, ctx, command): ...
    def on_post_command(self, ctx, command, output): ...
```

Use cases: audit logging, token tracking, screenshot annotation, command filtering.

### 14.3 When to Use JS vs. Vision

A decision framework:

| Situation | Approach |
|-----------|----------|
| Read exact form values | JS: `document.querySelector('input').value` |
| Read dropdown options | JS or accessibility tree |
| Find a button to click | Vision (you need coordinates) |
| Read dense table data | JS: `querySelectorAll('tr')...` |
| Verify form submission worked | JS: check for error messages in DOM |
| Navigate to a specific URL | JS: `location.href = '...'` |
| Check if something exists | Accessibility tree |

Teaching the model this decision framework in the prompt significantly improves efficiency.

### 14.4 Prompt as API

The system prompt is your API contract with the model. Changes to the prompt are the highest-leverage interventions:

- Adding one example of form clearing → -30% form errors
- Adding the `const`/`let` warning → eliminates JS re-declaration crashes
- Adding "scroll down after clicking" → -20% loop detections
- Adding the status bar explanation → model starts using DOM change timing

Track prompt changes and their impact on benchmark scores.

---

## Appendix A: Debugging Checklist

When the agent fails:

1. **Check VNC**: Is the browser showing what you expect?
2. **Check the accessibility tree**: Does it match the screenshot?
3. **Check the model's reasoning**: Does the `<think>` block show correct understanding?
4. **Check the command**: Did the model produce a valid command?
5. **Check the output**: Did the command produce the expected result?
6. **Check the plan**: Is the model stuck on a failed step?
7. **Check for loops**: Is the command history showing repetition?
8. **Check calibration**: Are coordinates mapping correctly?
9. **Check token budget**: Is the context window full?
10. **Check the prompt**: Does it cover this failure mode?

## Appendix B: Key Metrics

Track these across benchmark runs:

- **Pass rate**: Tasks completed correctly / total tasks
- **Step efficiency**: Average steps for completed tasks
- **Token cost**: Average tokens per task
- **Failure categories**: Breakdown by type
- **Coverage growth curve**: How quickly coverage increases over steps
- **Calibration hit rate**: % of tasks where calibration succeeded on first try

## Appendix C: Architecture Decision Records

| Decision | Alternative | Why |
|----------|-------------|-----|
| Shell commands over function calling | JSON/structured output | Composability, simplicity, model-agnostic |
| Text summaries over image history | Keep all screenshots | 5× token savings, minimal accuracy loss |
| Full plan rewrite over incremental | `--complete step N` | Models produce better full lists than surgical edits |
| Daemon for coverage over per-call | Reconnect each time | CDP profiler state is per-session |
| Cumulative range union over max(used) | Report each snapshot independently | `takePreciseCoverage` resets counters |
| Calibration over fixed mapping | Assume 1:1 coordinates | Models vary wildly in coordinate interpretation |

---

*This course is based on the Cualm project — a real system that went through every one of these obstacles. The bugs described are real bugs. The solutions are the ones that survived production use.*

## Module 15: Ref-Based Element Interaction

### 15.1 The Coordinate Problem

Clicking by pixel coordinates works but is fragile — calibration errors accumulate, small buttons get missed, and the model wastes steps on imprecise clicks. What if the model could say "click the Submit button" instead of "click pixel (743, 412)"?

### 15.2 Two-Pass Accessibility Tree

The solution uses the accessibility tree we already fetch (Module 7), but in two passes:

**Pass 1: Assign refs.** Walk the *entire* tree and assign sequential IDs (`e1`, `e2`, ...) to every interactive element (buttons, links, inputs, comboboxes, checkboxes). Store the mapping from ref → `backendDOMNodeId` in `/tmp/cua_a11y_refs.json`.

**Pass 2: Render for prompt.** Truncate the tree for the prompt (MAX_LINES=250) as before, but now each interactive element shows its ref: `button 'Submit' [e15]`.

The key insight: refs are assigned to *all* elements in pass 1, but only elements visible in the truncated tree show their refs in text. However, the model can also read ref badges from the screenshot — even for elements truncated from the tree text.

### 15.3 Visual Ref Badges

Inject floating overlay divs positioned via `getBoundingClientRect()`:

```javascript
const el = document.querySelector('[data-cua-ref="e15"]');
const rect = el.getBoundingClientRect();
const badge = document.createElement('div');
badge.textContent = 'e15';
badge.style.cssText = `position:fixed; left:${rect.left}px; top:${rect.top-14}px; ...`;
document.body.appendChild(badge);
```

Filtering: skip off-screen elements, check occlusion via `elementFromPoint`, respect `aria-hidden`, CSS visibility. Clean up old overlays before each step.

**Order matters:** Inject badges → take screenshot → badges appear in the image the model sees.

### 15.4 Playwright Integration

`cua-pw` connects to the existing Chromium via `connect_over_cdp`:

```python
from playwright.sync_api import sync_playwright
browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
page = browser.contexts[0].pages[0]
```

Ref resolution: CDP `DOM.resolveNode(backendNodeId)` → get `objectId` → tag with `data-cua-ref` attribute → Playwright `page.locator('[data-cua-ref="e15"]')`.

Subcommands: `click`, `fill`, `select`, `check`, `uncheck`, `text`, `hover`.

### 15.5 Exercise

Add ref assignment to your accessibility tree. Implement `cua-pw click e5`. Verify that clicking by ref is more reliable than clicking by coordinates on a page with small, closely-spaced buttons.

---

## Module 16: XMPP Integration — Phone-Controlled Agent

### 16.1 The Use Case

Instead of SSH + VNC + command line, control the agent from your phone. Send a message like "Check the weather in Madrid" and get the result back as a chat message with a screenshot.

### 16.2 Architecture

Three components, all in Docker via compose extensions:

1. **Prosody** — XMPP server (in `compose.d/xmpp/`). Self-signed TLS certs, three accounts: `agent@cua.local`, `user@cua.local`, `orchestrator@cua.local`.

2. **Orchestrator** — Python bot (in `compose.d/orchestrator/`). Listens for messages from the user, spawns a fresh agent container per task, streams progress to a MUC room, sends the result + screenshot back.

3. **Agent** — The standard `cua-agent` container, spawned on demand with the Docker API.

### 16.3 The Orchestrator Flow

```
1. User sends "Find today's featured article on Wikipedia" to orchestrator@cua.local
2. Orchestrator creates MUC room: task-1-find-todays-featured-...@conference.cua.local
3. Orchestrator invites user to the room
4. Orchestrator spawns: docker run cua-agent agent "Find today's..."
5. Orchestrator streams agent stdout to the MUC room (step markers, commands)
6. Agent completes: writes /tmp/cua_done.json with result + screenshot
7. Orchestrator reads result, sends "✅ #1: Title\nResult: Easter Oratorio" to user
8. Orchestrator uploads final screenshot via XEP-0363 HTTP Upload
9. Container cleaned up
```

### 16.4 Compose Merging

`run.sh` auto-discovers compose files in `compose.d/*/compose.yml` and merges them with the base `docker-compose.yml`. Directories starting with `.` are skipped.

This means: drop `compose.d/xmpp/` and `compose.d/orchestrator/` into the project, run `./run.sh up`, and XMPP is enabled. Remove the directories to disable.

### 16.5 Self-Signed Certificates

Prosody generates self-signed certs at startup. Phone clients (Conversations) show a "trust this certificate?" prompt. The demo XMPP client bypasses TLS verification via a custom `ssl.SSLContext` with `CERT_NONE`.

**Lesson learned:** `slixmpp` (Python XMPP library) couldn't handle STARTTLS with Prosody's self-signed certs despite multiple approaches (`ssl_context`, `disable_starttls`, env vars). The solution: rewrite the client using raw sockets + manual STARTTLS upgrade, which worked immediately.

### 16.6 Exercise

Set up Prosody with `./run.sh up`. Connect from a phone XMPP client. Send a task and watch the MUC room for progress. Verify the result comes back with a screenshot.

---

## Module 17: Demo Recording & Presentation

### 17.1 Three Demo Modes

Each demo records the agent in action with different capture methods:

**WebArena:** Runs inside a demo container. Captures asciinema terminal recording, generates screenshot timelapse from audit data, produces a trace viewer HTML.

**Coverage:** Runs on the host via `benchmark.sh --coverage` (because it manages its own compose lifecycle). Captures terminal output via `tee`, persists audit screenshots via `CUA_AUDIT_DIR`, generates timelapse.

**Wikipedia:** Runs on the host. Starts services via `./run.sh up`, sends task via raw XMPP client, captures screenshots from the orchestrator-spawned agent container via `docker exec scrot`, generates timelapse.

### 17.2 The Screenshot Capture Problem

The orchestrator spawns agent containers dynamically. You can't predict the container name or map VNC ports in advance. Solution: `agent_capture.py` polls `docker ps` for containers matching `task-*`, then captures via:

```bash
docker exec <container> bash -c "DISPLAY=:99 scrot -o /tmp/cap.png"
docker cp <container>:/tmp/cap.png ./screenshots/step_0042.png
```

### 17.3 GitHub Pages Showcase

`demo/generate_site.py` processes demo output into a static site:

```bash
python3 demo/generate_site.py ./demo_out -o docs/
```

The generator:

1. **Finds demos** by scanning timestamped subdirectories for `webarena/`, `coverage/`, `wikipedia/`
2. **Parses XMPP logs** — strips ANSI codes, extracts sender/timestamp/body, identifies system messages
3. **Parses coverage traces** — reads `trace.json` for step commands and `metadata.json` for model info
4. **Selects screenshots** — evenly spaced subset (8 of 80 for wikipedia, 6 of 10 for coverage) to keep site under 10MB
5. **Generates HTML** — dark-themed single page with sticky nav, video embeds, chat view, screenshot gallery with lightbox zoom
6. **Copies assets** — only selected screenshots + all videos + trace.html

To publish: enable GitHub Pages on `docs/` in repo settings. Update after new demo runs with `git add docs/ && git push`.

### 17.4 Exercise

Run all three demos with `./demo.sh all`. Generate the showcase site and verify it renders correctly in a browser.

---

## Module 18: Testing Without a Real LLM

### 18.1 The Mock LLM Server

The biggest barrier to testing CUA agents: you need a running LLM. The mock server removes this dependency:

```python
from mock_llm import MockLLMServer

server = MockLLMServer(script=[
    {"match": r"navigate|go to", "response": 'run: cua-type "example.com" && cua-key Return'},
    {"match": r"Example Domain",  "response": 'run: cua-done "done" --result "OK"'},
])
```

It's a real HTTP server (runs on a random port) that matches prompts against regex patterns and returns scripted responses. No GPU, no model, deterministic results.

### 18.2 What You Can Test

With a mock LLM, you can verify:

- **Agent loop mechanics**: Does it call the LLM? Does it parse commands? Does it execute them?
- **Screenshot inclusion**: Does each request contain an image?
- **Task completion**: Does `cua-done` in the response end the loop?
- **Max steps**: Does the agent stop at the limit?
- **Error handling**: What happens with empty responses?
- **Evaluator logic**: String matching, normalization, nested alternatives.

What you *can't* test: Whether the model makes good decisions. That requires real LLM + benchmark runs.

### 18.3 Rootless Podman Gotcha

The mock server must bind to `0.0.0.0` (not `127.0.0.1`) when the agent runs in a container. Rootless Podman's `--network host` doesn't share the host's loopback — `127.0.0.1` inside the container is the container's own loopback.

```python
# For tests where the agent runs in a container:
MockLLMServer(script=..., host="0.0.0.0", port=0)
```

### 18.4 Exercise

Write a mock script for a 3-step task. Run the agent against it and verify the step count, LLM call count, and final result all match expectations. Then write a test that intentionally triggers max_steps and verify the agent stops.

---

## Module 19: CLI and Environment Detection

### 19.1 The cualm CLI

A single Python script that wraps all entry points:

```bash
./cualm agent "task"        # → ./run.sh agent "task"
./cualm serve               # → ./run.sh up
./cualm benchmark --tasks 0 # → ./benchmark.sh --tasks 0
./cualm selftest --full     # → runs 4-level sanity checks
./cualm setup               # → preflight + auto-build
```

On first run of any command, `ensure_ready()` runs preflight checks. If the agent image is missing but everything else is OK, it auto-builds.

### 19.2 System Detection

Detects:
- **OS**: `/etc/os-release` → Fedora, Ubuntu, Debian, Arch
- **GPU**: `lspci` → AMD/Nvidia/Intel, then `rocminfo` or `nvidia-smi` for driver
- **Runtime**: Podman (preferred) or Docker, rootless detection
- **Socket**: `DOCKER_HOST` or probe common paths
- **Image**: `docker image inspect cua-agent`
- **LLM**: `GET /v1/models`

Each check produces a `CheckResult` with `ok`, `message`, `hint`, and `blocking` flag. GPU without drivers is a warning; no runtime is blocking.

### 19.3 Per-Distro Hints

When something is missing, the hint is specific to the detected OS:

```
✗ Container runtime: No Docker or Podman found
  Install Podman:
    sudo dnf install podman podman-compose
    systemctl --user enable --now podman.socket
    https://podman.io/docs/installation
```

### 19.4 Exercise

Run `./cualm setup` on a fresh system (or a VM). Note which checks fail and whether the hints are actionable. Fix the issues and verify all checks pass.

---

## Appendix C: Architecture Decision Records (Updated)

| Decision | Alternative | Why |
|----------|-------------|-----|
| Shell commands over function calling | JSON/structured output | Composability, simplicity, model-agnostic |
| Text summaries over image history | Keep all screenshots | 5× token savings, minimal accuracy loss |
| Full plan rewrite over incremental | `--complete step N` | Models produce better full lists than surgical edits |
| Daemon for coverage over per-call | Reconnect each time | CDP profiler state is per-session |
| Ref badges as overlays over DOM children | Inject into page DOM | Overlays don't break page layout or CSS |
| Playwright for ref clicks over xdotool | Raw CDP DOM manipulation | Playwright handles scrolling, visibility, iframes |
| Raw socket XMPP over slixmpp | slixmpp library | STARTTLS + self-signed certs incompatible in slixmpp |
| Demo coverage on host over in container | Container-in-container | benchmark.sh manages its own compose lifecycle |
| Mock LLM for tests over real model | Test with actual LLM | Deterministic, fast, free, no GPU needed |
| Python CLI over Go binary | Compiled single binary | Python available everywhere, simpler to modify |
