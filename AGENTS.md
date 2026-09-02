# AGENTS.md — x-brain harness

This file is for any agent (or human) operating the repo. Read it before touching code or running drains.

## What this is

A local-first, 5-level pipeline that turns any X account's reposts into `state.sqlite` + an Obsidian `vault/` mind graph. Deterministic harness owns control flow; models are pure `(card, input)->JSON` slot-fillers. See `harness/README.md` (full spec) and `SKILL.md` (skill card).

## Repo map

```
.                          # submit root — push this
  .env.example             # ← copy to .env (never commit .env)
  .gitignore               # excludes .env, creds.json, state.sqlite*, vault/, *.log
  README.md                # quick start (install → configure → run)
  SKILL.md                 # agent skill card (triggers, rules, pipeline cheat-sheet)
  AGENTS.md                # this file
  LEARNINGS-gateway-504.md # vision 504 engineering notes (warm-ping, downscale, stream)
  config/
    models.json            # routing_tiers + eval provenance — the only place to swap models
    models.md              # 24-model eval table that chose the defaults
  examples/
    limits.json.example    # rate-limit window persistence shape
    tombstones.jsonl.example
    rerun_tids.txt.example
    vision.log.example
  harness/
    requirements.txt       # requests
    xb.py                  # CLI — every command enters here
    xbrain/
      protocol.py          # GraphQL wire: FEATURES, QID_REPOSTS, TID, build_url/headers
      session.py           # creds + user_id: env/.env/config.json/--flag/prompt (0600)
      lane.py              # GraphQLLane primary + FxLane fallback, AuthRotted
      ratelimit.py         # token-bucket + jitter + per-op windows + circuit breaker
      enum.py              # L1 enumerator (double-empty EOF, checkpointed)
      enrich.py            # L2 enricher
      store.py             # SQLite FSM, leases, kv, pending_* queries, tombstones
      links.py             # L3 link resolver (t.co → title/text, boilerplate strip)
      llm.py               # L5 cards + deep curation + fusion (schemas, budgeting)
      backends.py          # Ollama / InferX / OpenRouter / LiteLLM / NemotronVL + default_backends()
      router.py            # per-row router (hybrid/ext-int-int/int-ext-int, _escalate)
      vision.py            # L4 VisionWorker (downscale, warm, stream, 10-min backoff)
      graph.py             # Obsidian vault + graph.json exporter
  run_all.sh               # supervisor: enrich→llm→retry (respects XBRAIN_DIR)
  run_text_ox.sh           # resilient llm-run --workers 4 loop → $XBRAIN_DIR/llm_ox.log
  run_links_deep.sh        # links-run → deep-run chain
  run_vision.sh            # resilient vision-run --workers 2 loop
```

## How to run (agent workflow)

1. **Configure** — never hard-code. `cp .env.example .env` or pass `--brain-dir`/`--user-id` flags. Keys: `X_USER_ID` (numeric), `X_AUTH_TOKEN`, `X_CT0` (both from DevTools → Application → Cookies → x.com). Optional `INFERX_API_KEY`, `OPENROUTER_API_KEY`, `XBRAIN_DIR`, `XBRAIN_CATALOG`. See `session.py:get_brain_dir`, `load_creds`, `require_user_id`.
2. **Doctor** — `python3 harness/xb.py --brain-dir ./data doctor` probes reposts; `--posts-only` probes `UserTweets` via `protocol.build_url(kind=posts)`. Via `_make_lane()` → `lane.fetch_page(uid, kind)`.
3. **L1→L2→L3→L4→L5→deep→fuse→graph** — in order; `fuse-run` must be last (needs all signals). All stages resume cleanly (`store.py:SCHEMA`, per-kind `enum_cursor`/`enum_cursor_posts`, `expire_llm_leases()`). `--posts-only` (`xb.py:enum --posts-only`) drives `UserTweets` → filters to originals only (reposts dropped), stored with `kind='post'`; reposts remain `kind='repost'`. Comments/replies are **not** handled here — they need `SearchTimeline`/`UserTweetsAndReplies` + ancestor BFS (design note in `xbrain/enum.py:1`). See `harness/README.md:3` diagram.
4. **Routing** — `llm-run --route --mode hybrid|ext-int-int|int-ext-int --backends ollama,inferx,or-nemotron --judge-model nemotron3-nano --judge-backup granite-4.2-3b-q8 --catalog ./config/models.json`. Tiers entirely from `config/models.json:routing_tiers`. Startup prints `AVAILABLE`/`MISSING`. Per-row audit in `route_tier`/`route_json`/`model_used` + `drain_calls.log`.
5. **Monitor** — `tail -f $XBRAIN_DIR/drain_calls.log` (every call) + `xbrain_drain.log` (per-row `[drain] N/TOTAL | tier | id | cards ok`). `sqlite3 $XBRAIN_DIR/state.sqlite "SELECT stage, COUNT(*) FROM tweets GROUP BY stage"` is ground truth. If drain log stalls but calls log advances, it's a `deep` row (`qwen3-4b-thinking` 214-375s) — check `curl http://localhost:11434/api/ps`.
6. **Cleanup** — never `sed -i` an open drain log (detaches fd → `(deleted)`). Never commit `state.sqlite*`, `vault/`, `*.log`, `.env`.

## Where to look (by task)

| Task | File |
|------|------|
| change target user | `xbrain/session.py:load_user_id`, `xbrain/protocol.py:build_url(kind)`, `harness/xb.py:cmd_auth/cmd_enum/cmd_doctor` |
| change headers / QIDs / FEATURES | `xbrain/protocol.py:22-85` (QID_REPOSTS/QID_POSTS rotate; override via `X_QID_POSTS` env) |
| rate limits / 429 handling | `xbrain/ratelimit.py`, `xbrain/lane.py:TxRateLimiter` |
| DB schema / quarantine / leases | `xbrain/store.py:SCHEMA`, `save_*`, `pending_*`, `expire_llm_leases` |
| cards / schemas / fusion | `xbrain/llm.py:STUDY_PROMPT`, `*_SCHEMA`, `build_link_context`, `fuse_row`, `LlmWorker` |
| router / escalation / tiers | `xbrain/router.py:_heuristic`, `_escalate`, `_route_uncached`, `model_for/models_chain_for`, `DEFAULT_TIERS` |
| backends / keys / pacing | `xbrain/backends.py:default_backends`, `InferXBackend`, `OpenRouterBackend`, `OllamaBackend`, `NemotronVLBackend` |
| vision / 504 fix | `xbrain/vision.py:VisionWorker`, `xbrain/backends.py:NemotronVLBackend._warm/_pace`, `LEARNINGS-gateway-504.md` |
| graph export | `xbrain/graph.py:GraphExporter`, `harness/xb.py:cmd_graph` |
| model eval / catalog | `config/models.json`, `config/models.md` |

## Conventions for agents

- Respect the harness/model split — tighten enums / add examples / shrink input on failures; never loosen validation (`harness/README.md:1`).
- Validate JSON against schemas before storing; retry ×2 with error appended, then quarantine.
- Keep `state.sqlite` as single-writer WAL; `store.py` is the only writer.
- Rate-limit headers (`limits.json`) and `xbrain_drain.log` are monotonic — append, checkpoint, resume.
- All paths honour `$XBRAIN_DIR` / `--brain-dir` / `.env`; never assume `~/x-brain` or `~/.xbrain` in new code — use `session.get_brain_dir()`.
- When adding a model: `ollama create <tag> -f Modelfile` → edit `config/models.json:routing_tiers` → restart → confirm `AVAILABLE` line → grep `model_used` to verify routing.
- Before pushing: `grep -R "1637425\|GFaang\|/home/mbhat" --include="*.py" --include="*.sh"` must be clean; `ls state.sqlite* *.log vault/` must be empty (`.gitignore` does this, but check).

## Commands the agent should know

```bash
python3 harness/xb.py --help
python3 harness/xb.py --brain-dir ./data auth --auth-token ... --ct0 ... --user-id 123
python3 harness/xb.py --brain-dir ./data doctor
python3 harness/xb.py --brain-dir ./data doctor --posts-only
python3 harness/xb.py --brain-dir ./data doctor --posts-reposts
python3 harness/xb.py --brain-dir ./data enum --resume --max-pages 2
python3 harness/xb.py --brain-dir ./data enum --posts-only --resume --max-pages 2
python3 harness/xb.py --brain-dir ./data enum --posts-reposts --resume --max-pages 2  # both timelines
python3 harness/xb.py --brain-dir ./data stats   # shows reposts/posts cursors + kind breakdown
python3 harness/xb.py --brain-dir ./data graph-export --out ./data/vault --min-cooccur 2 --min-mentions 2
# drain variants
python3 harness/xb.py --brain-dir ./data llm-run --route --mode hybrid --backends ollama
python3 harness/xb.py --brain-dir ./data llm-run --route --mode int-ext-int --backends ollama,inferx,or-nemotron
```

See `harness/README.md:9` for full flag table and `harness/README.md:11` for resilience guarantees.
