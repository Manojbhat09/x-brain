"""vision_understand — media comprehension for tweets/threads.

Uses OpenRouter free VL models (stealth/ox-alpha primary, gemma-4-31b fallback).
For videos, X's media_json carries the poster thumbnail — we analyze that and
mark modality=video so downstream knows it's a frame, not the full clip.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .backends import OpenRouterBackend, Capacity, BackendDown

VISION_PROMPT = """Analyze this social-media image. Context: {context}
Modality: {modality}.
1. Describe briefly (1-2 sentences). 2. OCR all visible text ("none" if no text). 3. 3-5 topical tags.
Reply ONLY: {{"description":"...","ocr_text":"...","tags":["..."]}}"""


class VisionWorker:
    def __init__(self, key_file: Path, store, quarantine_dir: Path,
                  inferx_key_file: Path | None = None):
        from .backends import NemotronVLBackend
        # FIX 401: tenant tn-f87uflojzk is single-GPU (1 Ready +1 Standby) -> 2 concurrent -> 401 on 2nd
        # Use 3 separate Nemotron backends with different keys for rotation + pacing, then glm fallback
        ix5 = Path.home() / "inferx5key"
        ix4 = Path.home() / "inferx4key"
        ix1 = Path.home() / "inferxkey"
        # primary chain: try nemotron with 5, then 4, then 1 (different ix keys) before falling back
        self.chain = []
        for name, kf in [("nemotron-5", ix5), ("nemotron-4", ix4), ("nemotron-1", ix1)]:
            if kf.exists():
                try:
                    self.chain.append((name, NemotronVLBackend(kf)))
                except Exception:
                    pass
        # keep at least one nemotron
        if not any(n.startswith("nemotron") for n,_ in self.chain):
            self.chain.append(("nemotron", NemotronVLBackend(ix5 if ix5.exists() else ix1)))
        self.chain.extend([
            ("glm-5.3-flash", OpenRouterBackend("z-ai/glm-5.3-flash", key_file)),
            ("gemma-4-31b", OpenRouterBackend("google/gemma-4-31b-it:free", key_file)),
        ])
        self.store = store
        self.qdir = quarantine_dir
        self.qdir.mkdir(parents=True, exist_ok=True)
        self.usage = {}

    def vision_understand(self, data: list[str], modality: str, context_prompt: str,
                          max_waits: int = 25) -> dict:
        """data: media URLs. modality: 'image'|'video'. Waits out free-pool 429s."""
        plural = "s" if len(data) > 1 else ""
        note = (" These are VIDEO POSTER FRAMES — describe the scene, note it is video content."
                if modality == "video" else "")
        prompt = VISION_PROMPT.format(context=context_prompt[:800],
                                      modality=modality, plural=plural) + note
        last_err = None
        waits = 0
        parse_fails = 0
        last_raw = ""
        while True:
            for name, be in self.chain:
                if not be.available():
                    print(f"    [{name}] cooling down")
                    continue
                t0 = time.time()
                try:
                    if name.startswith("nemotron") or name in ("kimi",):
                        answer, thinking = be.vision(prompt, data)
                        raw = answer
                        th = thinking
                    else:
                        raw = be.vision(prompt, data)
                        th = getattr(be, "last_thinking", "")
                    try:
                        result = _parse(raw)
                    except Exception:
                        # unstructured reply: count it, keep the raw text
                        parse_fails += 1
                        last_raw = raw
                        last_err = f"unstructured reply ({parse_fails}/3)"
                        print(f"    [{name}] PARSE-FAIL {time.time()-t0:.0f}s ({parse_fails}/3)")
                        if parse_fails >= 3:
                            self.usage[name] = self.usage.get(name, 0) + 1
                            return {"raw_response": last_raw[:4000],
                                    "error": "unstructured after 3 attempts"}
                        break  # next backend
                    if th:
                        result["thinking"] = th
                    self.usage[name] = self.usage.get(name, 0) + 1
                    print(f"    [{name}] OK in {time.time()-t0:.0f}s")
                    return result
                except (Capacity, BackendDown) as e:
                    last_err = str(e)
                    print(f"    [{name}] FAIL {time.time()-t0:.0f}s: {str(e)[:80]}")
                    continue
            waits += 1
            if waits > max_waits:
                if last_raw:
                    return {"raw_response": last_raw[:4000], "error": f"waits exhausted: {last_err}"}
                raise RuntimeError(f"all vision backends failed after {waits} waits: {last_err}")
            time.sleep(60)  # free-pool congestion: wait, don't waste

    def run(self, limit: int = 0, log=print, workers: int = 1) -> dict:
        import threading, queue
        rows = self.store.pending_vision(limit=limit or 100000)
        stats = {"done": 0, "failed": 0}
        lock = threading.Lock()
        q: queue.Queue = queue.Queue()
        for r in rows:
            q.put(r)

        def process_one_row(row):
            tid, media_json, mod, text = row
            media = json.loads(media_json or "[]")
            if not media:
                return True
            try:
                result = self.vision_understand(media, mod, text or "")
                self.store.save_media_understanding(tid, result)
                return True
            except RuntimeError as e:
                log(f"vision fail {tid}: {str(e)[:80]}")
                return False

        def worker():
            while True:
                try:
                    row = q.get_nowait()
                except queue.Empty:
                    return
                ok = process_one_row(row)
                with lock:
                    stats["done" if ok else "failed"] += 1
                    n = stats["done"] + stats["failed"]
                    if stats["done"] and stats["done"] % 25 == 0:
                        log(f"vision: {stats['done']} done | {self.usage}")
                    if stats["failed"] > 80:
                        log("too many failures — stopping (resumable)")
                        # drain queue to stop other threads
                        while True:
                            try: q.get_nowait()
                            except queue.Empty: return
                q.task_done()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return stats


def _parse(raw: str) -> dict:
    i = raw.find("{")
    if i == -1:
        raise ValueError("no JSON in vision reply")
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[i:])
    except json.JSONDecodeError:
        # truncated JSON (max_tokens cut mid-string): salvage fields by regex
        def grab(field):
            m = re.search(r'"' + field + r'"\s*:\s*"(.*?)"(?:\s*[,}])|"' + field +
                          r'"\s*:\s*"(.*)$', raw[i:], re.S)
            if not m:
                return ""
            v = m.group(1) if m.group(1) is not None else m.group(2)
            return v.replace("\\n", " ").strip()
        obj = {"description": grab("description"),
               "ocr_text": grab("ocr_text"),
               "tags": re.findall(r'"([^"]{3,40})"', raw[i:].split("tags")[1])[:6] if "tags" in raw[i:] else []}
        if not obj["description"] and not obj["ocr_text"]:
            raise
    return {"description": obj.get("description", ""),
            "ocr_text": obj.get("ocr_text", ""),
            "tags": obj.get("tags", []),
            "model": "vision"}
