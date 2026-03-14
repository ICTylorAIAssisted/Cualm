"""Usage tracking plugin — token counts, throughput, session summary.

Tracks prompt and completion tokens across LLM calls and prints
per-step stats and a final summary at shutdown.

Delete this file to silence token logging.
"""


def on_startup(ctx):
    """Initialize counters."""
    ctx["usage"] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "llm_calls": 0,
        "total_llm_time_sec": 0.0,
    }


def on_post_llm_call(ctx, *, messages, reply, usage):
    """Record token usage and print per-step stats."""
    u = ctx["usage"]
    p = usage.get("prompt_tokens", 0)
    c = usage.get("completion_tokens", 0)
    t = usage.get("total_tokens", 0)
    elapsed = usage.get("elapsed_sec", 0.0)

    u["prompt_tokens"] += p
    u["completion_tokens"] += c
    u["total_tokens"] += t
    u["llm_calls"] += 1
    u["total_llm_time_sec"] += elapsed

    tps = c / elapsed if elapsed > 0 else 0.0
    avg_tps = (
        u["completion_tokens"] / u["total_llm_time_sec"]
        if u["total_llm_time_sec"] > 0
        else 0.0
    )
    print(
        f"   Tokens: {p}→{c}"
        f" ({elapsed:.1f}s, {tps:.1f} tok/s)"
        f" | Session: {u['total_tokens']} total"
        f", {avg_tps:.1f} avg tok/s"
    )


def on_shutdown(ctx):
    """Print session summary."""
    u = ctx.get("usage", {})
    step = ctx.get("step", 0)
    wall = ctx.get("wall_time_sec", 0)
    avg_tps = (
        u["completion_tokens"] / u["total_llm_time_sec"]
        if u.get("total_llm_time_sec", 0) > 0
        else 0.0
    )

    print("\n── Session Summary ──")
    print(f"   Steps: {step}")
    print(f"   Wall time: {wall:.1f}s")
    print(f"   LLM calls: {u.get('llm_calls', 0)}")
    print(f"   LLM time:  {u.get('total_llm_time_sec', 0):.1f}s")
    print(
        f"   Tokens: {u.get('prompt_tokens', 0)} prompt"
        f" + {u.get('completion_tokens', 0)} completion"
        f" = {u.get('total_tokens', 0)} total"
    )
    print(f"   Avg throughput: {avg_tps:.1f} completion tok/s")
