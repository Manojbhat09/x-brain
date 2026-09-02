# LEARNINGS — Defeating the InferX Vision Gateway 504
### How we made Nemotron-Nano-12B-v2-VL work for the reposts vision pipeline (2026-08-25)

## Symptom

Dedicated InferX endpoint (`NVIDIA-Nemotron-Nano-12B-v2-VL-FP8`, vLLM behind their gateway):
- **Short test prompt** → 200 OK in ~30s
- **Full production prompt** (longer context + JSON instructions + real image) → **504 Gateway Timeout**, consistently

Streaming (`"stream": true`) did **not** fix it. Neither did raising client timeouts.

## Root cause chain

1. **The gateway enforces a first-byte/target-response timeout (~60s).**
   Research (nginx/ALB/Cloudflare behavior): this is the wait for the *first byte*,
   not total request time — but a vision model emits no bytes until prefill finishes.
2. **Vision prefill is the killer.** The model tiles the image (Nemotron VL: 512×512
   tiles) and processes prompt+image before emitting token #1. A full-res tweet image
   + long prompt ⇒ prefill alone blows the budget.
3. **Standby cold start (~30s) eats half the budget.** The instance sleeps; the first
   request pays wake-up + prefill inside the same 60s window.
4. So: 30s wake + 40s prefill = 70s > 60s ⇒ 504 before a single token streams.

## The fixes (all four together)

| Fix | Effect |
|---|---|
| **Downscale images to ≤1024px JPEG q82 before base64** | Fewer vision tiles ⇒ prefill time drops massively. Biggest win. |
| **Warm-up ping** (5-token text request) if last call >3 min ago | Cold start happens *outside* the critical request. |
| **Immediate single warm-retry on 504** | First attempt warms the instance; retry succeeds in-budget. |
| **Streaming kept** | Still correct hygiene; helps on slow decode. |

Result: full production prompt → 200 OK in ~30s with detailed description +
verbatim OCR (including formulas on an RL cheat-sheet image).

## Debugging lessons (the meta-learnings)

1. **`pkill -f <pattern>` matches your own shell** if the pattern appears in the
   command line — it killed our launch commands silently, repeatedly, and even
   aborted a code patch mid-write. Use PID-based kills or wrapper scripts.
2. **Broad `except Exception: return None` buried a `NameError`** (missing
   `import base64`) and surfaced as a misleading "no images could be downloaded".
   Narrow excepts or log the exception.
3. **Return-contract mismatch (tuple vs string) between backend and worker** fell
   through silently to failing fallbacks — the real error was overwritten by the
   next backend's 429. Chain workers must log per-backend failures.
4. **Test the exact production path.** The simplified test prompt passed while the
   full prompt 504'd — every time. Fix verification must use the real prompt.
5. **Stale logs from dead processes mislead.** Check process liveness + start times
   (`ps -o pid,lstart,cmd`) before trusting a log tail.
6. **Server-side image fetch (vLLM `fetch_image_async`) is fragile** — send base64
   data URIs downloaded client-side instead. (Also what killed Kimi-VL earlier.)

## Applicable rule of thumb

> Serverless GPU endpoints (InferX, NIM, Lambda-style): budget = gateway
> first-byte timeout. Shrink inputs (images!), keep instances warm, stream,
> and retry-into-warmth. If prefill alone can't fit the window, no client-side
> timeout setting will save you.

## Addendum — the full bug chain behind "vision not progressing"

The 504 was only one of six stacked issues. Final fix list:
1. Gateway 504 → image downscale (≤1024px) + warm-up ping + warm-retry (see above)
2. `import base64` missing → NameError swallowed by broad except → "no images could be downloaded"
3. Key plumbing: VisionWorker passed the OpenRouter key to the InferX Nemotron backend → 401 → silent fallthrough. Backends need DIFFERENT keys — wire them explicitly.
4. Return-contract: kimi/nemotron branch expects `(answer, thinking)` tuple — backend returned str
5. Gateway 504 even when streaming → InferX gateway buffers; keep total time < ~60s: max_tokens 550 + tight prompt + warm instance
6. Truncated JSON (max_tokens cut mid-OCR) → regex salvage parser for description/ocr_text

Debugging rule that cracked it: instrument EVERY backend with timing + error prints
(`[nemotron] FAIL 3s: ...`) — six stacked silent failures were invisible until each
backend's outcome was printed per attempt.
