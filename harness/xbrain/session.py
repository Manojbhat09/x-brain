"""Credential + identity storage.

Priority for USER_ID:  --user-id flag > X_USER_ID env > .env > creds.json > interactive prompt.
Priority for creds:    X_AUTH_TOKEN/X_CT0 env > .env > creds.json.
All files are 0600. No defaults are shipped.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

# --- paths ---------------------------------------------------------------

# Brain dir: $XBRAIN_DIR > $X_BRAIN_DIR > ./data > ~/.xbrain
def _brain_dir() -> Path:
    for k in ("XBRAIN_DIR", "X_BRAIN_DIR"):
        v = os.environ.get(k)
        if v:
            return Path(v).expanduser()
    # .env in cwd or harness/ may set it — load lazily via get_brain_dir()
    return Path.home() / ".xbrain"

def get_brain_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    # honour .env if present (without hard dependency on python-dotenv)
    _load_dotenv()
    for k in ("XBRAIN_DIR", "X_BRAIN_DIR", "BRAIN_DIR"):
        v = os.environ.get(k)
        if v:
            return Path(v).expanduser()
    return Path.home() / ".xbrain"

def _load_dotenv() -> None:
    """Minimal .env loader: sets os.environ for keys not already set."""
    for p in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env", Path.home() / ".xbrain" / ".env"):
        if not p.exists():
            continue
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception:
            pass
        break  # first .env wins

def creds_path(brain_dir: Path | None = None) -> Path:
    return (get_brain_dir(brain_dir) / "creds.json")

def config_path(brain_dir: Path | None = None) -> Path:
    return (get_brain_dir(brain_dir) / "config.json")

# --- creds ---------------------------------------------------------------

def load_creds(brain_dir: Path | None = None) -> dict | None:
    # env wins
    tok = os.environ.get("X_AUTH_TOKEN") or os.environ.get("AUTH_TOKEN")
    ct0 = os.environ.get("X_CT0") or os.environ.get("CT0")
    if tok and ct0:
        return {"auth_token": tok, "ct0": ct0}
    _load_dotenv()
    tok = os.environ.get("X_AUTH_TOKEN") or os.environ.get("AUTH_TOKEN")
    ct0 = os.environ.get("X_CT0") or os.environ.get("CT0")
    if tok and ct0:
        return {"auth_token": tok, "ct0": ct0}
    p = creds_path(brain_dir)
    if not p.exists():
        return None
    try:
        c = json.loads(p.read_text())
    except Exception:
        return None
    if c.get("auth_token") and c.get("ct0"):
        return c
    return None


def save_creds(auth_token: str, ct0: str, brain_dir: Path | None = None) -> Path:
    bd = get_brain_dir(brain_dir)
    bd.mkdir(parents=True, exist_ok=True)
    p = bd / "creds.json"
    p.write_text(json.dumps({"auth_token": auth_token, "ct0": ct0}, indent=2))
    os.chmod(p, 0o600)
    return p


# --- user id -------------------------------------------------------------

def _valid_user_id(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4,22}", s.strip()))

def load_user_id(brain_dir: Path | None = None, cli_value: str | None = None) -> Optional[str]:
    """Resolve target user id. cli_value > env > .env > config.json."""
    if cli_value and _valid_user_id(cli_value):
        return cli_value.strip()
    for k in ("X_USER_ID", "USER_ID", "TARGET_USER_ID"):
        v = os.environ.get(k)
        if v and _valid_user_id(v):
            return v.strip()
    _load_dotenv()
    for k in ("X_USER_ID", "USER_ID", "TARGET_USER_ID"):
        v = os.environ.get(k)
        if v and _valid_user_id(v):
            return v.strip()
    # config.json
    for p in (config_path(brain_dir), Path.cwd() / "config.json"):
        if p.exists():
            try:
                uid = json.loads(p.read_text()).get("user_id") or json.loads(p.read_text()).get("target_user_id")
                if uid and _valid_user_id(str(uid)):
                    return str(uid).strip()
            except Exception:
                pass
    # legacy creds.json may carry it
    p = creds_path(brain_dir)
    if p.exists():
        try:
            uid = json.loads(p.read_text()).get("user_id")
            if uid and _valid_user_id(str(uid)):
                return str(uid).strip()
        except Exception:
            pass
    return None


def save_user_id(user_id: str, brain_dir: Path | None = None) -> Path:
    if not _valid_user_id(user_id):
        raise ValueError("user_id must be 4-22 digits (numeric X user id)")
    bd = get_brain_dir(brain_dir)
    bd.mkdir(parents=True, exist_ok=True)
    p = bd / "config.json"
    cfg = {}
    if p.exists():
        try:
            cfg = json.loads(p.read_text())
        except Exception:
            cfg = {}
    cfg["user_id"] = user_id.strip()
    p.write_text(json.dumps(cfg, indent=2))
    os.chmod(p, 0o600)
    return p


def require_user_id(brain_dir: Path | None = None, cli_value: str | None = None) -> str:
    uid = load_user_id(brain_dir, cli_value)
    if uid:
        return uid
    # interactive prompt (only if tty)
    try:
        import sys
        if sys.stdin.isatty():
            raw = input("X user id (numeric, 4-22 digits) — find via https://x.com/i/api/graphql/... or https://tweeterid.com/ : ").strip()
            if _valid_user_id(raw):
                # offer to save
                save = input(f"Save {raw} to {config_path(brain_dir)} ? [Y/n] ").strip().lower()
                if save in ("", "y", "yes"):
                    save_user_id(raw, brain_dir)
                return raw
    except Exception:
        pass
    raise SystemExit(
        "No target user id configured.\n"
        "  Provide it via one of:\n"
        "    --user-id 123456789\n"
        "    X_USER_ID=123456789 in .env or environment\n"
        "    python3 xb.py auth --user-id 123456789 --auth-token ... --ct0 ...\n"
        "  Tip: copy .env.example to .env and fill it."
    )
