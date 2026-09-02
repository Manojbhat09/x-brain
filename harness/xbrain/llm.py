"""LLM worker — Gemma e4b via Ollama. Slot-filling only; harness validates.

Per x-brain-small-llm-harness.md: temp 0, seed 42, num_ctx 4096, JSON-schema
constrained decoding, retry <=2 with error feedback, then quarantine (never drop).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from .store import Store
from .backends import default_backends

OLLAMA = "http://localhost:11434"
MODEL = "extractor"  # ollama tag; `ollama cp gemma4:e4b extractor`

TOPICS = ["ai-research", "ai-industry", "markets", "startups", "infra",
          "robots", "crypto", "geopolitics", "culture", "other"]
REASONS = ["technical-insight", "market-signal", "humor",
           "career-founders", "evidence-of-claim", "aesthetic", "other"]

SCHEMAS = {
    "tag_topic": {"type": "object", "properties": {
        "topic": {"enum": TOPICS}}, "required": ["topic"]},
    "extract_entities": {"type": "object", "properties": {
        "entities": {"type": "array", "items": {"type": "string"}}},
        "required": ["entities"]},
    "self_relevance": {"type": "object", "properties": {
        "reason": {"enum": REASONS}, "confidence": {"enum": ["high", "medium", "low"]}},
        "required": ["reason", "confidence"]},
    "summarize": {"type": "object", "properties": {
        "summary": {"type": "string"}, "sentiment": {
            "enum": ["positive", "negative", "mixed", "neutral"]}},
        "required": ["summary", "sentiment"]},
}

PROMPTS = {
    "tag_topic": (
        "You label tech/social-media posts with exactly one topic.\n"
        "Rules:\n- Choose ONLY from the allowed list.\n- If nothing fits, use \"other\".\n"
        "- Do not explain.\nAllowed: " + ", ".join(TOPICS) +
        "\nExample input:  \"Qwen3 35B runs at 39 tok/s on RTX 4060\"\n"
        "Example output: {\"topic\":\"infra\"}\nLINK CONTEXT: {{CONTEXT}}\nPOST:\n{{TEXT}}"),
    "extract_entities": (
        "Extract named entities from the post.\n"
        "Rules:\n- People/companies/products/papers/tickers/countries only.\n"
        "- Use canonical names (\"Halliburton\" not \"$HAL\" alone).\n"
        "- Empty list if none. Do not invent.\n"
        "Example output: {\"entities\":[\"Exa\",\"Jeff Pinner\"]}\nLINK CONTEXT: {{CONTEXT}}\nPOST:\n{{TEXT}}"),
    "self_relevance": (
        "Judge why a user might have reposted this.\nPick ONE reason enum: "
        + " | ".join(REASONS) + ".\nAlso give confidence high|medium|low.\n"
        "Example output: {\"reason\":\"market-signal\",\"confidence\":\"high\"}\nLINK CONTEXT: {{CONTEXT}}\nPOST:\n{{TEXT}}"),
    "summarize": (
        "Summarize this post in AT MOST 2 sentences.\n"
        "Rules:\n- State the main claim.\n- No preamble, no markdown.\n"
        "Example output: {\"summary\":\"Claims China will open-weight a frontier model timed to Anthropic's IPO.\",\"sentiment\":\"neutral\"}\nLINK CONTEXT: {{CONTEXT}}\nPOST:\n{{TEXT}}"),
}

LENGTH_CAPS = {"summary": 400, "topic": 40, "reason": 40, "confidence": 10}
MAX_ENTITIES = 12


def build_link_context(row: dict, card: str) -> str:
    """Adaptive link context (math-modelled budget):
      B = clamp(B_card * (0.4 + 0.6*P) * W_domain * sqrt(min(1, L_clean/1500)), 375, Cap_card)  # 1.5x scaled (600 tok << 1M ctx)
    P  = tweet poverty: 1 - min(1, text_len/280)  (bare-URL post => link is everything)
    W  = domain weight: arxiv/github/docs 1.2, news/blog 1.0, social 0.5, unknown 0.9
    L_clean = content length after boilerplate strip. Diminishing returns via sqrt.
    Dead links / quoted tweets get explicit markers (no hallucination)."""
    import math, re
    title = row.get("link_title")
    desc = row.get("link_desc")
    content = row.get("link_content")
    err = row.get("link_error")
    quoted = row.get("quoted_id")
    text = row.get("text") or ""

    quoted_id_only = row.get("link_url") == "in-network"
    if quoted_id_only or (quoted and not title and not content):
        return f"(quotes tweet {quoted})" if quoted else "(no link)"
    if (err or (not title and not content)) and not content:
        return "(linked page unavailable)" if err else "(no link)"

    # --- budget math ---
    BASE = {"tag_topic": 450, "extract_entities": 1500,
            "self_relevance": 900, "summarize": 2400}
    CAP = {"tag_topic": 600, "extract_entities": 2100,
           "self_relevance": 1350, "summarize": 3600}
    b_card = BASE.get(card, 600)
    cap = CAP.get(card, 1200)

    P = 1 - min(1.0, len(text) / 280)                      # tweet poverty
    domain = (row.get("link_domain") or "").lower()
    if any(d in domain for d in ("arxiv", "github", "docs.", "nature.", "stanford", ".edu")):
        W = 1.2
    elif any(d in domain for d in ("twitter.com", "x.com", "youtube.com")):
        W = 0.5
    elif domain:
        W = 0.9 if domain not in ("t.co",) else 0.9
    else:
        W = 0.9

    # clean boilerplate before measuring
    clean = content or ""
    clean = re.sub(r"(Skip to (main )?content|Accept all cookies|Sign in|Subscribe\s*$)",
                   " ", clean, flags=re.I)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    L = len(clean)

    budget = b_card * (0.4 + 0.6 * P) * W * math.sqrt(min(1.0, L / 1500)) if L else 0
    budget = int(max(375, min(budget, cap, L if L else cap)))

    parts = []
    if title: parts.append(f'Page: "{title}"')
    if desc: parts.append(f"About: {desc[:250]}")
    if clean and budget:
        parts.append(f"Page text: {clean[:budget]}")
    if not parts:
        return "(no link)"
    return " | ".join(parts)


def validate(card: str, row: dict) -> list[str]:
    errs = []
    if card == "tag_topic" and row.get("topic") not in TOPICS:
        errs.append("topic not in enum")
    if card == "self_relevance":
        if row.get("reason") not in REASONS:
            errs.append("reason not in enum")
        if row.get("confidence") not in ("high", "medium", "low"):
            errs.append("bad confidence")
    if card == "extract_entities":
        e = row.get("entities")
        if not isinstance(e, list) or len(e) > MAX_ENTITIES:
            errs.append("entities missing or spam")
    if card == "summarize":
        s = row.get("summary", "")
        if not isinstance(s, str) or not s.strip():
            errs.append("empty summary")
        elif len(s) > LENGTH_CAPS["summary"]:
            errs.append("summary too long")
        if row.get("sentiment") not in ("positive", "negative", "mixed", "neutral"):
            errs.append("bad sentiment")
    return errs


def ask_ollama(prompt: str, schema: dict, temperature: float = 0.0,
               model: str = MODEL, base: str = OLLAMA) -> dict:
    r = requests.post(f"{base}/api/chat", json={
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "format": schema,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": 4096, "seed": 42},
        "keep_alive": "60m",
    }, timeout=300)
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"])


class LlmWorker:
    def __init__(self, store: Store, quarantine_dir: Path, backends: list | None = None,
                 base: str = OLLAMA, model: str = MODEL,
                 card_models: dict | None = None, router=None):
        self.store = store
        self.qdir = quarantine_dir
        self.qdir.mkdir(parents=True, exist_ok=True)
        self.backends = backends or default_backends(Path.home() / "inferxkey")
        self.base = base
        self.model = model
        self.card_models = card_models or {}
        self.router = router
        self.usage = {"inferx": 0, "ollama": 0, "quarantined": 0}

    def _route_row(self, row: dict) -> dict:
        """Per-row model map via the router (once per row). {} if no router."""
        if not self.router:
            return {}
        try:
            profile = self.router.route(row)
            self.store.save_route(row["tweet_id"], profile.to_dict())
            try:
                from .backends import call_log
                call_log("route", tid=row.get("tweet_id"), tier=profile.tier,
                         source=profile.source, cx=profile.complexity,
                         sens=profile.sensitivity, conf=profile.confidence,
                         refuse=int(profile.refusal_risk), reason=profile.reason[:60])
            except Exception:
                pass
            return self.router.models_for_row(profile)
        except Exception:
            return {}

    def process_one(self, card: str, tid: str, text: str, context: str = "(no link)",
                    model_map: dict | None = None) -> str:
        """Returns 'llm_done' or 'quarantined'. Tries backends in order per attempt.
        Capacity/backoff failures WAIT for cooldown instead of burning attempts —
        only invalid model output counts toward quarantine."""
        prompt = PROMPTS[card].replace("{{TEXT}}", text[:1500]).replace("{{CONTEXT}}", context)
        attempts = 0
        waits = 0
        from .backends import call_log
        while True:
            row, errs = None, ["no backend answered"]
            backend_failure = False
            used_model = None
            for be in self.backends:
                try:
                    if getattr(be, "per_card_model", False):
                        models = (model_map or {}).get(card) or self.card_models.get(card)
                        models = (models if isinstance(models, (list, tuple))
                                  else ([models] if models else [None]))
                        candidate = None
                        for m in models:  # local failover: tier model -> standard model
                            try:
                                candidate = be.chat(prompt, SCHEMAS[card], model=m)
                                used_model = m
                                break
                            except Exception as e:
                                call_log("card-model-fail", card=card, backend=be.name(),
                                         model=m, err=str(e)[:100])
                                continue
                        if candidate is None:
                            raise RuntimeError("all models in chain failed")
                    else:
                        candidate = be.chat(prompt, SCHEMAS[card])
                except Exception as e:
                    backend_failure = True  # down/capacity — not the model's fault
                    call_log("card-backend-fail", card=card, backend=be.name(),
                             err=type(e).__name__, detail=str(e)[:120])
                    continue
                if not isinstance(candidate, dict):
                    errs = ["non-dict reply"]
                    continue
                errs = validate(card, candidate)
                if not errs:
                    self.usage[be.name()] = self.usage.get(be.name(), 0) + 1
                    self.store.upsert_l5(tid, candidate)
                    th = getattr(be, "last_thinking", "")
                    if th:
                        self.store.save_thinking(tid, th)
                    # Track which model was used
                    if used_model is not None:
                        model_name = used_model
                    elif getattr(be, "per_card_model", False):
                        mm = (model_map or {}).get(card) or self.card_models.get(card)
                        model_name = mm[0] if isinstance(mm, (list, tuple)) else mm
                    else:
                        model_name = getattr(be, "model", be.name())
                    self.store.db.execute("UPDATE tweets SET model_used=? WHERE tweet_id=?", (model_name, tid))
                    self.store.db.commit()
                    return "llm_done"
                row, errs = candidate, errs
                break  # valid backend answered but invalid content -> validation retry
            if backend_failure and row is None:
                waits += 1
                if waits > 20:  # ~20+ min of cooldowns: give up for now
                    self._quarantine(card, tid, text, {}, ["backends down too long"])
                    self.usage["quarantined"] += 1
                    return "quarantined"
                # sleep until the primary's cooldown likely lifted, then retry
                time.sleep(60)
                continue
            attempts += 1
            if attempts >= 3:
                self._quarantine(card, tid, text, row or {}, errs)
                self.usage["quarantined"] += 1
                return "quarantined"
            prompt += "\n\nYour previous reply was invalid: " + "; ".join(errs) + \
                      ". Return ONLY valid JSON for the schema."
            time.sleep(2)

    def _quarantine(self, card, tid, text, row, errs):
        rec = {"card": card, "tweet_id": tid, "errors": errs, "raw": row, "text": text[:500]}
        (self.qdir / f"{tid}.{card}.skip.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2))

    def run(self, cards: list[str], limit: int = 0, log=print) -> dict:
        rows = self.store.pending_llm(limit=limit or 100_000)
        stats = {"done": 0, "quarantined": 0, "elapsed_s": 0}
        t0 = time.time()
        import sys
        for i, r in enumerate(rows, 1):
            model_map = self._route_row(r)
            for card in cards:
                ctx = build_link_context(r, card)
                stage = self.process_one(card, r["tweet_id"], r["text"], context=ctx,
                                         model_map=model_map)
                stats["done" if stage == "llm_done" else "quarantined"] += 1
            self.store.mark_llm_done(r["tweet_id"])
            tier = (self.router.tiers and "") or ""
            try:
                rj = json.loads(self.store.db.execute(
                    "SELECT route_json FROM tweets WHERE tweet_id=?",
                    (r["tweet_id"],)).fetchone()[0] or "{}")
                tier = f"{rj.get('tier','?')}/{rj.get('source','?')[:5]}"
            except Exception:
                tier = "?"
            log(f"[drain] {i}/{len(rows)} | {tier} | {r['tweet_id']} | "
                f"{(r['text'] or '')[:60]!r} | cards ok={stats['done']} "
                f"quar={stats['quarantined']} | {time.time()-t0:.0f}s")
            sys.stdout.flush()
        stats["elapsed_s"] = round(time.time() - t0)
        return stats


# --- deep curation card (link-aware) ------------------------------------------

STUDY_SCHEMA = {"type": "object", "properties": {
    "deep_reason": {"type": "string"},
    "reference_value": {"enum": ["essential", "high", "medium", "low"]},
    "study_topics": {"type": "array", "items": {"type": "string"}},
    "go_deeper": {"type": "string"}},
    "required": ["deep_reason", "reference_value", "study_topics", "go_deeper"]}

STUDY_PROMPT = """You curate a personal knowledge base. The user reposted this post:

POST: {text}

Linked resource: {title}
Description: {desc}
Page content: {content}

Explain:
1. deep_reason: why the user likely reposted this AND why it is a good
   reference/resource/study material worth going deeper into later (2-4 sentences,
   concrete, mention what specifically to study or verify).
2. reference_value: essential | high | medium | low
3. study_topics: 3-6 topics to study to fully understand it.
4. go_deeper: one concrete next action (what to read/replicate/verify first).

Reply with ONLY valid JSON."""


# --- synthesis (fuse) card: connects text + vision + links ---------------------

FUSE_SCHEMA = {"type": "object", "properties": {
    "unified_summary": {"type": "string"},
    "fused_topics": {"type": "array", "items": {"type": "string"}},
    "key_entities": {"type": "array", "items": {"type": "string"}},
    "content_type": {"enum": ["paper", "blog", "chart", "screenshot", "meme",
                              "video", "thread", "announcement", "opinion", "other"]}},
    "required": ["unified_summary", "fused_topics", "key_entities", "content_type"]}

FUSE_PROMPT = """Combine these signals about one reposted post into a single understanding.

POST TEXT: {text}

MEDIA ANALYSIS: {media}

LINKED PAGE: {link}

Produce:
1. unified_summary: 2-4 sentences — what this post is actually about, using ALL
   signals (the post text may be a bare link; the media/link may carry the real
   content). Do not mention "the post is just a link" — describe the content.
2. fused_topics: 3-6 merged topics (union of text topics and media tags, deduped).
3. key_entities: the most important named entities across all signals (max 8).
4. content_type: one of paper | blog | chart | screenshot | meme | video |
   thread | announcement | opinion | other

Reply with ONLY valid JSON."""


def fuse_row(row: dict, backends: list) -> dict | None:
    """Build fusion input from a tweet's signals and call the backend chain."""
    text = (row.get("text") or "")[:700]
    media = row.get("media_understanding") or {}
    media_s = (f"description: {media.get('description','')}; OCR: {media.get('ocr_text','')}; "
               f"tags: {media.get('tags', [])}") if media else "none"
    link = "none"
    if row.get("link_title") or row.get("link_content"):
        link = f"{row.get('link_title','')} — {row.get('link_desc','')}\n{(row.get('link_content') or '')[:900]}"
    prompt = FUSE_PROMPT.format(text=text, media=media_s, link=link)
    for be in backends:
        try:
            cand = be.chat(prompt, FUSE_SCHEMA)
        except Exception:
            continue
        if isinstance(cand, dict) and cand.get("unified_summary") and cand.get("content_type"):
            return cand
    return None


# --- synthesis (fuse) card: connects text + vision + links ---------------------

FUSE_SCHEMA = {"type": "object", "properties": {
    "unified_summary": {"type": "string"},
    "fused_topics": {"type": "array", "items": {"type": "string"}},
    "key_entities": {"type": "array", "items": {"type": "string"}},
    "content_type": {"enum": ["paper", "blog", "chart", "screenshot", "meme",
                              "video", "thread", "announcement", "opinion", "other"]}},
    "required": ["unified_summary", "fused_topics", "key_entities", "content_type"]}

FUSE_PROMPT = """Combine these signals about one reposted post into a single understanding.

POST TEXT: {text}

MEDIA ANALYSIS: {media}

LINKED PAGE: {link}

Produce:
1. unified_summary: 2-4 sentences — what this post is actually about, using ALL
   signals (the post text may be a bare link; the media/link may carry the real
   content). Do not mention "the post is just a link" — describe the content.
2. fused_topics: 3-6 merged topics (union of text topics and media tags, deduped).
3. key_entities: the most important named entities across all signals (max 8).
4. content_type: one of paper | blog | chart | screenshot | meme | video |
   thread | announcement | opinion | other

Reply with ONLY valid JSON."""


# --- parallel runner: N workers × provider affinity ----------------------------

def run_parallel(store: Store, backends: list, cards: list[str], n_workers: int = 4,
                 quarantine_dir: Path | None = None, log=print,
                 card_models: dict | None = None, router=None) -> dict:
    """Thread pool over llm_queued tweets. Each worker gets its own backend
    instance (independent sessions/cooldowns); backends are assigned round-robin
    so different rate pools get hit in parallel."""
    import threading, queue
    from itertools import cycle

    # one backend-chain copy per worker (sessions aren't thread-safe)
    worker_chains = []
    for i in range(n_workers):
        chain = []
        for be in backends:
            clone = type(be).__new__(type(be))
            clone.__dict__.update(be.__dict__)
            clone.http = requests.Session()
            # Copy adapters (e.g., retry=0 for inferx)
            for prefix, adapter in be.http.adapters.items():
                clone.http.mount(prefix, adapter)
            clone.cooldown_until = 0.0
            chain.append(clone)
        worker_chains.append(chain)

    q: queue.Queue = queue.Queue()
    rows = store.pending_llm(limit=100000)
    for r in rows:
        q.put(r)
    stats = {"done": 0, "failed": 0, "requeued": 0}
    lock = threading.Lock()
    stop = threading.Event()

    def worker(wid: int):
        chain = worker_chains[wid % len(worker_chains)]
        w = LlmWorker(store, quarantine_dir or Path("/tmp"), backends=chain,
                      card_models=card_models, router=router)
        while not stop.is_set():
            try:
                row = q.get_nowait()
            except queue.Empty:
                return
            tid = row["tweet_id"]
            if not store.claim_llm(tid):
                q.task_done()
                continue
            ok = True
            model_map = w._route_row(row)
            for card in cards:
                ctx = build_link_context(row, card)
                stage = w.process_one(card, tid, row["text"], context=ctx,
                                      model_map=model_map)
                if stage == "quarantined":
                    ok = False
                    break
            store.release_llm(tid, ok=True)
            with lock:
                stats["done"] += 1
                if stats["done"] % 50 == 0:
                    log(f"parallel: {stats['done']}/{len(rows)} done | usage={w.usage}")
            q.task_done()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return stats
