"""Per-operation rate-limit buckets, persisted across runs (mirrors x-cli limits.go).

Known: UserTweets-class ops = 50 req / 15 min. We seed from response headers
(x-rate-limit-*) and never guess harder than observed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class RateLimiter:
    def __init__(self, limits_path: Path, min_interval: float = 1.8, jitter: float = 0.3):
        self.path = limits_path
        self.min_interval = min_interval   # humanlike pacing between calls
        self.jitter = jitter
        self.buckets: dict[str, dict] = self._load()
        self._last_call = 0.0

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.buckets))
        tmp.replace(self.path)

    def wait(self, op: str) -> None:
        """Block until this op's window allows a call. Raises RuntimeError if
        the window is spent and reset is unreasonably far away."""
        # humanlike pacing first
        since = time.time() - self._last_call
        if since < self.min_interval:
            delay = max(0.05, self.min_interval - since
                        + self.jitter * (2 * (time.time() % 1) - 1))
            time.sleep(delay)
        self._last_call = time.time()

        b = self.buckets.get(op)
        if not b:
            return
        if b.get("remaining", 1) > 0:
            return
        reset = b.get("reset", 0)
        wait_s = reset - time.time()
        if wait_s <= 0:
            return
        if wait_s > 16 * 60:
            raise RuntimeError(f"{op}: rate window spent, resets in {wait_s/60:.0f} min")
        time.sleep(wait_s + 1)

    def observe(self, op: str, headers) -> None:
        """Update bucket from x-rate-limit-* response headers."""
        try:
            limit = headers.get("x-rate-limit-limit")
            remaining = headers.get("x-rate-limit-remaining")
            reset = headers.get("x-rate-limit-reset")
            if limit is None:
                return
            self.buckets[op] = {
                "limit": int(limit),
                "remaining": int(remaining) if remaining is not None else 0,
                "reset": int(reset) if reset else 0,
                "seen": int(time.time()),
            }
            self._save()
        except Exception:
            pass

    def status(self, op: str) -> str:
        b = self.buckets.get(op)
        if not b:
            return f"{op}: no window observed yet"
        return (
            f"{op}: {b['remaining']}/{b['limit']} left, "
            f"resets in {max(0, b['reset'] - time.time())/60:.1f} min"
        )
