# Coverage Test Target: test report

- **URL:** http://host.docker.internal:8080
- **Tested:** 2026-11-21, in the cualm sandbox (Chromium, 1280x720) by Claude Code subagents. There were two runs: the first exploration, then a second run with code coverage recording on.
- **Code coverage (second run):** JS **95.1%**, CSS **89.6%**, 57 of 58 functions called. See [Code coverage](#code-coverage).
- **JS errors seen:** none. `window.onerror`, unhandled rejections and `console.error` were hooked after the page loaded, so errors during load were not checked.

## Page summary

This is a single-page widget test page: static HTML plus `/index.js`, with no images and no links. It has a dark-themed card grid.

The header has a status line, a **Theme** toggle and a **↺ Reset** button that resets everything after a confirm dialog.

The cards are:

1. **Counter**: buttons for −1, +1, ×2 and Reset. The up and down arrow keys also change the value.
2. **Tasks**: add tasks with a priority, mark them done, delete them, or drag them.
3. **Validated Form**: Name, Email, Age, Category and a terms checkbox. Checks run in the page only and nothing is sent.
4. **Tabbed Content**: Overview (a progress slider), Details (accent colour swatches) and Settings (checkboxes and a Save button).
5. **Search & Filter**: a list of 9 items with All/Fruit/Veggie/Grain filters.
6. **Calculator**: two numbers and an operator (+ − × ÷ % ^).
7. **Drag & Drop Zone**: archives tasks dropped on it. Clicking it simulates an upload.
8. **Event Log**: Clear and Export buttons, plus Ctrl+L and Ctrl+E shortcuts.

These all worked as expected:

- Counter
- Task validation, adding, completing and deleting
- Form error messages
- Tabs, slider and swatches
- Search and filters
- Calculator arithmetic, including divide-by-zero and modulo-by-zero messages
- Theme toggle
- Clear and Export
- The Reset dialog: Cancel, Escape and clicking outside all close it

---

## Page bugs

### Functional

| # | Bug | Steps to reproduce | Observed | Expected |
|---|-----|--------------------|----------|----------|
| 1 | Reset leaves Search empty | Type a search with no matches (e.g. `rice `), then Reset → Confirm | Search box is empty and filter is All, but the list shows 0 items and "No matches found" | All 9 items. The cause: the reset handler calls `renderSearch()` before it clears `#searchInput`. |
| 2 | Reset is incomplete | Change theme, accent, tab, settings, task priority, calculator operator, add log entries; Reset → Confirm | All of these keep their values. Most visibly, the page stays in light theme after "Everything reset" | Everything returns to its default |
| 3 | Email check too loose | Submit the form with email `@.` or `a.b@` | "Form submitted successfully!" | Invalid email error. The check only looks for an `@` and a `.` anywhere. |
| 4 | Age accepts decimals and has conflicting limits | Enter age `120.9` | Accepted, because `parseInt` turns it into 120 | Rejected. Also, the input has `min=1 max=200` but the placeholder and checks say 18–120. |
| 5 | Archive count is wrong | Click the drop zone once ("Simulated upload #1"), then drop one task into it | "2 item(s) archived" | "1 item(s) archived". Uploads and archives share one counter. |
| 6 | Event log entries run together | Do several actions and look at the Event Log | All entries appear as one wrapped paragraph, and the box has a horizontal scrollbar | One entry per line. Entries are joined with `\n` but `#eventLog` has `white-space: normal`. |
| 7 | Active filter button not highlighted *(disputed)* | Click Fruit, Veggie or Grain | First run: all four buttons look the same | The selected filter is highlighted. **The second run disagreed:** after a click, the active button had a different background (`rgb(233,69,96)` vs `rgb(15,52,96)`). Check this by hand before fixing anything. |

### Found in the second run

| # | Bug | Steps to reproduce | Observed | Expected |
|---|-----|--------------------|----------|----------|
| 20 | Ages written with an exponent get through | Enter age `20e1` (= 200) with otherwise valid data, then Submit | "Form submitted successfully!", because `parseInt("20e1")` gives 20. `1e3` similarly fails as "Must be at least 18". | Rejected as out of range. This is related to bug 4 but is a different kind of input. |
| 21 | Calculator shows NaN as a success | −8 ^ 0.5 | "Result: NaN" with a green success toast | An error message |
| 22 | Status line never clears after Reset | Reset → Confirm | The header status stays at "All widgets reset" | It goes back to "Ready — 9 searchable items loaded" |
| 23 | Task priority not reset after adding a task | Pick High, add a task | The dropdown stays on High | It goes back to Low. This is minor and may be intended. |
| 24 | Export toast grammar | Export with one log entry | "Log exported (1 entries)" | "1 entry" |

The second run also confirmed bug 2: Reset leaves the tabs card (active tab and settings checkboxes), the calculator operator and the task priority untouched.

### Minor

| # | Bug | Details |
|---|-----|---------|
| 8 | Calculator toast shows internal operator names | e.g. "12 div 5 = 2.4" and "7 add 3 = 10" instead of ÷ and + |
| 9 | Calculator rounding and overflow | `0.00001 × 1` shows "Result: 0" because results are rounded to 4 decimal places. `1e308 × 10` shows "Result: Infinity" with a success toast. |
| 10 | Search doesn't trim spaces | `" RICE "` finds nothing |
| 11 | Task counter text differs on first load | It reads "0 tasks" when the page loads but "No tasks yet" after any change |
| 12 | "Nothing to export" can never appear | Clearing the log writes a "Log cleared" entry into it, so the log is never empty |
| 13 | Ctrl+E is case-sensitive | It only matches lowercase `e`, so it fails with Caps Lock on |
| 14 | Poor contrast in light theme | Card headings stay cyan `#53d8fb` on a white background |
| 15 | `/favicon.ico` returns 404 | Cosmetic |

### Accessibility

| # | Issue |
|---|-------|
| 16 | The Name, Email, Age and Category labels are not linked to their inputs |
| 17 | The task checkbox and ✕ delete button have no accessible name |
| 18 | The colour swatches are plain `<div>`s that can't be reached with the keyboard |
| 19 | No buttons set a `type`, so they all default to `type=submit` |

---

## Code coverage

Recorded with cualm's new `coverage_start` / `coverage_report` tools (V8 precise coverage via CDP). The subagent drove the page **only through real UI actions**, never by calling page functions. The page's `index.js` is 19,045 bytes.

| Step | JS % | Functions called |
|---|---|---|
| Page load | 22.5 | 11/43 |
| Counter (+1, −1, ×2 on zero and non-zero, Reset, arrow keys) | 31.8 | 19/44 |
| Tasks (empty, over 100 chars, Add button, Enter, priorities, complete, delete) | 44.3 | 27/51 |
| Form (all empty, invalid fields, age 150, valid) | 52.4 | 29/51 |
| Tabs (3 tabs, slider to 100, swatches, Settings save) | 61.2 | 36/54 |
| Search & Filter (query, no matches, all 4 filters) | 63.0 | 39/55 |
| Calculator (empty, every operator, ÷0, %0, overflow, NaN) | 70.1 | 40/55 |
| Drop zone (click upload, one drag attempt) | 73.6 | 45/55 |
| Theme, Reset dialog (Cancel / Escape / click outside / Confirm), Event Log (Clear, Export, Ctrl+L, Ctrl+E) | **95.1** | **57/58** |

**Final:** JS 95.1% (18,118 / 19,045 B), CSS 89.6% (9,467 / 10,565 B), 57/58 functions.

The function total grows from 43 to 58 because V8 only reports functions nested inside other functions once their parent has run. The byte total stays fixed, and coverage never went down.

### Code not covered

| Location | What it is | Why it didn't run |
|---|---|---|
| `index.js:486` and lines 489–511 | The drop zone's `drop` handler and its branches (archive a task, task not found, non-task drop) | The `drag` tool couldn't complete an HTML5 drag-and-drop at the time. **Since fixed** (see below): a later test with the fixed `drag` archived a task. |
| `index.js:596` | "Nothing to export" | Dead code. Clear, Ctrl+L and Export all log a toast, so the log is never empty (bug 12). |
| `index.js:464` | The calculator `switch`'s `default:` case | Dead code. The dropdown only offers the six handled operators. |

With the drag fix, everything except the two dead-code paths can be reached through the UI.

---

## Sandbox and tool issues (not page bugs)

> **Update:** these tool issues have since been fixed in cualm. Each one is marked **FIXED** below with a note on how it was checked.

### Tool calls that failed

Two calls failed. Nothing timed out and every other call worked.

| Tool | What was tried | What happened | Workaround | Cause |
|------|----------------|---------------|------------|-------|
| `click_element` | target `text=Details` to open the Details tab | `Locator.click: Error: strict mode violation: get_by_text("Details") resolved to 2 elements` (the tab button and a `<p>` in the panel that also contains "Details") | Used `role=button[name=Details]` | Tool: text selectors match substrings, so a word that appears more than once is ambiguous. **FIXED:** when a `text=` selector matches more than one element, `cua-pw` now uses the exact-text match. `click_element("text=Details")` now works. |
| `drag` | Drag the "Buy milk" task (450,247) onto the drop zone (786,560) at 75% zoom | Reported "Dragged model(450,247)→(786,560)", but nothing happened. In the second run `dragstart` and `dragover` fired, but `drop` never did. | Fired synthetic `DragEvent`s with a `DataTransfer` via `eval_js`, which archived the task correctly | Tool: one jump from A to B is too fast for Chromium's native drag-and-drop. **FIXED:** `cua-drag` now presses, nudges past the drag threshold, glides in steps, hovers on the target and then releases. A real `drag` now archives a task ("Dropped task: Drag me", "1 item(s) archived"). |

### Other sandbox quirks

| Quirk | Source | Notes |
|-------|--------|-------|
| Ref badges go stale and are added to the page | Tool | Badges from `screenshot(with_refs=true)` stay in the page as `position:fixed` divs. After scrolling or zooming they float in the wrong places; the second run saw this after zooming the browser out. **FIXED:** the overlay now removes itself on any scroll or resize (browser zoom fires a resize), and the next snapshot or screenshot draws fresh badges, which line up correctly at 80% zoom. The `#cua-ref-overlay` element and `data-cua-ref` attributes are still added to the page while the badges are shown. |
| Status strip covers the bottom of the page | Tool | The black "scroll / page / dom / focus / url" bar covers about the bottom 20px. **Partly FIXED:** the bar is now `aria-hidden`, so its text no longer appears in `snapshot`. It still covers the bottom of the screen; the extension adds bottom margin to the page so content can scroll clear of it. |
| ↺ and ✕ render as empty boxes | Sandbox fonts (likely) | Emoji such as 🌙 and ☀️ render fine, so a symbol font is probably missing |
| Small viewport | Environment | The page area is only about 628px tall. Zooming out with ctrl+minus works, but Chromium's zoom popup then covers the top-right corner. |
| Address bar focused after `navigate` | Environment | You have to click into the page before pressing keys. After that, Ctrl+L and Ctrl+E reach the page. |
| Export starts a real download | Environment | The Chromium download icon appeared. The saved file was not checked; only the blob link (`event-log.txt`) was confirmed. |
| `snapshot` output is cut off | Expected | It stops at `max_lines`; `eval_js` was used to read the rest |
| Toasts vanish after 3s | Page (by design) | They are often gone before the next screenshot, so toast text was read with `eval_js` right after each action |
