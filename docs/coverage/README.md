# Example coverage run output

Captured from a real `./coverage.sh` run against the bundled demo site
(`benchmark/coverage-target/`).

| File | What it is |
|---|---|
| `terminal.log` | Full stdout / stderr of the run |
| `steps.mp4` | Timelapse rendered via `--video` (one frame per agent step, captioned with the action) |
| `audit/` | Per-step screenshots, accessibility tree dumps, LLM I/O |

To produce equivalent output yourself:

```bash
./coverage.sh --minutes 5 --output ./docs/coverage --video
```
