"""Unit tests for benchmark evaluator."""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from benchmark.evaluate import check_string_match, evaluate_task, _normalize


# ── Helper ──

def _ref(**kwargs):
    """Build a reference_answers dict."""
    return kwargs


def _task(reference_answers, eval_type="string_match"):
    """Build a minimal task config."""
    return {
        "eval": {
            "eval_types": [eval_type],
            "reference_answers": reference_answers,
        }
    }


def _result(answer, outcome="completed"):
    return {"outcome": outcome, "result": answer}


# ── check_string_match tests ──

class TestStringMatch:

    def test_exact_hit(self):
        assert check_string_match("hello world", _ref(must_include=["hello world"]))

    def test_case_insensitive(self):
        assert check_string_match("hello world", _ref(must_include=["Hello World"]))

    def test_substring(self):
        assert check_string_match("hello world", _ref(must_include=["world"]))

    def test_miss(self):
        assert not check_string_match("hello world", _ref(must_include=["xyz"]))

    def test_multiple_must_include(self):
        ref = _ref(must_include=["hello", "world"])
        assert check_string_match("hello world", ref)
        assert not check_string_match("hello", ref)

    def test_must_exclude_not_implemented(self):
        """must_exclude is not currently implemented in the evaluator."""
        # If we add this feature, uncomment and fix:
        # ref = _ref(must_include=["hello"], must_exclude=["goodbye"])
        # assert check_string_match("hello", ref)
        # assert not check_string_match("hello goodbye", ref)
        pass

    def test_nested_alternatives(self):
        """WebArena uses [["option A", "option B"]] for alternatives."""
        ref = _ref(must_include=[["Yoga ball", "yoga ball"]])
        assert check_string_match("Yoga ball", ref)
        assert check_string_match("yoga ball", ref)
        assert not check_string_match("tennis ball", ref)

    def test_empty_answer(self):
        assert not check_string_match("", _ref(must_include=["something"]))

    def test_none_answer(self):
        assert not check_string_match(None, _ref(must_include=["something"]))


class TestExactMatch:

    def test_exact(self):
        ref = _ref(exact_match="42")
        assert check_string_match("42", ref)
        assert not check_string_match("43", ref)

    def test_exact_case_insensitive(self):
        assert check_string_match("hello", _ref(exact_match="Hello"))

    def test_exact_alternatives_list(self):
        ref = _ref(exact_match=["42", "forty-two"])
        assert check_string_match("42", ref)
        assert check_string_match("forty-two", ref)
        assert not check_string_match("43", ref)

    def test_exact_with_whitespace(self):
        assert check_string_match("  hello   world  ", _ref(exact_match="hello world"))


class TestNormalization:

    def test_unicode_symbols_stripped(self):
        assert "iphone" in _normalize("iPhone™")
        assert "iphone" in _normalize("iPhone®")
        assert "iphone" in _normalize("iPhone©")

    def test_whitespace_collapsed(self):
        assert _normalize("hello   world") == _normalize("hello world")
        assert _normalize("hello\nworld") == _normalize("hello world")

    def test_nfkd(self):
        # NFKD decomposes compatibility characters
        assert _normalize("\ufb01") == _normalize("fi")  # fi ligature


# ── evaluate_task (full flow) ──

class TestEvaluateTask:

    def test_completed_match(self):
        task = _task(_ref(must_include=["hello"]))
        assert evaluate_task(task, _result("hello world"))

    def test_completed_no_match(self):
        task = _task(_ref(must_include=["xyz"]))
        assert not evaluate_task(task, _result("hello world"))

    def test_failed_outcome(self):
        """Non-completed outcomes always fail."""
        task = _task(_ref(must_include=["hello"]))
        assert not evaluate_task(task, _result("hello", outcome="max_steps"))
        assert not evaluate_task(task, _result("hello", outcome="error"))

    def test_url_match_unsupported(self):
        """url_match returns False (needs WebArena-Verified)."""
        task = _task(_ref(must_include=["anything"]), eval_type="url_match")
        assert not evaluate_task(task, _result("anything"))

    def test_numeric_in_string(self):
        task = _task(_ref(must_include=["$100"]))
        assert evaluate_task(task, _result("The price is $100.00"))
        assert not evaluate_task(task, _result("100"))

    def test_special_chars(self):
        task = _task(_ref(must_include=["C++"]))
        assert evaluate_task(task, _result("I use C++ daily"))
