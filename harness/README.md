# x-brain

A local-first pipeline that turns any X/Twitter account's timeline — reposts,
posts, or both — into a structured, queryable knowledge base, then renders it
as an interactive mind graph (Obsidian-compatible vault plus a portable network
JSON).

The tool was built for creators and heavy readers who use reposts and posts as
curation signals: everything an account has amplified or authored is enumerated,
enriched, scraped, understood across text, media, and links, fused into one
coherent record per item, and connected into a graph. It runs on a single
consumer GPU (or CPU only), stores everything in one SQLite file, and is
designed to be resumable, auditable, and never lossy. By default it enumerates
reposts; `--posts-only` switches to the authored posts timeline and
`--posts-reposts` builds a compatible archive of both in one run.

---

## 1. Design philosophy

```
RULE: THE HARNESS THINKS. THE MODELS FILL IN BLANKS.
```

All navigation, pagination, checkpointing, validation, retries, storage, and
diffing are deterministic Python. Language models are used strictly as pure
functions `(card, input) -> JSON`: classify into closed enums, extract
entities, write one- to two-sentence summaries, and make yes/no judgments.
Models are never asked to plan, control flow, hold state, or produce long
text. Every output is schema-validated in code before it is stored; invalid
outputs are retried with error feedback and then quarantined, never dropped.

---

## 2. Data model (five levels)

One row per timeline item (repost or post); levels are column groups so cheap
levels fill first and enrichment backfills:

| level | content | produced by |
|---|---|---|
| L1 item head | tweet id, `kind` (`repost` / `post`), author, text, timestamps, timeline position, quote/retweet links, media refs | enumerator (`UserRepostsTimeline` or `UserTweets`) |
| L2 thread context | root/conversation ids, ancestors, in-reply-to, engagement snapshots | enricher |
| L3 link layer | resolved URL, domain, title, description, page text, error states | link resolver |
| L4 media understanding | description, OCR text, tags | vision worker |
| L5 knowledge layer | topic, entities, summary, relevance reason, curation fields, fused synthesis, embeddings-ready fields | LLM cards |

`kind` distinguishes the source timeline: `repost` for reposts, `post` for
original authored posts; legacy rows have `NULL` (treated as `repost` for
compatibility). `--posts-reposts` writes both kinds into the same SQLite file
with separate cursors (`enum_cursor` / `enum_cursor_posts`) so reposts and
posts never clobber each other. Deleted posts are first-class: tombstone records
preserve first-seen/last-seen chronology, and the master ID list enables monthly
deletion diffs. Comments/replies are not enumerated in L1 (see Limitations) —
they are fetched as ancestors in L2 and require a separate `SearchTimeline`
design.

---

## 3. Pipeline architecture

```
   account creds
        |
        v
  L1  enumerator      full history via reposts (--default), posts (--posts-only),
        |             or both (--posts-reposts) — each with its own cursor,
        |             double-empty end-of-feed detection, checkpointed, resumable
        v
  L2  enricher        thread context + engagement, with a cookie-free
        |             public fallback resolver per ID
        v
  L3  links           t.co resolution -> final URL/domain/title/text;
        |             in-network quotes routed to L4; dead links recorded
        v
  L4  vision          OCR / description / tags per media item (optional)
        |
        v
  L5  llm-run         per-row dynamic model router -> four cards
        |             (tag_topic, extract_entities, self_relevance,
        |              summarize) with strict validation + quarantine
        v
  deep-run            curation card per post: why reposted, reference
        |             value, study topics, concrete next action
        v
  fuse-run            synthesis pass: unifies text + vision + links into
        |             one record per post (runs last, needs all signals)
        v
  graph-export        Obsidian vault + graph.json mind graph
```

### Enumeration lanes

The primary lane drives either `UserRepostsTimeline` (reposts, `QID bV_DHAI…`)
or `UserTweets` (posts, `QID 6r5OLC_…` — override via `X_QID_POSTS` env if X
rotates it) with the full authenticated header set (bearer, session cookies,
CSRF, `x-client-transaction-id`, pinned feature blob). `--posts-only` uses
`UserTweets` and filters to originals only (reposts in that timeline are
dropped after fetch, reported as `posts filter: 20 fetched → 6 originals`);
`--posts-reposts` runs both timelines sequentially with independent
`enum_cursor` / `enum_cursor_posts` checkpoints and per-op rate windows
(`UserRepostsTimeline` vs `UserTweets` budgets are tracked separately).
A cookie-free public resolver serves as the per-ID fallback/verifier, and an
official archive takeout can be imported to merge text snapshots (including
since-deleted content) by tweet id. All lanes return identical DTOs; upper
layers never know which lane served a request.

Rate safety: token-bucket pacing with jitter, per-operation limit windows
persisted from response headers, and a circuit breaker that trips on
sustained rate-limit responses, halves the rate, and probes before closing.

### Timeline kinds and compatibility

| flag | timeline op | QID env override | cursor key | stored `kind` | notes |
|------|-------------|-----------------|------------|---------------|-------|
| (default) | `UserRepostsTimeline` | `QID_REPOSTS` (= `bV_DHAI…`) | `enum_cursor` | `repost` (legacy `NULL` also) | repost archive, compatible with all prior DBs |
| `--posts-only` | `UserTweets` | `X_QID_POSTS` (= `6r5OLCC_…`) | `enum_cursor_posts` | `post` | original authored posts only; `UserTweets` also contains reposts but they are dropped, `kind breakdown: post=N` |
| `--posts-reposts` | both sequentially | both | both | both | one `stats` shows `reposts cursor` + `posts cursor` + `kind breakdown`; idempotent per `tweet_id` so the two runs never duplicate |
| comments/replies | not yet — requires `SearchTimeline`/`UserTweetsAndReplies` + ancestor BFS | — | — | `reply` (reserved) | replies live outside the two timelines; L2 already stores `ancestors_json` but full enumeration needs a separate design (see Limitations) |

### Deep curation pass (`deep-run`)

For every post with a resolved link or substantive text, a curation card
produces: `deep_reason` (why the repost likely happened and why the resource
is worth studying, 2-4 concrete sentences), `reference_value`
(essential/high/medium/low), `study_topics` (3-6 topics required to fully
understand it), and `go_deeper` (one concrete next action: what to read,
replicate, or verify first).

### Fusion pass (`fuse-run`) — the connection layer

Text, vision, and link passes are blind to each other by construction
(different queues, different models, different times). The fusion pass is
the synthesis layer that closes this gap: for every post carrying media
understanding or scraped link content, it combines all signals and produces
a single coherent record — `unified_summary` (2-4 sentences describing what
the post is actually about, using all signals; a bare-link post is described
by its linked content, not dismissed as "just a link"), `fused_topics`
(union of text and media topics, deduped), `key_entities` (the most
important named entities across all signals), and `content_type` (paper,
blog, chart, screenshot, meme, video, thread, announcement, opinion, other).
Sequencing is deliberate: fusion runs last, after every other pass has had a
chance to fill its columns.

### Adaptive link-context budgeting

Card prompts receive scraped page content through a mathematically budgeted
excerpt rather than a raw dump:

```
B = clamp(B_card * (0.4 + 0.6 * P) * W_domain * sqrt(min(1, L_clean/1500)), 375, Cap_card)
P       = post poverty: 1 - min(1, text_len/280)   (bare-link post -> link is everything)
W       = domain weight: papers/code/docs 1.2, news/blogs 1.0, social 0.5
L_clean = page length after boilerplate strip; diminishing returns via sqrt
```

Dead links and quoted tweets get explicit markers (never hallucinated).
Boilerplate (cookie banners, sign-in chrome) is stripped before measurement.

---

## 4. The dynamic model router

Static model assignment wastes compute in both directions: trivial posts pay
reasoning-model latency, hard posts get underpowered models. The router
fixes this per row, before the cards run.

### Judgment schema (enum-only, one call per row)

```json
{
  "complexity":       "simple | medium | deep",
  "sensitivity":      "clean | nsfw | controversial",
  "needs":            ["entity-heavy", "number-heavy", "jargon", "reasoning", "long-ctx"],
  "recommended_tier": "fast | standard | deep | uncensored",
  "refusal_risk":     true,
  "confidence":       "high | medium | low"
}
```

### Three modes (`--mode`)

| mode | behavior | external spend |
|---|---|---|
| `hybrid` (default) | deterministic heuristic fast-path (trivial rows -> `fast`, paper/code domains -> `deep`, NSFW-lexicon -> external judgment); an external model judges the ambiguous remainder | ambiguous rows only |
| `ext-int-int` | an external model judges every row | 1 call/row |
| `int-ext-int` | a local judge model judges every row; external arbitration only when the local judgment is structurally unreliable; the local judgment stands if external is unavailable | flagged rows only |

### Escalation intelligence (`int-ext-int`)

The local judge escalates a row to external arbitration if and only if any
hold:

- `confidence == low` (ambiguous or cryptic post),
- `refusal_risk == true` (an aligned local judge is least trustworthy
  exactly on rows it might hedge on; its own flag poisons its verdict),
- `sensitivity == nsfw` (alignment bias corrupts tier choice),
- `recommended_tier == uncensored` (local roster cannot serve it).

Rationale: local judgment is cheap, private, and always available, but it is
unreliable precisely on the rows that need judgment. External arbitration is
spent only there.

### Tier-to-model mapping

Driven entirely by the model catalog JSON (section 5). The shipped mapping:

| tier | tag_topic | extract_entities | self_relevance | summarize |
|---|---|---|---|---|
| fast | granite-4.1-3b (q8) | qwen3.5-4b-super-coder (q4) | lfm2.5-2.6b (q5) | qwen3.5-4b-super-coder (q4) |
| standard | nemotron3-nano (q4) | nemotron3-nano (q4) | lfm2.5-2.6b (q5) | nemotron3-nano (q4) |
| deep | ornith-1.5-9b (q4) | qwen3-4b-thinking (q8) | granite-4.2-3b (q8) | qwen3-4b-thinking (q8) |
| uncensored | lfm2.5-8b-a1b-unc (q4) | lfm2.5-8b-a1b-unc (q4) | granite-4.2-3b (q8) | lfm2.5-8b-a1b-unc (q4) |

VRAM pairing matters on small GPUs: `self_relevance` uses a 1.9 GB model so
it coexists in memory with the 2.8 GB judge; a 4.4 GB alternative
CPU-straddles when resident alongside it and degrades to timeouts. Slow
(CPU-straddling) models receive a raised request timeout automatically.

### Failover layers (every route has backups)

| layer | chain |
|---|---|
| local judge | primary judge -> backup judge -> external rescue -> standard tier |
| external chain | provider A -> provider B -> provider C -> standard tier |
| tier model (per card) | routed model -> standard-tier model |
| backend transport | local runtime -> direct local runtime -> external A -> external B |
| proxy (optional) | config-level model fallbacks |

Additional runtime protections: streaming consumption of local inference so
client timeouts cancel server-side generation (non-streaming abandonment
creates zombie generations that serialize the GPU); cooldown/circuit-breaker
per external backend; per-row leases; quarantine with requeue.

### Audit trail

Every routing decision and every LLM call is logged (tier, source, judge
verdict, model, latency, failure reason) to `drain_calls.log`, and the
chosen tier plus full judgment is persisted per row (`route_tier`,
`route_json`, `model_used` columns), so any output can be traced to the
model that produced it.

---

## 5. Model catalog (JSON drop-in)

All model selection reads a single JSON file (`~/models_recommendation.json`
by default; override with `--catalog`). Edit the file, restart the worker,
and the new mapping is active. No code changes.

```jsonc
{
  "routing_tiers": {
    "fast":       { "tag_topic": "granite-4.1-3b-q8", "...": "..." },
    "standard":   { "tag_topic": "nemotron3-nano",    "...": "..." },
    "deep":       { "tag_topic": "ornith-1.5-9b-q4km","...": "..." },
    "uncensored": { "tag_topic": "small-8b-gaston-q4km", "...": "..." },
    "_meta":      { "policy": "...", "deep_budget": "...", "fallback": "..." }
  },
  "recommended_models": [
    {
      "model": "nemotron3-nano",
      "size_gb": 2.8, "vram_gb": 3.3, "gpu_fit": "full",
      "role": "primary_fast_bulk",
      "quality": { "topic_frac": 0.92, "F1": 0.534, "obj": 0.866,
                   "rel_frac": 0.50, "avg_s": 6.1, "determinism": 1.0 },
      "routing": { "speed_tier": "standard", "grounding": 0.866,
                   "best_cards": ["tag_topic", "extract_entities"] }
    }
  ],
  "deleted_models": [ { "model": "...", "reason": "..." } ]
}
```

Rules:

- Tier maps reference models by their local runtime tag (e.g. an Ollama
  tag). The referenced model must exist in the local runtime; on startup the
  harness checks availability and prints `AVAILABLE` / `MISSING` per tier
  model. Missing models fall back to the standard tier at routing time.
- `uncensored: null` disables that tier and downgrades flagged rows to
  standard with `refusal_risk` retained for audit.
- Per-model quality numbers are carried for provenance; the router itself
  only requires `routing_tiers`.
- Swapping a model is: (1) register the GGUF in the local runtime,
  (2) point the tier/card entry at its tag, (3) restart. A startup
  validation line confirms pickup.

---

## 6. Evaluation methodology

All models in the shipped catalog were measured on the same harness before
inclusion. (Scores are lower bounds against a noisy gold set; manual review
showed candidates frequently matched or exceeded the gold labels.)

Metrics per model (temperature 0, fixed seed, one run per prompt plus a
determinism re-run of a third of the set):

| metric | definition |
|---|---|
| topic agreement | fraction of posts whose assigned topic matches the gold label |
| F1 | token-level entity extraction F1 against gold entities |
| objectivity (obj) | fraction of emitted entities literally present in the post text or scraped link content; 1.0 means zero fabricated entities |
| relevance (rel) | agreement with gold repost-reason labels |
| determinism | fraction of identical outputs on re-run |
| avg_s | mean wall-clock per card (thinking inclusive) on the reference GPU |
| bench valid | fraction of schema-valid JSON outputs on a 9-task battery |

### Test specifications

- CPU/GPU: one consumer GPU with 6 GB VRAM (about 5.2 GB usable after
  display overhead); models above roughly 4.4 GB including KV cache
  partially offload to CPU and run markedly slower.
- Context window 8192; temperature 0; fixed seed; thinking mode enabled for
  reasoning models.
- Quality set: 12 gold posts (baseline roster) / 8 gold posts (GPU-fit
  candidates). Speed battery: 9 tasks. Determinism spot-checks on a third of
  each set. 24 distinct base models evaluated end to end; 15 rejected
  (broken JSON, fabrication, non-reasoning outputs, or latency collapse).
- Acceptance gates before any bulk run: schema-valid JSON at or above 99
  percent with retries, topic agreement at or above 85 percent on the golden
  set.
- Grounding spot-audit of production output: 86 percent of emitted entities
  literally present in post text or scraped page content, matching the
  bench-time objectivity range of the routed roster.

### Roster summary (see catalog JSON for full numbers)

| model | params/quant | tier | notable |
|---|---|---|---|
| nemotron-3-nano | 4B q4 | standard, judge | topic 11/12, F1 0.534, obj 0.866, 6.1 s |
| granite-4.2-3b | 3B q8 | deep self_relevance | obj 1.0 (zero fabrication) |
| granite-4.1-3b | 3B q8 | fast topic | 0.6 s; weak grounding, topic only |
| qwen3.5-4b-super-coder | 4B q4 | fast extract/summary | 9/9 schema validity |
| ornith-1.5-9b | 9B q4 | deep topic | topic 12/12; fabricates entities (post-validated) |
| qwen3-4b-thinking-2507 | 4B q8 | deep extract/summary | obj 1.0, rel 10/12; slow |
| lfm2.5-2.6b-code | 2.6B q5 | self_relevance | rel 7/8, obj 0.958, VRAM-paired |
| lfm2.5-8b-a1b uncensored | 8B-A1B q4 | uncensored | topic 8/8, obj 0.975; passes judgment probes |

---

## 7. Vision engineering notes

Hosted vision endpoints behind gateways enforce a first-byte timeout, and
vision models emit nothing until image prefill completes. The vision worker
therefore: downscales images before upload (fewer vision tiles, shorter
prefill — the single largest win), sends client-side base64 rather than
server-side fetch URLs, keeps instances warm with periodic ping requests so
cold start happens outside the critical path, retries once into warmth on
gateway timeout, and streams responses. The same streaming discipline
applies to local inference, where client abandonment of a non-streaming
request otherwise leaves a server-side generation running that serializes
the GPU behind a dead consumer.

---

## 8. Installation

Requirements:

- Python 3.10+
- A local model runtime (Ollama) with the models you select
- `pip install requests`
- Account credentials for the enumeration lane (auth token and session
  cookie from an authenticated browser session)

Optional: LiteLLM proxy in front of the local runtime; free-tier external
provider keys for the external router legs.

---

## 9. Usage

```bash
python3 xb.py auth --auth-token <token> --ct0 <cookie> --user-id <numeric_id>
python3 xb.py doctor                                # verify reposts lane
python3 xb.py doctor --posts-only                   # verify posts lane
python3 xb.py doctor --posts-reposts                # verify both

python3 xb.py enum --resume                         # L1: reposts (default, resumable)
python3 xb.py enum --posts-only --resume            # L1: posts only (originals, resumable)
python3 xb.py enum --posts-reposts --resume         # L1: both timelines sequentially, compatible
python3 xb.py enrich                                # L2: thread context (per-id, handles both kinds)
python3 xb.py links-run                             # L3: resolve + scrape links
python3 xb.py vision-run                            # L4: media understanding (optional)

# L5: routed analysis, four cards per item (kind-aware but model-agnostic)
python3 xb.py llm-run --route --mode int-ext-int \
    --backends ollama,inferx,or-nemotron

python3 xb.py deep-run                              # curation: why reposted/posted + study value
python3 xb.py fuse-run                              # synthesis: unify text+vision+links

# mind graph (optionally filter by kind/topic)
python3 xb.py graph-export --out ~/vault --min-cooccur 2 --min-mentions 2
```

Key `enum`/`doctor` flags: `--posts-only` (posts), `--posts-reposts` (both), `--count`, `--max-pages`, `--resume`; QID override `X_QID_POSTS` env.

Key `llm-run` flags: `--mode hybrid|ext-int-int|int-ext-int`,
`--judge-model`, `--judge-backup`, `--catalog <json>`, `--model`,
`--rel-model`, `--workers N`, `--limit`, `--cards`.

### Monitoring

```bash
tail -f ~/xbrain_drain.log          # per-row heartbeats (tier, cards, elapsed)
tail -f ~/x-brain/drain_calls.log   # every route decision + call + failure
sqlite3 ~/x-brain/state.sqlite "SELECT stage, COUNT(*) FROM tweets GROUP BY stage"
sqlite3 ~/x-brain/state.sqlite "SELECT kind, COUNT(*) FROM tweets GROUP BY kind"  # repost/post breakdown
sqlite3 ~/x-brain/state.sqlite "SELECT value FROM kv WHERE key IN ('enum_cursor','enum_cursor_posts')"  # per-timeline checkpoints
```

---

## 10. Mind graph export

Two output formats from the same data:

**Obsidian vault** (`--format obsidian`): one markdown note per post,
entity, topic, and author. Edges are wikilinks: post to its
entities/topic/author, entity to co-occurring entities, author to kindred
authors (shared-entity affinity), post to quoted post. Note bodies carry
summaries, relevance labels, curation fields, and engagement metadata. Open
the folder as a vault and the graph view renders the full network.

**graph.json** (`--format json`): `{nodes, links}` with kinds (`post`,
`entity`, `topic`, `author`, `domain`) and integer weights (co-occurrence
counts, affinity strengths). Consumable by cytoscape.js, cosmograph, or
Gephi converters.

Edge semantics and filters: `--min-cooccur` (entity co-occurrence
threshold), `--min-mentions` (suppresses singleton extraction noise),
`--topic` (subgraph), `--limit`. Reference export on a 4,772-post dataset:
3,711 raw entities (628 after the noise filter), 156 co-occurrence edges,
798 author-affinity edges, 10 topic nodes; 10,412 nodes and 15,645 links in
graph.json; runtime about 3 seconds.

Because every post carries extracted entities, topics, authors, scraped
domains, and fused synthesis fields, the graph also serves as an index for
further crawling and higher-level analysis: any high-degree node is a seed
for targeted re-enumeration, and monthly snapshots of the graph surface
thematic drift over time.

---

## 11. Resilience and data integrity

- Single-writer SQLite with WAL; append-only raw JSONL alongside; every
  store mutation is idempotent per tweet id.
- Lease-based claiming survives worker crashes (`kill -9` anywhere is
  safe); expired leases requeue automatically.
- Any row that fails all backends after retries is quarantined to disk with
  full error context and can be requeued; nothing is silently dropped.
- Dead links, deleted posts (tombstones), and unavailable pages are
  recorded as first-class states.
- Model outputs are validated against strict schemas (closed enums, length
  caps, entity-count caps); invalid outputs retry with the error appended
  before quarantine.
- Every L5 output records the producing model, route tier, and judgment,
  enabling later re-processing of rows handled by a model that a future
  evaluation retires (this was exercised in production: rows previously
  processed by an unvalidated model were identified via the `model_used`
  column and requeued).
- Rate-limit windows are persisted across runs; the enumerator checkpoints
  its cursor and resumes mid-feed.

---

## 12. Limitations and roadmap

- Enumeration depends on non-public platform endpoints; expect breakage
  when the platform changes shapes. Parsers are deliberately tolerant and
  raw payloads are retained for re-parsing. Feature blobs and operation ids
  (`QID_REPOSTS`, `QID_POSTS` via `X_QID_POSTS`, `QID_TWEET`) rotate and are pinned in config for easy patching.
- Throughput on one 6 GB GPU is roughly 1 to 2.5 minutes per item for the
  full four-card routed pipeline depending on tier mix; deep-tier posts are
  markedly slower (CPU offload).
- External free tiers are rate-limited and intermittently unavailable;
  every external dependency has a local degradation path, at the cost of
  routing those rows to the standard tier.
- The uncensored tier exists for rows where aligned models hedge or refuse;
  it is selected only when the router flags such rows, and its outputs
  carry the same grounding audits as everything else.
- Comments/replies are not yet enumerated in L1: replies live outside
  `UserRepostsTimeline`/`UserTweets` and require a separate
  `SearchTimeline` or `UserTweetsAndReplies` enumerator plus ancestor BFS
  over `conversation_id` (L2 `ancestors_json` already stores ancestors but
  currently single-hop; comments need multi-hop with dedupe and tombstone
  propagation per ancestor). The `kind='reply'` column is reserved; graph
  `reply→parent` edges and conversation clustering are designed but not
  implemented. Posts vs reposts, however, already work: `--posts-only`
  (originals only) and `--posts-reposts` (both, compatible cursors) are
  tested and cover the full `UserTweets` + `UserRepostsTimeline` surface.
- Roadmap: embedding-backed semantic search over fused records; era/theme
  segmentation (monthly clustering of summaries into named periods);
  archive-takeout import and Wayback reconciliation for deleted-post
  recovery; full comments/replies enumeration.

---

## 13. Cost and rate-limit research (paid-hosting option)

The drain workload was measured from production call logs (777 real calls:
per-model output and reasoning character counts), then priced against 61
models across live provider pricing (OpenRouter's public model API with 399
priced listings, DeepInfra direct pricing, DeepSeek direct pricing;
researched at release time).

### Measured workload (11,000 rows)

Per row: one router judgment plus four cards. Measured averages: about 450
input tokens per call (fixed card prompt plus post text plus budgeted link
context), about 22 tokens of JSON output per card, and 0 to 3,400 tokens of
reasoning depending on model. Totals for 11,000 rows (55,000 calls):

| scenario | input | output | when |
|---|---|---|---|
| A: lean (non-thinking models) | 25M | 1.4M | enum classification, temp 0 |
| B: light thinking | 25M | 13M | ~150 reasoning tokens per call |
| C: heavy thinking | 25M | 36M | local-router mix (~640 avg) |

### Rate limits (the real gate, not price)

Free tiers cap requests; paid tiers generally do not. Verified findings:

| provider | limit policy | 55k-call feasibility |
|---|---|---|
| OpenRouter, free variants | 20 requests/minute; 50 requests/day with no purchase, 1,000/day after any 10 USD credit purchase | not feasible (this is the wall the free-tier router legs hit) |
| OpenRouter, paid models | no platform-level request cap; DDoS protection only; provider-side 429s auto-failover across providers | feasible; full drain in hours |
| DeepSeek direct | concurrency-based, not request-count: 2,500 concurrent (flash), 500 (pro) | trivially feasible |
| DeepInfra | per-model requests/minute tiers, typically 100-1,000, raisable on request | feasible; hours at low tier |
| Batch variants (":batch") | designed for bulk; no request-rate competition | feasible by construction; ~24 h turnaround |

### Cost table (61 models compared; selected)

USD for the full 11,000-row drain, scenarios A / B / C. Cache and batch
discounts stack further: the fixed card-prompt prefix is about 27 percent of
input and hits prompt-cache rates on providers that discount it (DeepSeek
cache-hit input is 1/30th price); batch variants halve everything.

| model | $/M in | $/M out | A lean | B light | C heavy | class |
|---|---|---|---|---|---|---|
| mistral-nemo | 0.019 | 0.03 | 0.52 | 0.86 | 1.56 | 12B |
| llama-3.1-8b-turbo (DeepInfra) | 0.02 | 0.04 | 0.56 | 1.02 | 1.94 | 8B |
| ling-3.0-flash | 0.021 | 0.063 | 0.61 | 1.34 | 2.79 | small |
| gemma-4-E4B | 0.02 | 0.10 | 0.64 | 1.80 | 4.10 | 4B MoE |
| granite-4.0-h-micro | 0.017 | 0.112 | 0.58 | 1.88 | 4.46 | micro |
| l3-lunaris-8b (uncensored) | 0.04 | 0.05 | 1.07 | 1.65 | 2.80 | 8B |
| gpt-oss-120b | 0.037 | 0.17 | 1.16 | 3.13 | 7.04 | 117B MoE |
| deepseek-v4-flash (via OR) | 0.05 | 0.16 | 1.47 | 3.33 | 7.01 | large |
| gpt-5-nano (batch) | 0.025 | 0.20 | 0.91 | 3.23 | 7.83 | mid |
| nemotron-3-nano-30b-a3b | 0.05 | 0.20 | 1.53 | 3.85 | 8.45 | 30B MoE |
| gemini-2.5-flash-lite (batch) | 0.05 | 0.20 | 1.53 | 3.85 | 8.45 | mid |
| qwen3.5-9b | 0.10 | 0.15 | 2.71 | 4.45 | 7.90 | 9B |
| mistral-small-3.2-24b | 0.075 | 0.20 | 2.15 | 4.47 | 9.07 | 24B |
| gemma-3-27b-it | 0.08 | 0.16 | 2.22 | 4.08 | 7.76 | 27B |
| gemma-4-31b-turbo | 0.09 | 0.34 | 2.73 | 6.67 | 14.49 | 31B |
| llama-3.3-70b-turbo | 0.10 | 0.32 | 2.95 | 6.66 | 14.02 | 70B |
| qwen3-235b-a22b | 0.09 | 0.55 | 3.02 | 9.40 | 22.05 | 235B MoE |
| nemotron-3-super-120b | 0.085 | 0.40 | 2.69 | 7.33 | 16.52 | 120B MoE |
| deepseek-v3.2 | 0.26 | 0.38 | 7.03 | 11.44 | 20.18 | large |
| gemini-2.5-flash (direct) | 0.30 | 2.50 | 11.00 | 40.00 | 97.50 | large |
| kimi-k2.6 | 0.75 | 3.50 | 23.65 | 64.25 | 144.75 | large MoE |
| claude-haiku-4.5 | 1.00 | 5.00 | 32.00 | 90.00 | 205.00 | frontier-lite |
| claude-sonnet-5 | 2.00 | 10.00 | 64.00 | 180.00 | 410.00 | frontier |

### Findings

1. The full drain costs between 0.50 and 8 USD on sensible paid models; the
   historical bottleneck was free-tier request caps (20/minute,
   1,000/day), never price.
2. Output volume dominates cost spread: lean JSON versus billed reasoning
   changes the total three- to six-fold. Non-thinking models at temperature
   zero are sufficient for the fast and standard tiers of this workload.
3. Task-model fit: local evaluation showed 3-9B models matching or beating
   larger models on enum classification and grounded extraction, so the
   cheap tier is not a quality compromise. Uncensored cheap options exist
   for sensitivity-flagged rows.
4. Recommended paid profile, mirroring the local router: cheap tier
   (llama-3.1-8b class) for fast and standard rows plus a large MoE
   (gpt-oss-120b class) for deep and flagged rows: roughly 2-4 USD total,
   completing in one to three hours instead of weeks.
5. Frontier chat models are 10 to 50 times overpriced for this task and
   add alignment-refusal risk on flagged rows.

---

## 15. Cost and rate-limit research for every remaining layer

The same methodology applied to the vision, deep-curation, and fusion loads,
plus the single-call text option. Vision pricing was re-researched live (256
vision-capable models found on the aggregator; the earlier free-tier VLM
research is retained for context but prices have fallen roughly an order of
magnitude since).

### Measured workloads

| load | calls | input tok | output tok | basis |
|---|---|---|---|---|
| text, 5 calls/row (judge + 4 cards) | 55,000 | 25M | 1.4M lean / 36M thinking | production logs, section 13 |
| text, 1 call/row (all four cards in one JSON) | 11,000 | 7.2M | 2.2M | single prompt carries text+context once instead of five times |
| vision (full) | 3,578 | 4.1M | 1.1M | 3,578 media posts, 4,035 images (1-4 per post), ~1,000 image tokens per downscaled image, ~300 output |
| vision (pending only) | 1,435 | 1.65M | 0.43M | 2,143 already processed on free tiers |
| deep curation | 11,000 | 12.1M | 2.75M | text up to 900 + link content up to 1,500 chars + instructions; ~250 out |
| fusion | 5,200 | 9.9M | 1.04M | rows with media understanding or link content; three signals concatenated, ~200 out |

The single-call text option is doubly attractive on hosted APIs: five times
fewer requests (rate-limit exposure) and roughly 3.5 times less input, since
the post text and link context are carried once instead of repeated per
card. The tradeoff is reliability on small models: this harness's design
rule warns that multi-task prompts degrade 4B-class models (silently
dropping fields), so the merged card is recommended only on hosted mid-size
models and up; the local five-card path remains the reference
implementation.

### Vision rate limits

The vision worker's historical stalls came from the same free-tier caps
(20 requests/minute, 1,000/day on free variants) compounded by
gateway-first-byte timeouts on serverless GPU endpoints (section 7). Paid
options remove both: aggregator paid models have no platform request cap,
and direct Gemini paid tiers allow on the order of thousands of requests per
minute with per-minute token budgets that the entire vision job (about 4M
input tokens) fits inside at the first paid tier. The full vision load is
roughly 3,600 requests, which every paid tier absorbs in minutes to hours.

### Vision cost table (full / pending-only)

| model | $/M in | $/M out | full | pending | class |
|---|---|---|---|---|---|
| nex-n2-mini | 0.025 | 0.10 | 0.21 | 0.08 | small VLM |
| gemma-3-4b-it | 0.05 | 0.10 | 0.31 | 0.13 | 4B VLM |
| gemma-4-31b-it | 0.09 | 0.34 | 0.74 | 0.30 | 31B VLM |
| qwen3-vl-30b-a3b-instruct | 0.15 | 0.60 | 1.26 | 0.51 | 30B MoE VLM |
| qwen3-vl-235b-a22b-instruct | 0.21 | 1.90 | 2.99 | 1.21 | 235B MoE VLM |
| gemini-3.1-flash-image | 0.50 | 3.00 | 5.23 | 2.12 | large VLM |
| claude-sonnet-5 | 2.00 | 10.00 | 19.20 | 7.79 | frontier VLM |
| gemini-3-pro-image | 2.00 | 12.00 | 21.30 | 8.70 | frontier VLM |

### Deep-curation and fusion cost tables

| deep curation (12.1M in / 2.75M out) | cost |
|---|---|
| mistral-nemo | 0.31 |
| llama-3.1-8b-turbo | 0.35 |
| nemotron-3-nano-30b-a3b | 1.16 |
| gpt-oss-120b | 0.92 |
| gemini-3.1-flash-lite | 7.15 |
| claude-haiku-4.5 | 25.85 |

| fusion (9.9M in / 1.04M out) | cost |
|---|---|
| mistral-nemo | 0.22 |
| llama-3.1-8b-turbo | 0.24 |
| gpt-oss-120b | 1.20 |
| gemma-4-31b-it (VLM, reads media too) | 1.24 |
| gemini-3.1-flash-image | 8.00 |
| claude-sonnet-5 | 30.00 |

### Single-call text option (11,000 calls, 7.2M in / 2.2M out)

| model | cost | vs 5-call |
|---|---|---|
| mistral-nemo | 0.20 | 0.52 |
| llama-3.1-8b-turbo | 0.23 | 0.56 |
| gpt-5-nano (batch) | 0.62 | 0.91 |
| gpt-oss-120b | 0.61 | 1.16 |
| gemini-3.1-flash-lite | 5.10 | - |
| claude-haiku-4.5 | 18.20 | 32.00 |

### Master summary: cheap / middle / best per load

Recommended profiles for running every remaining layer of the full dataset
on paid hosted APIs (USD, approximate):

| load | cheap | middle (recommended) | best quality |
|---|---|---|---|
| text 5-call | nemo / llama-8b: 0.5-0.6 | gpt-oss-120b: 1.2-3.1 | claude-haiku: 32 (overkill) |
| text 1-call | nemo / llama-8b: 0.2 | gpt-oss-120b: 0.6 | gemini-3.1-flash-lite: 5.1 |
| vision (pending 1.4k) | gemma-3-4b: 0.13 | qwen3-vl-30b / gemma-4-31b: 0.3-0.5 | gemini-3.1-flash-image: 2.1 |
| deep curation | nemo: 0.31 | gpt-oss-120b / nemotron-30b: 0.9-1.2 | gemini-3.1-flash-lite: 7.2 |
| fusion | nemo: 0.22 | gpt-oss-120b / gemma-4-31b: 1.2 | gemini-3.1-flash-image: 8.0 |
| **all layers, one profile** | **about 1.1-1.6 total** | **about 4-7 total** | **about 50-75 total** |

A complete, fully-enriched dataset — every row routed, judged, curated, and
fused, with all images understood — costs roughly 1 USD at the cheap
profile, 4-7 USD at the recommended middle profile, and 50-75 USD at
best-quality. All paid tiers absorb the request volumes; only the free
variants (20/minute, 1,000/day) cannot.

---

## 16. Notes

- Use in accordance with the platform's terms and applicable law; the tool
  enumerates only data visible to the authenticated account and is intended
  for personal archival and research.
- No telemetry. All data stays local except explicit external router legs,
  which receive only the post-text excerpt needed for tier judgment.
