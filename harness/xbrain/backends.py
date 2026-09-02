"""LLM backends — InferX (free, capacity-limited) primary, Ollama fallback.

InferX: OpenAI-compatible /chat/completions, Qwen3.8-27B-FP8 (reasoning model —
strip <think> blocks). Conservative pacing + circuit breaker: capacity/429/5xx
errors open a cooldown; worker fails over to next backend. Never hard-fails.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import requests

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def strip_think(text: str) -> str:
    """Qwen reasoning models emit <think>...</think>; this endpoint sometimes
    strips only the opening tag, leaving reasoning + orphan '</think>'."""
    text = THINK_RE.sub("", text)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()



def inline_log(model: str, status: str, prompt: str = "", response: str = "", error: str = "", usage=None):
    """Append every call (success or failure) to ~/x-brain/inline_ox.log for manual inspection."""
    try:
        from pathlib import Path as _P
        import datetime as _dt
        log = _P.home() / "x-brain" / "inline_ox.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        u = f" p_tok={usage.get('prompt_tokens','?')} c_tok={usage.get('completion_tokens','?')}" if usage else ""
        with open(log, "a") as f:
            f.write(f"[{ts}] [{model}] {status}{u}\n")
            if error:
                f.write(f"ERROR: {error[:300]}\n")
            if prompt:
                f.write(f"PROMPT: {str(prompt)[:500]}\n")
            if response:
                f.write(f"RESPONSE: {str(response)[:600]}\n")
            f.write("---\n")
    except Exception:
        pass


def call_log(event: str, **kw) -> None:
    """Single-line structured event log for the drain: ~/x-brain/drain_calls.log.
    Every LLM call attempt, route decision, and failure lands here for tail -f."""
    try:
        import datetime as _dt
        from pathlib import Path as _P
        f = _P.home() / "x-brain" / "drain_calls.log"
        parts = " ".join(f"{k}={v}" for k, v in kw.items())
        with open(f, "a") as fh:
            fh.write(f"[{_dt.datetime.now().strftime('%m-%d %H:%M:%S')}] {event} {parts}\n")
    except Exception:
        pass


class BackendDown(Exception):
    pass


class Capacity(Exception):
    """Backend at capacity / rate-limited — try next backend or cooldown."""


class InferXBackend:
    def __init__(self, key_file: Path, model: str = "Qwen3.8-27B-FP8",
                 base_url: str = "https://model.inferx.net/endpoints/v1",
                 min_interval: float = 1.0):
        self.key = key_file.read_text().strip()
        self.model = model
        self.url = f"{base_url}/chat/completions"
        self.min_interval = min_interval
        self._last = 0.0
        self.cooldown_until = 0.0
        self.last_thinking = ""
        self.http = requests.Session()
        # Disable retries for connection errors to fail fast
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def name(self) -> str:
        return "inferx"

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def _pace(self):
        since = time.time() - self._last
        if since < self.min_interval:
            time.sleep(self.min_interval - since)
        self._last = time.time()

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0) -> dict:
        if not self.available():
            raise BackendDown(f"inferx cooling down {self.cooldown_until - time.time():.0f}s more")
        self._pace()
        try:
            r = self.http.post(self.url, headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            }, json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 2048,
            }, timeout=(5, 60))
        except requests.RequestException as e:
            inline_log("inferx:" + self.model, "FAIL", prompt, error=str(e)[:150])
            self._trip(120)
            raise BackendDown(f"inferx unreachable: {e}")
        if r.status_code in (429, 503, 502, 529):
            inline_log("inferx:" + self.model, "FAIL", prompt, error=f"HTTP {r.status_code}")
            self._trip(300 if r.status_code == 429 else 180)
            raise Capacity(f"inferx {r.status_code}")
        if r.status_code == 401:
            inline_log("inferx:" + self.model, "FAIL", prompt, error="401 key rejected")
            raise BackendDown("inferx key rejected (401)")
        if r.status_code == 404:
            self._trip(300)
            raise Capacity(f"openrouter 404 {self.model}: {r.text[:80]}")
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        # Capture reasoning field (Qwen3.8-27B-FP8 puts thinking in message.reasoning)
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        clean, thinking = split_thinking(content)
        if reasoning and not thinking:
            thinking = reasoning
        self.last_thinking = thinking
        try:
            result = _loads_loose(clean)
        except Exception:
            inline_log("inferx:" + self.model, "PARSE-FAIL", prompt,
                       response=clean[:300], error="no JSON (reasoning truncated at token limit?)")
            raise
        inline_log("inferx:" + self.model, "OK", prompt, response=clean,
                   usage={"prompt_tokens": "?", "completion_tokens": "?"})
        return result

    def _trip(self, seconds: float):
        self.cooldown_until = time.time() + seconds


def _loads_loose(content: str) -> dict:
    """Parse JSON from a model reply; tolerate fences/prose/trailing objects."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    i = content.find("{")
    if i == -1:
        raise json.JSONDecodeError("no object found", content, 0)
    obj, _ = json.JSONDecoder().raw_decode(content[i:])
    if isinstance(obj, dict):
        return obj
    raise json.JSONDecodeError("not an object", content, i)


SLOW_MODELS_TIMEOUT = 400  # CPU-straddle models: granite-4.2(4.4G+KV), qwen3-thinking, ornith, r1:7b, 8B LFM MoEs

def _timeout_for(model: str | None) -> int:
    m = model or ""
    if any(k in m for k in ("granite-4.2", "qwen3-4b-thinking", "ornith",
                            "deepseek-r1:7b", "small-8b")):
        return SLOW_MODELS_TIMEOUT
    return 150


class OllamaBackend:
    per_card_model = True

    def __init__(self, model: str = "extractor", base: str = "http://localhost:11434"):
        self.model = model
        self.base = base
        self.cooldown_until = 0.0
        self.http = requests.Session()
        # Disable retries for connection errors to fail fast
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def name(self) -> str:
        return "ollama"

    def available(self) -> bool:
        return True

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0,
             model: str | None = None) -> dict:
        m = self.model if model is None else model
        t0 = time.time()
        try:
            # STREAMING: (a) client disconnect cancels generation server-side —
            # non-streaming calls abandoned on timeout keep generating as zombies
            # that serialize the GPU (observed 31-min /api/generate); (b) flowing
            # chunks reset the read timer, so timeouts only fire on true hangs.
            r = self.http.post(f"{self.base}/api/chat", json={
                "model": m,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "think": True,
                "options": {"temperature": temperature, "num_ctx": 8192, "seed": 42},
                "keep_alive": "60m",
            }, timeout=(30, _timeout_for(m)), stream=True)
        except requests.RequestException as e:
            call_log("ollama-FAIL", model=m, err=type(e).__name__, detail=str(e)[:120],
                     t=round(time.time() - t0, 1))
            self._trip(300)
            raise BackendDown(f"ollama unreachable: {e}")
        if r.status_code == 404:
            call_log("ollama-FAIL", model=m, http=404, t=round(time.time() - t0, 1))
            self._trip(600)
            raise BackendDown("ollama model missing (404)")
        r.raise_for_status()
        content_parts, think_parts = [], []
        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                msg = chunk.get("message") or {}
                if msg.get("content"):
                    content_parts.append(msg["content"])
                th = msg.get("thinking")
                if th:
                    think_parts.append(th)
                if chunk.get("done"):
                    break
        except requests.RequestException as e:
            call_log("ollama-FAIL", model=m, err="stream-" + type(e).__name__,
                     detail=str(e)[:100], t=round(time.time() - t0, 1))
            r.close()  # cancel server-side generation (no zombie)
            self._trip(60)
            raise Capacity(f"ollama stream died: {e}")
        r.close()
        content = "".join(content_parts)
        thinking = "".join(think_parts)
        clean, think2 = split_thinking(content)
        if thinking and not think2:
            think2 = thinking
        self.last_thinking = think2
        if not clean.strip():
            call_log("ollama-EMPTY", model=m, t=round(time.time() - t0, 1))
            raise Capacity("ollama empty content")
        call_log("ollama-OK", model=m, chars=len(clean), think=len(think2),
                 t=round(time.time() - t0, 1))
        return _loads_loose(clean)

    def _trip(self, seconds: float):
        self.cooldown_until = time.time() + seconds


class LiteLLMBackend:
    """OpenAI-compatible proxy (litellm) in front of the local ollama models.

    Exposes per-card model selection (self_relevance -> rel_model) via the
    `model` chat kwarg, mirroring OllamaBackend. cat: start_litellm.sh + litellm_drain.yaml.
    """
    per_card_model = True

    def __init__(self, base: str = "http://127.0.0.1:4000", model: str = "nemotron3-nano",
                 rel_model: str = "granite-4.2-3b-q8", api_key: str = "sk-local-xbrain",
                 timeout: int = 150):
        self.base = base
        self.model = model
        self.rel_model = rel_model
        self.api_key = api_key
        self.timeout = timeout
        self.cooldown_until = 0.0
        self.last_thinking = ""
        self.http = requests.Session()
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def name(self) -> str:
        return "litellm"

    def available(self) -> bool:
        return True

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0,
             model: str | None = None) -> dict:
        body = {
            "model": model or self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "stream": False,
        }
        m = model or self.model
        t0 = time.time()
        try:
            r = self.http.post(
                f"{self.base}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            call_log("litellm-FAIL", model=m, err=type(e).__name__, detail=str(e)[:120],
                     t=round(time.time() - t0, 1))
            self._trip(30)
            raise BackendDown(f"litellm unreachable: {e}")
        if r.status_code in (400, 401, 403):
            call_log("litellm-FAIL", model=m, http=r.status_code, detail=(r.text or "")[:160],
                     t=round(time.time() - t0, 1))
            raise BackendDown(f"litellm {r.status_code}: {(r.text or '')[:200]}")
        if r.status_code in (429, 500, 502, 503, 504):
            call_log("litellm-FAIL", model=m, http=r.status_code, t=round(time.time() - t0, 1))
            self._trip(45)
            raise Capacity(f"litellm {r.status_code}")
        r.raise_for_status()
        try:
            content = r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            call_log("litellm-FAIL", model=m, err="bad-payload", detail=str(e)[:120],
                     t=round(time.time() - t0, 1))
            raise Capacity(f"litellm bad payload: {e}")
        if not content.strip():
            call_log("litellm-EMPTY", model=m, t=round(time.time() - t0, 1))
            raise Capacity("litellm empty content")
        call_log("litellm-OK", model=m, chars=len(content), t=round(time.time() - t0, 1))
        return _loads_loose(content)

    def _trip(self, seconds: float):
        self.cooldown_until = time.time() + seconds


def _key_from_env_or_file(env_names: list[str], file_names: list[str]) -> Path | str | None:
    """Return a Path or raw key string: env var wins, else first existing file."""
    import os as _os
    for k in env_names:
        v = _os.environ.get(k)
        if v and v.strip():
            # write to a temp Path-like wrapper: return the string directly
            return v.strip()
    for kf in file_names:
        for base in (Path.home(), Path.cwd(), Path(__file__).resolve().parents[2]):
            p = base / kf if not str(kf).startswith("/") else Path(kf)
            if p.exists():
                return p
            # also check $XBRAIN_DIR
            bd = _os.environ.get("XBRAIN_DIR") or _os.environ.get("X_BRAIN_DIR")
            if bd and (Path(bd) / kf).exists():
                return Path(bd) / kf
    return None

def _make_inferx(model: str, min_interval: float = 0.5):
    src = _key_from_env_or_file(
        ["INFERX_API_KEY", "INFERX_KEY", "X_INFERX_KEY"],
        ["inferx4key", "inferx3key", "inferx2key", "inferxkey"])
    if src is None:
        return None
    # InferXBackend accepts Path or string key via key_file
    if isinstance(src, str) and "\n" not in src and len(src) < 500 and not Path(src).exists():
        # raw key: wrap in a temp file via a tiny shim — InferXBackend reads .read_text()
        # so create an ephemeral Path in the brain dir
        import tempfile, os as _os
        bd = _os.environ.get("XBRAIN_DIR") or str(Path.home() / ".xbrain")
        d = Path(bd); d.mkdir(parents=True, exist_ok=True)
        p = d / f".inferx_{model.replace('/','_')}.key"
        if not p.exists():
            p.write_text(src); os.chmod(p, 0o600)
        src = p
    return InferXBackend(src, model=model, min_interval=min_interval)

def default_backends(key_file: Path | None = None) -> list:
    """InferX glm-5.3-flash (thinking, 839 avg) primary, then Qwen3-Coder-Next-FP8, then Ollama extractor."""
    backends = []
    b = _make_inferx("glm-5.3-flash", 0.5)
    if b: backends.append(b)
    b2 = _make_inferx("Qwen3-Coder-Next-FP8", 0.5)
    if b2 and (not backends or str(b2.key) != str(backends[0].key) or b2.model != backends[0].model):
        backends.append(b2)
    # Ollama deepseek-r1:1.5b as final fallback (thinking in code blocks)
    backends.append(OllamaBackend())
    return backends


class OpenRouterBackend:
    """OpenRouter free-tier models. chat() = text; vision() = image URLs."""

    def __init__(self, model: str, key_file: Path, min_interval: float = 1.5):
        self.key = key_file.read_text().strip()
        self.model = model
        self.min_interval = min_interval
        self._last = 0.0
        self.cooldown_until = 0.0
        self.last_thinking = ""
        self.http = requests.Session()
        # Disable retries for connection errors to fail fast
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def name(self) -> str:
        return f"or:{self.model.split('/')[-1]}"

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def _pace(self):
        since = time.time() - self._last
        if since < self.min_interval:
            time.sleep(self.min_interval - since)
        self._last = time.time()

    def _post(self, messages, max_tokens=4000):
        self._pace()
        try:
            # Use higher tokens for reasoning models (nemotron returns reasoning field)
            r = self.http.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "max_tokens": max_tokens,
                      "messages": messages}, timeout=180)
        except requests.RequestException as e:
            self._trip(120)
            raise BackendDown(f"openrouter unreachable: {e}")
        if r.status_code in (429, 502, 503):
            self._trip(180)
            raise Capacity(f"openrouter {r.status_code}")
        if r.status_code == 401:
            raise BackendDown("openrouter key rejected")
        r.raise_for_status()
        d = r.json()
        if "error" in d:  # provider-level error wrapped in 200
            self._trip(120)
            raise Capacity(str(d["error"].get("message", ""))[:80])
        content = d["choices"][0]["message"].get("content") or ""
        # inline opencode prompt/response log for ox-alpha (free tier visibility)
        if "ox-alpha" in self.model:
            try:
                from pathlib import Path as _P
                _log = _P.home() / "x-brain" / "inline_ox.log"
                _log.parent.mkdir(parents=True, exist_ok=True)
                with open(_log, "a") as _f:
                    _f.write(f"[{self.model}] prompt_tokens={d.get('usage',{}).get('prompt_tokens','?')} "
                             f"completion={d.get('usage',{}).get('completion_tokens','?')}\n")
                    _f.write(f"PROMPT: {str(messages)[:600]}\n")
                    _f.write(f"RESPONSE: {content[:800]}\n---\n")
            except Exception:
                pass
        return content

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0) -> dict:
        content = self._post([{"role": "user", "content": prompt}])
        clean, thinking = split_thinking(content)
        self.last_thinking = thinking
        return _loads_loose(clean)

    def vision(self, prompt: str, image_urls: list[str], max_tokens=500) -> str:
        content = [{"type": "text", "text": prompt}]
        for u in image_urls[:4]:  # cap 4 images per call
            if not isinstance(u, str) or "[" in u[:2] or not u.startswith("http"):
                continue  # defensive: never send malformed URLs
            content.append({"type": "image_url", "image_url": {"url": u}})
        raw = self._post([{"role": "user", "content": content}], max_tokens)
        clean, thinking = split_thinking(raw)
        self.last_thinking = thinking
        return clean

    def _trip(self, seconds: float):
        self.cooldown_until = time.time() + seconds


THINK_OPEN = ("<think>", "◁think▷", "🤔", "```thinking", "<thinking>")
THINK_CLOSE = ("</think>", "◁/think▷", "/thinking", "```", "</thinking>")

def split_thinking(content: str) -> tuple[str, str]:
    """Return (clean_answer, thinking). Handles ◁think▷, 思考, **Reasoning:**, and code-block JSON."""
    import json, re
    thinking = ""
    # 1. Code-block JSON at END: reasoning text, then ```json {...}``` (check first)
    code_blocks = list(re.finditer(r"```(?:json)?\s*\n(.*?)\n```", content, re.DOTALL))
    if code_blocks:
        last_block = code_blocks[-1]
        try:
            candidate = last_block.group(1).strip()
            json.loads(candidate)
            thinking_text = content[:last_block.start()].strip()
            return candidate, thinking_text
        except json.JSONDecodeError:
            pass
    # 2. Explicit thinking tags: ◁think▷, 思考, ```thinking, <thinking>
    for o, c in zip(THINK_OPEN, THINK_CLOSE):
        if o in content:
            parts = content.split(o, 1)
            after = parts[1]
            thinking = after.split(c)[0] if c in after else after
            content = parts[0] + after.split(c, 1)[1] if c in after else parts[0]
            return content.strip(), thinking.strip()
    # 3. **Reasoning:** suffix (OpenRouter nemotron)
    if "**Reasoning:" in content:
        parts = content.split("**Reasoning:", 1)
        return parts[0].strip(), parts[1].strip()
    # 4. Reasoning: prefix (some models)
    if "Reasoning:" in content and content.strip().endswith("}"):
        idx = content.find("Reasoning:")
        if idx > content.find("}"):
            return content[:content.find("}", idx-200)+1].strip(), content[idx:].strip()
    return content.strip(), thinking.strip()
class KimiVLBackend:
    """InferX dedicated Kimi-VL-A3B-Thinking endpoint (vision + thinking)."""

    def __init__(self, key_file: Path, model: str = "moonshotai/Kimi-VL-A3B-Thinking-2506",
                 base: str = "https://model.inferx.net/funccall/tn-f87uflojzk/default/"
                             "Kimi-VL-A3B-Thinking-2506/v1"):
        self.key = key_file.read_text().strip()
        self.model = model
        self.url = f"{base}/chat/completions"
        self.cooldown_until = 0.0
        self.http = requests.Session()
        # Disable retries for connection errors to fail fast
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def name(self) -> str:
        return "kimi-vl"

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def vision(self, prompt: str, image_urls: list[str], max_tokens=900) -> tuple[str, str]:
        """Returns (answer, thinking) — thinking preserved, never discarded."""
        if not self.available():
            raise BackendDown("kimi-vl cooling down")
        content = [{"type": "text", "text": prompt}]
        for u in image_urls[:4]:
            content.append({"type": "image_url", "image_url": {"url": u}})
        try:
            r = self.http.post(self.url, headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": content}]}, timeout=180)
        except requests.RequestException as e:
            self.cooldown_until = time.time() + 60
            raise BackendDown(f"kimi-vl unreachable: {e}")
        if r.status_code in (429, 503):
            self.cooldown_until = time.time() + 120
            raise Capacity(f"kimi-vl {r.status_code}")
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        raw = msg.get("content") or ""
        answer, thinking = split_thinking(raw)
        if not thinking:
            thinking = str(msg.get("reasoning") or "")[:4000]
        return answer, thinking

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0) -> dict:
        answer, _ = self.vision(prompt, [], max_tokens=600)
        return _loads_loose(answer)


class NemotronVLBackend:
    """InferX dedicated NVIDIA Nemotron Nano 12B v2 VL (vision, base64 images)."""

    def __init__(self, key_file: Path,
                  model: str = "nvidia/NVIDIA-Nemotron-Nano-12B-v2-VL-FP8",
                  base: str = "https://model.inferx.net/funccall/tn-f87uflojzk/default/NVIDIA-Nemotron-Nano-12B-v2-VL-FP8/v1"):
        self.key = key_file.read_text().strip()
        self.model = model
        self.url = f"{base}/chat/completions"
        self.cooldown_until = 0.0
        self.last_thinking = ""
        self.http = requests.Session()
        # Disable retries for connection errors to fail fast
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)
        self._img_cache: dict[str, str] = {}
        import threading as _th
        self._lock = _th.Lock()

    def name(self) -> str:
        return "nemotron-vl"

    def _warm(self):
        """Ping the instance awake — a cold start eats the gateway's first-byte budget."""
        import time as _t
        now = _t.time()
        if now - getattr(self, "_last_call", 0) < 180:
            return
        try:
            self.http.post(self.url, headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "max_tokens": 5,
                      "messages": [{"role": "user", "content": "hi"}]}, timeout=90)
        except Exception:
            pass
        self._last_call = now

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def _pace(self):
        # FIX 401: single-GPU tenant can't handle 2 concurrent; pace to 1 req/3s
        now = time.time()
        last = getattr(self, "_last_vision", 0)
        if now - last < 3.0:
            time.sleep(3.0 - (now - last))
        self._last_vision = time.time()

    def _data_uri(self, url: str, max_dim: int = 4096) -> str | None:  # full quality (was 768)
        """Download → downscale to max_dim (cuts vision prefill time) → base64."""
        if url in self._img_cache:
            return self._img_cache[url]
        try:
            r = self.http.get(url, timeout=30)
            r.raise_for_status()
            data = r.content
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=78)
            b64 = base64.b64encode(buf.getvalue()).decode()
            uri = f"data:image/jpeg;base64,{b64}"
            if len(self._img_cache) > 500:
                self._img_cache.clear()
            self._img_cache[url] = uri
            return uri
        except Exception:
            return None

    def vision(self, prompt: str, image_urls: list[str], max_tokens=550) -> tuple[str, str]:
        """Returns (answer, thinking). Streams so the gateway never idle-504s."""
        if not self.available():
            raise BackendDown("nemotron cooling down")
        self._pace()
        self._warm()
        content = [{"type": "text", "text": prompt}]
        for u in image_urls[:4]:
            if isinstance(u, str) and u.startswith("http"):
                content.append({"type": "image_url", "image_url": {"url": u}})
        if len(content) == 1:
            raise Capacity("no valid image URLs")
        # streaming + inline retry: transient write-timeouts get a fresh connection
        # lock Session - not thread-safe with 2 concurrent workers
        r = None
        for attempt in range(3):
            try:
                with self._lock:
                    r = self.http.post(self.url,
                        headers={"Authorization": f"Bearer {self.key}"},
                        json={"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                              "stream": True, "messages": [{"role": "user", "content": content}]},
                        timeout=(60, 240), stream=True)
                break
            except requests.RequestException as e:
                if attempt < 2:
                    time.sleep(3)
                    continue
                self.cooldown_until = time.time() + 60
                raise BackendDown(f"nemotron unreachable after retries: {e}")
        if r is None:
            raise BackendDown("nemotron: no response")
        if r.status_code == 504:
            self._consec_504 = getattr(self, "_consec_504", 0) + 1
            if self._consec_504 >= 3:
                # instance wedged: back off hard instead of hammering
                self.cooldown_until = time.time() + 600
                self._consec_504 = 0
                raise Capacity("nemotron wedged (3x504) — backing off 10 min")
            # gateway first-byte budget blown (cold start + prefill): warm, then retry once
            self._warm()
            try:
                r = self.http.post(self.url,
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                          "stream": True, "messages": [{"role": "user", "content": content}]},
                    timeout=(60, 240), stream=True)
            except requests.RequestException as e:
                self.cooldown_until = time.time() + 60
                raise BackendDown(f"nemotron 504-retry unreachable: {e}")
            if r.status_code >= 400:
                self.cooldown_until = time.time() + 60
                raise Capacity(f"nemotron {r.status_code} after warm retry")
        elif r.status_code in (429, 502, 503):
            self.cooldown_until = time.time() + 120
            raise Capacity(f"nemotron {r.status_code}")
        elif r.status_code in (401, 403):
            self.cooldown_until = time.time() + 60
            raise BackendDown(f"nemotron {r.status_code} auth")
        r.raise_for_status()
        parts = []
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                delta = json.loads(payload)["choices"][0].get("delta", {})
                parts.append(delta.get("content") or "")
            except Exception:
                continue
        raw = "".join(parts)
        if not raw.strip():
            raise Capacity("nemotron empty stream")
        clean, thinking = split_thinking(raw)
        self.last_thinking = thinking or ""
        self._consec_504 = 0
        return clean, self.last_thinking

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0) -> dict:
        return _loads_loose(self.vision(prompt, [])[0])


class OpencodeZenBackend:
    """Opencode Zen's Ox Alpha Free (x-preview-f-free) — DISTINCT from OpenRouter's stealth/ox-alpha.
    Zero-retention, separate quota. Requires `opencode auth login` (stores token in ~/.local/share/opencode/auth.json)."""

    def __init__(self, model: str = "x-preview-f-free",
                 base: str = "https://opencode.ai/zen/v1"):
        import json as _j
        auth = _j.loads((Path.home() / ".local/share/opencode/auth.json").read_text())
        # Zen token is under "opencode" or "zen" key after `opencode auth login`
        tok = (auth.get("opencode", {}).get("key") or auth.get("zen", {}).get("key") or
               auth.get("opencode", {}).get("token") or "")
        if not tok:
            raise BackendDown("opencode zen not authenticated — run: opencode auth login")
        self.key = tok
        self.model = model
        self.url = f"{base}/chat/completions"
        self.cooldown_until = 0.0
        self.last_thinking = ""
        self.http = requests.Session()
        # Disable retries for connection errors to fail fast
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def name(self) -> str:
        return "zen:ox-alpha"

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0) -> dict:
        if not self.available():
            raise BackendDown("zen cooling down")
        try:
            r = self.http.post(self.url, headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "messages": [{"role": "user", "content": prompt}],
                      "temperature": temperature}, timeout=120)
        except requests.RequestException as e:
            inline_log("zen:" + self.model, "FAIL", prompt, error=str(e)[:150])
            self.cooldown_until = time.time() + 60
            raise BackendDown(f"zen unreachable: {e}")
        if r.status_code in (429, 503):
            inline_log("zen:" + self.model, "FAIL", prompt, error=f"HTTP {r.status_code}")
            self.cooldown_until = time.time() + 180
            raise Capacity(f"zen {r.status_code}")
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        clean, thinking = split_thinking(content)
        # Zen puts thinking in reasoning_content (verified live 2026-08-26)
        rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if rc and not thinking:
            thinking = str(rc)
        self.last_thinking = thinking
        inline_log("zen:" + self.model, "OK", prompt, response=clean,
                   usage={"reasoning_chars": len(thinking)})
        return _loads_loose(clean)


class OpencodeZenBackend:
    """Opencode Zen's Ox Alpha Free (x-preview-f-free) — DISTINCT from OpenRouter's stealth/ox-alpha.
    Zero-retention, separate quota. Requires `opencode auth login` (stores token in ~/.local/share/opencode/auth.json)."""

    def __init__(self, model: str = "x-preview-f-free",
                 base: str = "https://opencode.ai/zen/v1"):
        import json as _j
        auth = _j.loads((Path.home() / ".local/share/opencode/auth.json").read_text())
        tok = (auth.get("opencode", {}).get("key") or auth.get("zen", {}).get("key") or
               auth.get("opencode", {}).get("token") or "")
        if not tok:
            raise BackendDown("opencode zen not authenticated — run: opencode auth login")
        self.key = tok
        self.model = model
        self.url = f"{base}/chat/completions"
        self.cooldown_until = 0.0
        self.last_thinking = ""
        self.http = requests.Session()
        # Disable retries for connection errors to fail fast
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def name(self) -> str:
        return "zen:ox-alpha"

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def chat(self, prompt: str, schema: dict, temperature: float = 0.0) -> dict:
        if not self.available():
            raise BackendDown("zen cooling down")
        try:
            r = self.http.post(self.url, headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "messages": [{"role": "user", "content": prompt}],
                      "temperature": temperature}, timeout=120)
        except requests.RequestException as e:
            inline_log("zen:" + self.model, "FAIL", prompt, error=str(e)[:150])
            self.cooldown_until = time.time() + 60
            raise BackendDown(f"zen unreachable: {e}")
        if r.status_code in (429, 503):
            inline_log("zen:" + self.model, "FAIL", prompt, error=f"HTTP {r.status_code}")
            self.cooldown_until = time.time() + 180
            raise Capacity(f"zen {r.status_code}")
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        clean, thinking = split_thinking(content)
        # Zen puts thinking in reasoning_content (verified live 2026-08-26)
        rc = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if rc and not thinking:
            thinking = str(rc)
        self.last_thinking = thinking
        inline_log("zen:" + self.model, "OK", prompt, response=clean,
                   usage={"reasoning_chars": len(thinking)})
        return _loads_loose(clean)
