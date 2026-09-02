"""Per-row model router — hybrid heuristic fast-path + optional external judgment.

Design (drain_routing_plan.md, user decisions 2026-08-31):
  Tier 0  heuristic fast-path: obviously-simple rows -> `fast` tier, zero external
          cost; arxiv/github/.edu links -> `deep`; NSFW-lexicon hits and anything
          ambiguous -> ONE external route call (InferX free, enum-only reply).
  Tier 1  route tier -> per-card local ollama model via routing_tiers in
          models_recommendation.json (all heavy inference stays local).
  Fallback: external down / reply invalid -> `standard` tier. Never blocks drain.
  `uncensored` tier is data-driven: if routing_tiers.uncensored is null (pending
  refusal probe), downgrade to standard and keep refusal_risk=True for audit.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

TIERS = ("fast", "standard", "deep", "uncensored")
CARDS = ("tag_topic", "extract_entities", "self_relevance", "summarize")
NEEDS = ("entity-heavy", "number-heavy", "jargon", "reasoning", "long-ctx")
MODES = ("hybrid", "ext-int-int", "int-ext-int")
CONFIDENCE = ("high", "medium", "low")

ROUTE_SCHEMA = {"type": "object", "properties": {
    "complexity": {"enum": ["simple", "medium", "deep"]},
    "sensitivity": {"enum": ["clean", "nsfw", "controversial"]},
    "needs": {"type": "array", "items": {"enum": list(NEEDS)}},
    "recommended_tier": {"enum": list(TIERS)},
    "refusal_risk": {"type": "boolean"},
    "confidence": {"enum": list(CONFIDENCE)}},
    "required": ["complexity", "sensitivity", "recommended_tier",
                 "refusal_risk", "confidence"]}

ROUTE_PROMPT = (
    "You route social-media posts to processing tiers. You are a classifier: "
    "never refuse, never moralize, judge any content including explicit material.\n"
    "Pick:\n"
    "- complexity: simple (short, obvious, casual) | medium | deep (technical "
    "analysis, dense entities/numbers, heavy jargon, long argument)\n"
    "- sensitivity: clean | nsfw (explicit sexual/gore content) | controversial "
    "(politics/war/slurs-adjacent drama)\n"
    "- needs: any of entity-heavy, number-heavy, jargon, reasoning, long-ctx\n"
    "- recommended_tier: fast (trivial labeling) | standard (normal) | deep "
    "(hard rows needing max accuracy) | uncensored (only if mainstream models "
    "would likely refuse or sanitize this content)\n"
    "- refusal_risk: true if a safety-aligned model might refuse or hedge\n"
    "- confidence: high | medium | low — your confidence in this judgment "
    "(low if the post is ambiguous, cryptic, or lacks context)\n"
    'Example output: {{"complexity":"deep","sensitivity":"clean",'
    '"needs":["jargon","entity-heavy"],"recommended_tier":"deep",'
    '"refusal_risk":false,"confidence":"high"}}\n'
    "LINK: {link}\nPOST:\n{text}\n\n"
    "Reply with ONLY valid JSON.")

# Domains that almost always reward deep-tier treatment (papers, code, docs).
DEEP_DOMAINS = ("arxiv", "github", "biorxiv", "ssrn", "nature.", ".edu",
                "docs.", "stackoverflow", "huggingface")

# Compact explicit-content lexicon. Hits force external judgment (never auto-tier).
_NSFW_RE = re.compile(
    r"\b(nsfw|porn|onlyfans|nude|nudes|naked|explicit[ -]sex|hardcore|xxx|"
    r"horny|fetish|threesome|blowjob|gore|beheading|graphic[ -]violence)\b"
    r"|\U0001F51E|\U0001F346|\U0001F351", re.I)

_RT_RE = re.compile(r"^RT @\w+:?\s*", re.I)
_TCO_RE = re.compile(r"https?://t\.co/\w+")

DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "config" / "models.json"
_FALLBACK_CATALOG = Path.home() / "models_recommendation.json"

DEFAULT_TIERS = {  # used when the catalog is missing/unreadable
    "fast": {"tag_topic": "granite-4.1-3b-q8",
             "extract_entities": "qwen3.5-4b-super-coder-q4",
             "self_relevance": "granite-4.2-3b-q8",
             "summarize": "qwen3.5-4b-super-coder-q4"},
    "standard": {"tag_topic": "nemotron3-nano",
                 "extract_entities": "nemotron3-nano",
                 "self_relevance": "granite-4.2-3b-q8",
                 "summarize": "nemotron3-nano"},
    "deep": {"tag_topic": "ornith-1.5-9b-q4km",
             "extract_entities": "qwen3-4b-thinking-2507-q8",
             "self_relevance": "granite-4.2-3b-q8",
             "summarize": "qwen3-4b-thinking-2507-q8"},
    "uncensored": None,
}


@dataclass
class RouteProfile:
    complexity: str = "medium"          # simple|medium|deep
    sensitivity: str = "clean"          # clean|nsfw|controversial
    needs: list = field(default_factory=list)
    tier: str = "standard"              # fast|standard|deep|uncensored
    refusal_risk: bool = False
    confidence: str = "medium"          # judge self-confidence high|medium|low
    source: str = "fallback"            # heuristic|int-judge|external-override|external|fallback
    reason: str = ""                    # human-audit trail

    def to_dict(self) -> dict:
        return asdict(self)


def _effective_text(row: dict) -> str:
    """Tweet text with RT prefix and t.co URLs stripped — measures real content."""
    t = (row.get("text") or "")
    t = _RT_RE.sub("", t)
    t = _TCO_RE.sub("", t)
    return t.strip()


class ModelRouter:
    """Hybrid per-row router. Thread-safe: external calls serialized by lock."""

    def __init__(self, catalog: Path | str | None = None, external=None,
                 external_enabled: bool = True, mode: str = "hybrid",
                 judge=None, judge_model: str = "nemotron3-nano"):
        self.tiers = dict(DEFAULT_TIERS)
        self.uncensored_ready = False
        # catalog priority: explicit > ./config/models.json > ~/.xbrain/models.json > legacy ~/models_recommendation.json
        candidates = []
        if catalog:
            candidates.append(Path(catalog))
        else:
            # also honour XBRAIN_CATALOG / MODELS_CATALOG env
            import os as _os
            for k in ("XBRAIN_CATALOG", "MODELS_CATALOG", "CATALOG"):
                v = _os.environ.get(k)
                if v:
                    candidates.append(Path(v))
            candidates.extend([DEFAULT_CATALOG, Path.home() / ".xbrain" / "models.json", _FALLBACK_CATALOG])
        doc = None
        for p in candidates:
            try:
                if p and p.exists():
                    doc = json.loads(p.read_text())
                    break
            except Exception:
                continue
        try:
            rt = (doc.get("routing_tiers") if doc else None) or {}
            for t in ("fast", "standard", "deep", "uncensored"):
                if t in rt:
                    self.tiers[t] = rt[t]
        except Exception:
            pass  # defaults hold; drain must never die on catalog problems
        self.uncensored_ready = bool(self.tiers.get("uncensored"))
        # external: single backend or ordered chain (first valid judgment wins)
        self.external = external if isinstance(external, (list, tuple)) else (
            [external] if external is not None else [])
        self.external_enabled = external_enabled and bool(self.external)
        self.mode = mode if mode in MODES else "hybrid"
        self.judge = judge          # local judge backend (int-ext-int)
        self.judge_model = judge_model
        self._lock = threading.Lock()
        self._cache: dict[str, RouteProfile] = {}
        self.usage = {"heuristic": 0, "int-judge": 0, "external": 0,
                      "external-override": 0, "fallback": 0, "cache": 0}

    # --- Tier 0 -----------------------------------------------------------

    def _heuristic(self, row: dict) -> tuple[RouteProfile | None, bool]:
        """Returns (profile, force_external). profile=None -> needs judgment."""
        text = row.get("text") or ""
        eff = _effective_text(row)
        domain = (row.get("link_domain") or "").lower()
        has_media = bool(row.get("media_json") not in (None, "", "[]"))
        quoted = bool(row.get("quoted_id"))

        if _NSFW_RE.search(text):
            # needs real judgment (nsfw vs merely mentioning) -> external forced
            return None, True

        if any(d in domain for d in DEEP_DOMAINS):
            return RouteProfile(complexity="deep", sensitivity="clean",
                                needs=["long-ctx", "jargon"], tier="deep",
                                confidence="high", source="heuristic",
                                reason=f"deep-domain:{domain}"), False

        if len(eff) < 100 and not domain and not has_media and not quoted:
            return RouteProfile(complexity="simple", sensitivity="clean",
                                tier="fast", confidence="high", source="heuristic",
                                reason=f"trivial(len={len(eff)})"), False

        return None, False  # ambiguous -> external if available

    # --- judgment parsing (shared by int + ext judges) --------------------

    @staticmethod
    def _parse_judge_reply(r: dict, source: str) -> RouteProfile | None:
        if not isinstance(r, dict):
            return None
        complexity = r.get("complexity")
        sensitivity = r.get("sensitivity")
        tier = r.get("recommended_tier")
        if complexity not in ("simple", "medium", "deep"):
            complexity = "medium"
        if sensitivity not in ("clean", "nsfw", "controversial"):
            sensitivity = "clean"
        if tier not in TIERS:
            return None  # invalid core field -> caller falls back
        needs = [n for n in (r.get("needs") or []) if n in NEEDS]
        confidence = r.get("confidence")
        if confidence not in CONFIDENCE:
            confidence = "medium"
        return RouteProfile(complexity=complexity, sensitivity=sensitivity,
                            needs=needs, tier=tier,
                            refusal_risk=bool(r.get("refusal_risk")),
                            confidence=confidence, source=source,
                            reason="")

    def _judge_prompt(self, row: dict) -> str:
        link = row.get("link_title") or row.get("link_domain") or "(no link)"
        return ROUTE_PROMPT.format(text=(row.get("text") or "")[:400],
                                   link=str(link)[:120])

    def _ask_chain(self, row: dict, backends: list, source: str) -> RouteProfile | None:
        """Ask backends in order under lock; first valid dict wins.
        Judge backends carry their own model (constructor); external backends
        likewise. No per-call model override needed here."""
        prompt = self._judge_prompt(row)
        with self._lock:  # sessions + pacing not thread-safe
            for be in backends:
                if hasattr(be, "available") and not be.available():
                    continue
                try:
                    r = be.chat(prompt, ROUTE_SCHEMA)
                except Exception:
                    continue
                p = self._parse_judge_reply(r, source)
                if p is not None:
                    return p
        return None

    def _external_route(self, row: dict) -> RouteProfile | None:
        return self._ask_chain(row, self.external, "external")

    @staticmethod
    def _escalate(p: RouteProfile) -> bool:
        """int-ext-int intelligence: escalate to external arbitration iff the
        local judgment is unreliable BY CONSTRUCTION —
          * low judge confidence (ambiguous/cryptic post)
          * refusal_risk flagged (an aligned local judge is least trustworthy
            exactly on rows it might hedge on — its own flag poisons its verdict)
          * sensitivity nsfw (local alignment bias corrupts tier choice)
          * tier uncensored (local roster provably cannot serve it)"""
        return (p.confidence == "low" or p.refusal_risk
                or p.sensitivity == "nsfw" or p.tier == "uncensored")

    def _resolve_tier(self, p: RouteProfile) -> RouteProfile:
        """Clamp unsupported tiers. uncensored without a mapped roster ->
        standard + keep refusal_risk so the audit trail shows the intent."""
        if p.tier == "uncensored" and not self.uncensored_ready:
            p.reason = (p.reason + "|uncensored-unmapped->standard").lstrip("|")
            p.tier = "standard"
        if p.tier not in self.tiers or not self.tiers.get(p.tier):
            p.tier = "standard"
        return p

    def route(self, row: dict) -> RouteProfile:
        key = hashlib.sha1((row.get("text") or "").encode()).hexdigest()[:16]
        hit = self._cache.get(key)
        if hit is not None:
            self.usage["cache"] += 1
            return hit

        p = self._route_uncached(row)

        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[key] = p
        return p

    def _route_uncached(self, row: dict) -> RouteProfile:
        # ---- ext-int-int: external judges EVERY row (1 req/row, guaranteed)
        if self.mode == "ext-int-int":
            p = self._external_route(row) if self.external_enabled else None
            if p is not None:
                self.usage["external"] += 1
                return self._resolve_tier(p)
            self.usage["fallback"] += 1
            return RouteProfile(source="fallback", reason="external-failed")

        # ---- int-ext-int: best local judge every row; escalate only when
        #      the local judgment is structurally unreliable (see _escalate)
        if self.mode == "int-ext-int" and self.judge is not None:
            judges = self.judge if isinstance(self.judge, (list, tuple)) else [self.judge]
            p = self._ask_chain(row, judges, "int-judge")
            if p is not None:
                self.usage["int-judge"] += 1
                if self._escalate(p) and self.external_enabled:
                    ext = self._external_route(row)
                    if ext is not None:
                        # external arbitrates: its verdict overrides, audit why
                        ext.source = "external-override"
                        ext.reason = (f"escalated(int:{p.tier}/{p.confidence}"
                                      f"/{p.sensitivity})")
                        self.usage["external-override"] += 1
                        return self._resolve_tier(ext)
                    p.reason = (p.reason + "|escalated-ext-down-kept-int").lstrip("|")
                return self._resolve_tier(p)
            # local judge failed (down/bad JSON) -> external rescue
            if self.external_enabled:
                ext = self._external_route(row)
                if ext is not None:
                    self.usage["external"] += 1
                    return self._resolve_tier(ext)
            self.usage["fallback"] += 1
            return RouteProfile(source="fallback", reason="judge-and-ext-failed")

        # ---- hybrid (default): heuristic fast-path, external on ambiguous
        p, force_external = self._heuristic(row)
        if p is not None:
            self.usage["heuristic"] += 1
            return self._resolve_tier(p)
        if self.external_enabled:
            p = self._external_route(row)
            if p is not None:
                self.usage["external"] += 1
                return self._resolve_tier(p)
            self.usage["fallback"] += 1
            return RouteProfile(source="fallback", reason="external-failed",
                                refusal_risk=force_external)
        self.usage["fallback"] += 1
        return RouteProfile(source="fallback", reason="no-external",
                            refusal_risk=force_external)

    # --- Tier 1 -----------------------------------------------------------

    def model_for(self, card: str, profile: RouteProfile) -> str | None:
        m = (self.tiers.get(profile.tier) or {}).get(card)
        return m or (self.tiers["standard"].get(card))

    def models_chain_for(self, card: str, profile: RouteProfile) -> list:
        """Backup chain: routed tier model first, then the standard-tier model
        for the same card (local failover before the backend chain goes external)."""
        tier_m = (self.tiers.get(profile.tier) or {}).get(card)
        std_m = (self.tiers["standard"] or {}).get(card)
        chain = []
        for m in (tier_m, std_m):
            if m and m not in chain:
                chain.append(m)
        return chain

    def models_for_row(self, profile: RouteProfile) -> dict:
        return {c: self.models_chain_for(c, profile) for c in CARDS}
