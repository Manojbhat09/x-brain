---
name: x-brain
description: Turn any X account's reposts into a structured knowledge base + Obsidian mind graph. Local-first, 5-level pipeline (enumerate → enrich → links → vision → routed LLM → deep curation → fusion → graph). Use when the user wants to archive/curate X reposts, run the harness, add models, debug drains, or export the vault.
---

# x-brain Skill

## When to use

- User wants to archive, curate, or analyze an X account's reposts
- User asks to run, resume, debug, or monitor the x-brain pipeline
- User wants to add/swap local models, tune routing, or export the mind graph
- Agent needs to operate the harness without leaking credentials

## Quick start (copy-paste)

```bash
cp .env.example .env   # fill X_USER_ID, X_AUTH_TOKEN, X_CT0
pip install -r harness/requirements.txt
python3 harness/xb.py --brain-dir ./data doctor          # verify creds
python3 harness/xb.py --brain-dir ./data enum --resume   # L1
python3 harness/xb.py --brain-dir ./data enrich          # L2
python3 harness/xb.py --brain-dir ./data links-run       # L3
python3 harness/xb.py --brain-dir ./data vision-run      # L4 (optional)
python3 harness/xb.py --brain-dir ./data llm-run --route --mode int-ext-int --backends ollama,inferx,or-nemotron  # L5
python3 harness/xb.py --brain-dir ./data deep-run        # curation
python3 harness/xb.py --brain-dir ./data fuse-run        # synthesis (last)
python3 harness/xb.py --brain-dir ./data graph-export --out ./data/vault
```

Or via env: `X_USER_ID=123 XBRAIN_DIR=./data python3 harness/xb.py doctor`

## Core rules

```
RULE: THE HARNESS THINKS. THE MODELS FILL IN BLANKS.
```

- Deterministic Python owns navigation, pagination, checkpointing, validation, retries, storage. Models are pure functions `(card, input) -> JSON` (enum, entities, 1-2 sentence summary, yes/no). Never ask a model to plan or hold state.
- Every model output is schema-validated; invalid → retry with error appended → quarantine (`*.skip.json`), never dropped.
- No default username/id is shipped. `protocol.py` has no hard-coded user. Missing id → `X_USER_ID` env/`.env`/`--user-id`/`config.json` or interactive prompt.
- No secrets shipped. `session.py` reads `X_AUTH_TOKEN`/`X_CT0`/`INFERX_API_KEY`/`OPENROUTER_API_KEY` from env/`.env`/`creds.json` (0600). `.gitignore` excludes `.env`, `creds.json`, `state.sqlite*`, `vault/`, `*.log`.

## The 5-level data model

One row per repost; levels are column groups so cheap levels fill first:

| L | content | produced by |
|---|---------|-------------|
| L1 | tweet id, author, text, timestamps, repost position, quote/media refs | enumerator |
| L2 | root/conversation ids, ancestors, engagement snapshot | enricher (GraphQL + Fx fallback) |
| L3 | resolved URL, domain, title, description, page text, error states | link resolver |
| L4 | description, OCR, tags per media item | vision worker |
| L5 | topic, entities, summary, reason, curation, fused synthesis | LLM cards |

Deleted posts are tombstones (`examples/tombstones.jsonl.example`): first/last seen + URL, never garbage-collected.

## Pipeline

```
creds (.env / --user-id) → L1 enumerator (UserRepostsTimeline, double-empty EOF, checkpointed)
  → L2 enricher (TweetResultByRestId + FxLane fallback)
  → L3 links (t.co → final URL/domain/title/text)
  → L4 vision (downscale → base64, warm-ping, stream, retry)
  → L5 llm-run (router → tag_topic / extract_entities / self_relevance / summarize)
  → deep-run (deep_reason, reference_value, study_topics, go_deeper)
  → fuse-run (unified_summary, fused_topics, key_entities, content_type) — runs LAST
  → graph-export (vault + graph.json)
```

- **Lanes:** Primary `GraphQLLane` (bearer, cookies, CSRF, `x-client-transaction-id`, pinned `FEATURES`/`QID_REPOSTS` in `xbrain/protocol.py:34-85`) + `FxLane` cookie-free per-ID fallback + archive import. Rate safety: token-bucket + jitter, per-op windows from response headers, circuit breaker (see `xbrain/ratelimit.py`).
- **Link context budgeting** (`xbrain/llm.py:build_link_context`): `B = clamp(B_card*(0.4+0.6*P)*W_domain*sqrt(min(1,L/1500)),375,Cap)` — poverty, domain weight (papers 1.2), boilerplate strip, sqrt diminishing returns.
- **Deep** (`xbrain/llm.py:STUDY_PROMPT/SCHEMA`): why reposted, reference value, study topics, next action.
- **Fusion** (`xbrain/llm.py:fuse_row`): merges text+vision+links; bare-link posts described by linked content.

## Dynamic router (`xbrain/router.py`)

One enum-only judgment per row **before** cards:

```json
{"complexity":"simple|medium|deep","sensitivity":"clean|nsfw|controversial","needs":[...],"recommended_tier":"fast|standard|deep|uncensored","refusal_risk":bool,"confidence":"high|medium|low"}
```

| mode | behavior | external spend |
|------|----------|----------------|
| `hybrid` (default) | heuristic fast-path (trivial→fast, arxiv/github→deep, NSFW→external); external judges ambiguous | ambiguous only |
| `ext-int-int` | external judges every row | 1 req/row |
| `int-ext-int` | local judge (`--judge-model`→`--judge-backup`) every row; external arbitration only on `confidence==low`/`refusal_risk`/`nsfw`/`uncensored` | flagged only |

Escalation is the point: local is cheap but untrustworthy exactly on hedged rows, so `router.py:_escalate` is the intelligence. Failover layers: judge chain → external chain → tier model → backend chain → proxy fallbacks. Tier→model mapping is **entirely** `config/models.json:routing_tiers` (see below); VRAM pairing (e.g. `self_relevance` 1.9 GB to co-reside with 2.8 GB judge) and per-model timeouts already wired.

## Model catalog (`config/models.json`)

Single JSON drop-in; no code change. Priority: `--catalog` > `$XBRAIN_CATALOG` > `./config/models.json` > `~/.xbrain/models.json` > legacy `~/models_recommendation.json`.

```jsonc
{
  "routing_tiers": {
    "fast":       {"tag_topic":"granite-4.1-3b-q8", "extract_entities":"qwen3.5-4b-super-coder-q4", ...},
    "standard":   {"tag_topic":"nemotron3-nano", ...},
    "deep":       {"tag_topic":"ornith-1.5-9b-q4km", ...},
    "uncensored": {"tag_topic":"small-8b-gaston-q4km", ...}  // null to disable
  }
}
```

Swap: `ollama create <tag> -f Modelfile` → point tier/card at tag → restart. Startup prints `AVAILABLE`/`MISSING → falls back to standard`. See `config/models.md` for the 24-model eval that chose the defaults (6 GB GPU, 5.2 usable, Q4/Q8, 143s/row clean → 18d single / 6d x3).

## Vault export (`xbrain/graph.py`)

- **Obsidian** (`--format obsidian`): one note per post/entity/topic/author, wikilinks post→entity/topic/author, entity↔entity co-occurrence, author affinity, post→quoted. `graph-export --min-cooccur 2 --min-mentions 2` filters noise (ref: 4,772 posts → 3,711 entities → 628 after filter, 156 co-occurrence, 798 affinity, 10,412 nodes in `graph.json`, ~3s).
- **JSON** (`--format json`): `{nodes, links}` for cytoscape/cosmograph/Gephi.

## Monitoring & recovery

```bash
tail -f $XBRAIN_DIR/drain_calls.log   # every route + call (tier/source/judge/model/latency)
tail -f $XBRAIN_DIR/xbrain_drain.log  # per-row heartbeat [drain] N/TOTAL | tier | id | cards ok
sqlite3 $XBRAIN_DIR/state.sqlite "SELECT stage, COUNT(*) FROM tweets GROUP BY stage"
sqlite3 $XBRAIN_DIR/state.sqlite "SELECT route_tier, COUNT(*) FROM tweets WHERE stage='done' GROUP BY route_tier"
```

- SQLite WAL, single-writer, idempotent per `tweet_id`; `store.py:expire_llm_leases()` requeues `kill -9`'d claims; `quarantine/*.skip.json` holds full context.
- If `xbrain_drain.log` appears frozen but `drain_calls.log` advances, the current row is a `deep` card (`qwen3-4b-thinking` 214-375s, `ornith` 39-48s) — not stuck. `http://localhost:11434/api/ps` shows resident model. Never `sed -i` the open drain log (it detaches the fd → `(deleted)`).
- Vision gateway 504s: downscale to ≤1024 JPEG q82, client-side base64, warm-ping, retry-into-warmth, stream (`LEARNINGS-gateway-504.md`).

## Environment

| var | purpose | default |
|-----|---------|---------|
| `X_USER_ID` / `--user-id` | target numeric X user id | required (prompt if tty) |
| `X_AUTH_TOKEN` / `X_CT0` | browser session cookies | `creds.json` (0600) |
| `XBRAIN_DIR` / `--brain-dir` | `state.sqlite`, `cache/`, `vault/` | `~/.xbrain` |
| `XBRAIN_CATALOG` | model catalog JSON | `./config/models.json` |
| `INFERX_API_KEY` | router/vision InferX key | `~/inferx*key` files |
| `OPENROUTER_API_KEY` | router/vision OpenRouter key | `~/openrouterkey` |

Copy `.env.example` → `.env`; all flags also accept env. See `AGENTS.md` for file map and agent workflow.
