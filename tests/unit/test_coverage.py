"""Unit tests for the coverage math in cdp_coverage (no browser needed)."""

from cdp_coverage import CoverageState, covered_segments, merge_ranges


def test_merge_ranges():
    assert merge_ranges([(5, 8), (0, 3), (2, 4), (8, 9), (10, 10)]) == [(0, 4), (5, 9)]


def test_innermost_range_wins():
    # script 0-100 ran; function 20-50 never ran; inside it a block 30-40 "ran"
    # (impossible in practice, but checks the innermost rule)
    segs = covered_segments([(0, 100, 1), (20, 50, 0), (30, 40, 1)])
    assert segs == [(0, 20), (30, 40), (50, 100)]


def test_sibling_functions_and_uncovered_block():
    segs = covered_segments([
        (0, 100, 1),
        (10, 30, 2), (15, 20, 0),   # called fn with a skipped branch
        (40, 60, 0),                # never-called fn
    ])
    assert segs == [(0, 15), (20, 40), (60, 100)]


def test_all_zero_after_reset():
    # after takePreciseCoverage resets counters, nothing new ran
    assert covered_segments([(0, 100, 0), (10, 20, 0)]) == []


def _entry(url, ranges_by_fn, names=None):
    names = names or [""] * len(ranges_by_fn)
    return {"url": url, "functions": [
        {"functionName": n, "ranges": [
            {"startOffset": s, "endOffset": e, "count": c} for s, e, c in rs]}
        for n, rs in zip(names, ranges_by_fn)]}


def test_cumulative_union_never_shrinks():
    st = CoverageState()
    url = "http://x/app.js"
    scripts = {"1": {"url": url, "length": 100}}

    def snap(ranges, names):
        e = _entry(url, ranges, names)
        e["scriptId"] = "1"
        st.add_js([e], scripts)

    snap([[(0, 100, 1)], [(10, 30, 0)], [(40, 60, 0)]], ["", "onA", "onB"])
    f = next(iter(st.files.values()))
    assert f.used == 60
    # snapshot 2: counters reset, only onA ran
    snap([[(0, 100, 0)], [(10, 30, 1)], [(40, 60, 0)]], ["", "onA", "onB"])
    assert f.used == 80
    assert {n: hit for n, hit in f.functions.values()} == {"onA": True, "onB": False}
    # snapshot 3: V8 omits the top level and idle functions after a reset
    snap([[(40, 60, 1)]], ["onB"])
    assert f.used == 100 and len(st.files) == 1
    assert {n: hit for n, hit in f.functions.values()} == {"onA": True, "onB": True}


def test_ignores_eval_and_extension_scripts():
    st = CoverageState()
    st.add_js(scripts={}, entries=[_entry("", [[(0, 10, 1)]]),
               _entry("chrome-extension://abc/hud.js", [[(0, 10, 1)]])])
    assert st.files == {}


def test_css_and_report():
    st = CoverageState()
    st.add_js(scripts={}, entries=[_entry("http://x/a.js", [[(0, 100, 1)], [(40, 60, 0)]], ["", "neverRuns"])])
    sheets = {"s1": {"sourceURL": "http://x/a.css", "length": 50}}
    st.add_css([{"styleSheetId": "s1", "startOffset": 0, "endOffset": 10, "used": True},
                {"styleSheetId": "s1", "startOffset": 10, "endOffset": 50, "used": False},
                {"styleSheetId": "unknown", "startOffset": 0, "endOffset": 5, "used": True}],
               sheets)
    st.snapshots = 1
    out = st.report()
    assert "JS   80.0%" in out
    assert "CSS  20.0%" in out
    assert "functions called 0/1" in out
    assert "neverRuns" in out


def test_length_and_lines_from_scriptparsed():
    st = CoverageState()
    scripts = {"7": {"url": "http://x/p.html", "length": 200, "startLine": 10}}
    e = _entry("http://x/p.html", [[(50, 60, 0)]], ["handler"])
    e["scriptId"] = "7"
    st.add_js([e], scripts)
    f = next(iter(st.files.values()))
    assert f.total == 200 and f.start_line == 10 and f.used == 0
    out = st.report(line_of=lambda fc, off: fc.start_line + 3)
    assert "handler  (p.html:13)" in out
