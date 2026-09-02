# x-brain — quick start

Turn any X account's reposts into a queryable knowledge base + mind graph.

> **No username or key is shipped.** Copy `.env.example` → `.env` and fill it. Every secret stays local.

## 1. Install

```bash
git clone <this-repo> && cd x-brain-submit
python3 -m venv .venv && source .venv/bin/activate
pip install -r harness/requirements.txt
ollama serve &  # or your Ollama install
```

Pull the models you want (see `config/models.json` for the tested roster; any GGUF registered in Ollama works — edit the JSON and restart):

```bash
ollama pull nemotron3-nano   # or: ollama create nemotron3-nano -f Modelfile
```

## 2. Configure

```bash
cp .env.example .env
# edit .env:
#   X_USER_ID=...            # numeric X user id (https://tweeterid.com/)
#   X_AUTH_TOKEN=...         # from DevTools → Application → Cookies → x.com → auth_token
#   X_CT0=...                # same place → ct0
#   XBRAIN_DIR=./data        # optional, default ~/.xbrain
#   INFERX_API_KEY=...       # optional, for router/vision
#   OPENROUTER_API_KEY=...   # optional, for vision/fallback
```

Alternatively per-command:

```bash
python3 harness/xb.py --brain-dir ./data --user-id 123456 auth --auth-token ... --ct0 ... --user-id 123456
```

Or via env: `X_USER_ID=123 python3 harness/xb.py doctor`

## 3. Run

```bash
# verify creds + one live page
python3 harness/xb.py --brain-dir ./data doctor
python3 harness/xb.py --brain-dir ./data doctor --posts-only   # same for posts timeline

# L1: enumerate reposts — or posts with --posts-only (originals only, separate cursor)
python3 harness/xb.py --brain-dir ./data enum --resume
python3 harness/xb.py --brain-dir ./data enum --posts-only --resume

# L2: thread context (Fx fallback built-in)
python3 harness/xb.py --brain-dir ./data enrich

# L3: resolve t.co links
python3 harness/xb.py --brain-dir ./data links-run

# L4: media understanding (optional, needs vision keys)
python3 harness/xb.py --brain-dir ./data vision-run

# L5: routed LLM cards (4 cards/row, quarantines on failure)
python3 harness/xb.py --brain-dir ./data llm-run --route --mode int-ext-int --backends ollama,inferx,or-nemotron

# curation + fusion (run after L3/L4/L5)
python3 harness/xb.py --brain-dir ./data deep-run
python3 harness/xb.py --brain-dir ./data fuse-run

# mind graph
python3 harness/xb.py --brain-dir ./data graph-export --out ./data/vault
# open ./data/vault in Obsidian
```

`./run_all.sh`, `./run_text_ox.sh`, `./run_vision.sh`, `./run_links_deep.sh` are resilient wrappers (auto-restart, log to `$XBRAIN_DIR/*.log`).

## 4. Config as code

`config/models.json` drives all model selection (`routing_tiers` fast/standard/deep/uncensored). Edit it and restart:

```jsonc
{
  "routing_tiers": {
    "fast":       {"tag_topic": "granite-4.1-3b-q8", "extract_entities": "qwen3.5-4b-super-coder-q4", ...},
    "standard":   {"tag_topic": "nemotron3-nano", ...},
    "deep":       {"tag_topic": "ornith-1.5-9b-q4km", ...},
    "uncensored": {"tag_topic": "small-8b-gaston-q4km", ...}  // or null to disable
  }
}
```

The harness checks `ollama list` on start (`AVAILABLE`/`MISSING → falls back to standard`) so any Ollama tag can be dropped in.

## 5. Examples

```bash
python3 harness/xb.py --help
python3 harness/xb.py llm-run --help
python3 harness/xb.py graph-export --help
ls examples/   # tombstones, limits, rerun list samples
cat config/models.md   # evaluation that chose the defaults (Q4/Q8, 6GB GPU)
cat harness/LEARNINGS-gateway-504.md  # vision gateway engineering notes
```

## 6. Data layout

```
$XBRAIN_DIR/          # ./data by default
  state.sqlite        # single-writer WAL, single source of truth
  creds.json  (0600)  # auth_token + ct0
  config.json (0600)  # user_id
  limits.json         # rate-limit windows persisted across runs
  cache/              # tid pair cache
  quarantine/         # *.skip.json — never dropped, requeueable
  vault/              # Obsidian vault + graph.json
```

## 7. Security

- No default username/id — `protocol.py` has no hard-coded user. Missing id → prompt or `X_USER_ID` env/`.env`.
- No keys shipped — `session.py` reads `X_AUTH_TOKEN`/`X_CT0`/`INFERX_API_KEY`/`OPENROUTER_API_KEY` from env/`.env` or `creds.json` (0600).
- `.gitignore` excludes `.env`, `creds.json`, `state.sqlite*`, `vault/`, `*.log`.
- See `.env.example` for every knob.
