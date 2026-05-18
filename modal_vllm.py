# Deploy an OpenAI-compatible vLLM endpoint on Modal for cua-monkey.
#
# Adapted from Modal's official vLLM inference example:
#   https://modal.com/docs/examples/vllm_inference
#   https://github.com/modal-labs/modal-examples/blob/main/06_gpu_and_ml/llm-serving/vllm_inference.py
#
# Model, image, GPU, decorators, and command-line flags match the upstream
# tutorial verbatim. The ONE deviation is `--limit-mm-per-prompt`: upstream
# disables multimedia inputs (image=0/video=0/audio=0), but cua-monkey sends
# a screenshot in every chat request, so we allow up to 4 images per prompt.
# Flip `IMAGES_PER_PROMPT` to 0 if you want strict tutorial parity.
#
# Usage:
#   pip install modal
#   modal token new
#   modal deploy modal_vllm.py
#
# Then in `.env`:
#   UPSTREAM_SCHEME=https
#   UPSTREAM_HOST=<workspace>--example-vllm-inference-serve.modal.run
#   UPSTREAM_PORT=443
#   UPSTREAM_PATH=/v1
#   CUA_MODEL=llm                          # short served-model-name alias
#
# Tear down with:
#   modal app stop example-vllm-inference

import json
from typing import Any

import aiohttp
import modal

vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install(
        "vllm==0.19.0",
    )
    .uv_pip_install(  # as of vllm 0.19.0, must install transformers separately to use Gemma 4
        "transformers==5.5.0",
    )
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})  # faster model transfers
)

MODEL_NAME = "google/gemma-4-26B-A4B-it"
MODEL_REVISION = "47b6801b24d15ff9bcd8c96dfaea0be9ed3a0301"  # avoid nasty surprises when repos update!

hf_cache_vol = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
vllm_cache_vol = modal.Volume.from_name("vllm-cache", create_if_missing=True)

FAST_BOOT = False

app = modal.App("example-vllm-inference")

MINUTES = 60  # seconds
VLLM_PORT = 8000

# cua-monkey sends one screenshot per agent step. Upstream tutorial sets
# this to 0; we raise it so the agent can actually function.
IMAGES_PER_PROMPT = 4

# ── GPU profile ────────────────────────────────────────────────────────
# Pick one. Each profile sets the GPU type/count, quantization mode, and
# any extra vLLM flags needed for that hardware. Costs are Modal list
# prices (per hour, while the container is warm) — see modal.com/pricing.
#
#   "A100_80GB"  — bf16, 1×A100 80GB. $2.50/h. Easiest path, no quant.
#   "L40S_FP8"   — fp8,  1×L40S 48GB. $1.95/h. Cheapest, needs quant.
#   "H200"       — bf16, 1×H200.      $4.54/h. Tutorial default, fastest.
#   "L40S_TP2"   — bf16, 2×L40S TP=2. $3.90/h. Comparable to H200, slower.
GPU_PROFILE = "A100_80GB"

_PROFILES = {
    "A100_80GB": {"gpu": "A100-80GB:1", "n_gpu": 1, "extra": []},
    "L40S_FP8":  {"gpu": "L40S:1",      "n_gpu": 1, "extra": [
        "--quantization", "fp8",
        "--max-model-len", "8192",
        "--gpu-memory-utilization", "0.92",
    ]},
    "H200":      {"gpu": "H200:1",      "n_gpu": 1, "extra": []},
    "L40S_TP2":  {"gpu": "L40S:2",      "n_gpu": 2, "extra": []},
}
_profile = _PROFILES[GPU_PROFILE]
GPU = _profile["gpu"]
N_GPU = _profile["n_gpu"]
EXTRA_VLLM_FLAGS = _profile["extra"]


@app.function(
    image=vllm_image,
    gpu=GPU,
    scaledown_window=15 * MINUTES,  # how long should we stay up with no requests?
    timeout=10 * MINUTES,  # how long should we wait for container start?
    volumes={
        "/root/.cache/huggingface": hf_cache_vol,
        "/root/.cache/vllm": vllm_cache_vol,
    },
    # Gemma is a gated model on HuggingFace. Accept the license on the model
    # page, then: `modal secret create huggingface-secret HF_TOKEN=hf_xxx`
    secrets=[modal.Secret.from_name("huggingface-secret")],
)
@modal.concurrent(  # how many requests can one replica handle? tune carefully!
    max_inputs=100,
)
@modal.web_server(port=VLLM_PORT, startup_timeout=15 * MINUTES)
def serve():
    import json
    import subprocess

    cmd = [
        "vllm",
        "serve",
        MODEL_NAME,
        "--revision",
        MODEL_REVISION,
        "--served-model-name",
        MODEL_NAME,
        "llm",
        "--host",
        "0.0.0.0",
        "--port",
        str(VLLM_PORT),
        "--uvicorn-log-level=info",
        "--async-scheduling",
        # cua-monkey never needs Gemma 4's full 256K window. Capping context
        # shrinks the KV cache from tens of GB to a few GB and makes engine
        # init finish inside Modal's startup_timeout. Bump if you have very
        # long sessions (each agent turn is roughly 8–16K tokens).
        "--max-model-len", "32768",
    ]

    # enforce-eager disables both Torch compilation and CUDA graph capture
    # default is no-enforce-eager. see the --compilation-config flag for tighter control
    cmd += ["--enforce-eager" if FAST_BOOT else "--no-enforce-eager"]

    # assume multiple GPUs are for splitting up large matrix multiplications
    cmd += ["--tensor-parallel-size", str(N_GPU)]

    # GPU-profile-specific flags (quantization, max model length, etc.)
    cmd += EXTRA_VLLM_FLAGS

    # add model-specific configuration
    cmd += [
        # cua-monkey deviation: allow images (upstream sets all to 0)
        "--limit-mm-per-prompt",
        f"'{json.dumps({'image': IMAGES_PER_PROMPT, 'video': 0, 'audio': 0})}'",
        # enable reasoning and tool use
        "--enable-auto-tool-choice",
        "--reasoning-parser gemma4",
        "--tool-call-parser gemma4",
    ]

    print(*cmd)

    subprocess.Popen(" ".join(cmd), shell=True)


@app.local_entrypoint()
async def test(test_timeout=10 * MINUTES, content=None, twice=True):
    url = await serve.get_web_url.aio()

    system_prompt = {
        "role": "system",
        "content": "You are a pirate who can't help but drop sly reminders that he went to Harvard.",
    }
    if content is None:
        content = "Explain the singular value decomposition."

    messages = [  # OpenAI chat format
        system_prompt,
        {"role": "user", "content": content},
    ]

    async with aiohttp.ClientSession(base_url=url) as session:
        print(f"Running health check for server at {url}")
        async with session.get("/health", timeout=test_timeout - 1 * MINUTES) as resp:
            up = resp.status == 200
        assert up, f"Failed health check for server at {url}"
        print(f"Successful health check for server at {url}")

        print(f"Sending messages to {url}:", *messages, sep="\n\t")
        await _send_request(session, "llm", messages)
        if twice:
            messages[0]["content"] = "You are Jar Jar Binks."
            print(f"Sending messages to {url}:", *messages, sep="\n\t")
            await _send_request(session, "llm", messages)


async def _send_request(
    session: aiohttp.ClientSession, model: str, messages: list
) -> None:
    # `stream=True` tells an OpenAI-compatible backend to stream chunks
    payload: dict[str, Any] = {"messages": messages, "model": model, "stream": True}
    # explicitly enable thinking for this model
    payload["chat_template_kwargs"] = {"enable_thinking": True}

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}

    async with session.post(
        "/v1/chat/completions", json=payload, headers=headers
    ) as resp:
        async for raw in resp.content:
            resp.raise_for_status()
            # extract new content and stream it
            line = raw.decode().strip()
            if not line or line == "data: [DONE]":
                continue
            if line.startswith("data: "):  # SSE prefix
                line = line[len("data: ") :]

            chunk = json.loads(line)
            assert (
                chunk["object"] == "chat.completion.chunk"
            )  # or something went horribly wrong
            delta = chunk["choices"][0]["delta"]
            content = (
                delta.get("content")
                or delta.get("reasoning")
                or delta.get("reasoning_content")
            )
            if content:
                print(content, end="")
            else:
                print("\n", chunk)
    print()
