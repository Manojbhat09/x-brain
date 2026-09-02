# x-brain Local Model Recommendations (final)

**GPU:** NVIDIA GeForce GTX 1660 Ti **6 GB** (Xwayland hovers ~659 MiB → ~5.2 GB usable for models).
**Disk fix:** C-drive reclaimed from crashes; ollama store `<ollama-store>`; winner GGUF archive `<archive-dir>/`.

All numbers measured on **quality eval** (N=12 or N=8 gold tweets, temp=0, determinism all ≈1.0).

| key | meaning |
|---|---|
| topic | gold topic-category agreement (x/x) |
| F1 | extraction F1 vs gold entities (avg over tasks) |
| obj | extraction objectivity — fraction of emitted entities actually present in tweet/context (0–1, higher=less fabrication) |
| rel | self_relevance binary agreement with gold (x/x) |
| avg_s | avg wall-clock per task on GPU (inc. thinking + answer) |
| det | determinism (re-run 1/3 of prompts → % identical) |
| think | total thinking characters emitted (reasoning chains) |
| bench valid | JSON-valid outputs on the 9/15-task speed bench |

---

## RANKED RECOMMENDATION (composite value = 0.35·topic + 0.30·F1 + 0.20·obj + 0.15·rel, ÷ speed)

### 🥇 1. nemotron3-nano — PRIMARY / fast bulk
- **topic** 11/12 (.92) · **F1** .534 · **obj** .866 · **rel** 6/12 (.50) · **avg** 6.1s · **det** 1.0 · bench 20/20
- size 2.8G · VRAM ~3.3G (fits GPU) · est. ~970 tok/s reasoning
- **Why it wins:** highest topic **and** highest extraction F1 of any evaluated model, with near-best objectivity (.866) and fast 6.1s. Best quality/seconds (~9× better value than qwen3:4b).
- **Best for:** the default **tag_topic** and **extract_entities** cards on the bulk drain. First-line every tweet.
- Exclusive features: VL-capable (128K ctx arch), nano footprint leaves VRAM headroom.

### 🥈 2. granite-4.2-3b-q8 — SELF_RELEVANCE / objectivity specialist
- **topic** 9/12 (.75) · **F1** .442 · **obj** **1.000** (perfect) · **rel** 8/12 (tied-best) · **avg** 27.8s · **det** 1.0
- size 3.9G · VRAM ~4.4G (fits GPU) · est. ~1345 tok/s reasoning
- **Why it wins:** **objectivity 1.0 — zero fabricated entities** (only model to achieve this), tied-highest self_relevance (8/12).
- **Best for:** the **self_relevance** card, and as a trust-heavy 'grounding validator' when fabrication risk matters (e.g., financial claims).
- Trade-off: 4.5× slower than nemotron; use only where grounding > speed.

### 🥉 3. polaris-v1 — high-objectivity generalist (fast)
- **topic** 10/12 (.83) · **F1** .375 · **obj** .983 · **rel** 8/12 (.67) · **avg** 5.5s · **det** 1.0 · bench 10/10
- size 3.5G · VRAM ~4.0G (fits GPU) · est. ~1070 tok/s
- **Why it wins:** near-perfect objectivity (.983) **and** fast (5.5s) — combines granite-grade grounding with nemotron-grade speed; good relevance (8/12).
- **Best for:** a second opinion / ensemble vote on topic+extract, or where you want grounding without granite's latency. Highest value/s among grounded models.

### 4. granite-4.1-3b-q8 — ultra-fast topic / value-per-second king
- **topic** 7/8 (.88) · **F1** .083 · **obj** .250 · **rel** 3/8 (.38) · **avg** **0.6s** · **det** 1.0 · bench 7/10
- size 3.6G · VRAM ~4.1G (fits GPU) · est. ~300 tok/s (pure output, no thinking)
- **Why it wins:** fastest model on the machine by a wide margin (0.6s/task). If you only need a quick category label and can tolerate weak entity grounding, this is the cheapest call.
- **Best for:** high-volume low-depth classification, warm-up/keepalive, or the 'reject/detect' front-door. Not for entity grounding.

### 5. qwen3.5-4b-super-coder-q4 — fast, strong-topic, no thinking
- **topic** 7/8 (.88) · **F1** .289 · **obj** .753 · **rel** 5/8 (.62) · **avg** 5.7s · **det** 1.0 · bench 9/9 ✓ full validity
- size 2.6G · VRAM ~3.1G (fits GPU) · est. ~1030 tok/s
- **Why it wins:** the only **GPU-fit** candidate that passed — 9/9 valid JSON (best schema reliability of the batch), strong topic (7/8), decent grounding (.753), fast (5.7s), zero fragile thinking chains.
- **Best for:** a reliable non-reasoning fast worker; good default if nemotron is busy. Architected as a coder → very clean structured output.

### 6. qwen3-4b-thinking-2507-q8 — deep / slow tier (max grounding + relevance)
- **topic** 10/12 (.83) · **F1** .143 · **obj** **1.000** · **rel** **10/12** (best) · **avg** 88.9s · **det** 1.0
- size 4.3G · VRAM ~4.8G (fits GPU) · est. ~450 tok/s
- **Why it wins:** best self_relevance (10/12) + perfect objectivity (1.0). Deep reasoning quality; but expensive (88.9s/task).
- **Best for:** a **deep-review tier** — re-examine low-confidence or edge tweets the fast tier flagged. Quality over throughput.

### 7. ornith-1.5-9b-q4km — max topic agreement (with caution)
- **topic** **12/12 (1.00)** highest · **F1** .355 · **obj** .606 ⚠️ · **rel** 9/12 (.75) · **avg** 43.1s · **det** 1.0 · bench 10/10
- size 5.8G · VRAM ~6.4G (does **not** fit 6G fully → partial CPU offload, slow) · est. ~315 tok/s
- **Why it wins:** perfect gold-topic agreement (12/12) and high relevance. **Flagship for 'does the topic match', BUT objectivity .606 is the lowest-margin keeper — it fabricates entities ~40% of the time.**
- **Best for:** topic classification where category accuracy (not entity grounding) is the priority. Use with entity post-validation. Oversized for the GPU → expect slow partial-offload.

### 8. qwen38-distill — fast generalist (reasoning)
- **topic** 10/12 (.83) · **F1** .219 · **obj** .815 · **rel** 7/12 (.58) · **avg** 18.0s · **det** 1.0 · bench 23/23
- size 3.6G · VRAM ~4.1G (fits GPU) · est. ~910 tok/s · 64,894 thinking chars
- **Best for:** a balanced mid-speed reasoning worker; good general card when you want reasoning without the 50–90s cost of qwen3:4b/qwen-thinking. Reasonably grounded (.815).

### 9. deepseek-r1:7b — objectivity fallback
- **topic** 9/12 (.75) · **F1** .333 · **obj** .958 · **rel** 1/12 (.08) ⚠️ · **avg** 23.2s · **det** 1.0
- size 4.7G · VRAM ~5.2G (partial) · est. ~640 tok/s
- **Why:** near-perfect grounding (.958) — good reserve for entity-extract when you want low fabrication. BUT self_relevance is its sore spot (1/12). 7B straddles the 6G VRAM.
- **Best for:** fallback extract (grounding priority); not for relevance.

### 10. qwen3:4b — legacy baseline
- **topic** 10/12 (.83) · **F1** .215 · **obj** .667 ⚠️ · **rel** 8/12 (.67) · **avg** 53.8s · **det** 1.0
- size 2.5G · VRAM ~3.0G (fits GPU) · est. ~1420 tok/s · 304k thinking chars
- **Why:** historically the original primary; now outperformed by nemotron on every axis except raw relevance tie. Kept for continuity/regression.
- **Best for:** legacy repro / ablations; de-prioritized for new work.

### 11. deepseek-r1:1.5b — tiny / reserve
- **topic** 6/12 (.50) · **F1** .439 · **obj** .818 · **rel** 3/12 (.25) · **avg** 4.5s · **det** 1.0
- size 1.1G · VRAM ~1.5G (fits GPU) · est. ~3000 tok/s (fastest raw)
- **Best for:** ultra-light, ultra-fast tasks where accuracy is secondary; warm-start / low-power / edge. Decent F1 for its size but weak category agreement and relevance.

### 12. small-8b-liquid-q4km — LFM2.5-8B-A1B Liquid Q4_K_M — best small F1
- **topic** 6/8 (.75) · **F1** **.609** · **obj** .938 · **rel** 5/8 (.62) · **avg** 12.0s · **det** 1.0 · bench 9/9
- size 4.80G · VRAM ~5.3G (partial, 4.8G >4.4G threshold, straddles) · est. ~620 tok/s · Q4_K_M · `LiquidAI/LFM2.5-8B-A1B-GGUF` `LFM2.5-8B-A1B-Q4_K_M.gguf`
- **Why it wins:** highest F1 among small models (.609), high obj .938, 8B MoE via Q4_K_M fits 5.2G usable (partial offload). Best small extractor.
- **Best for:** small-pipeline `extract_entities` where F1 > speed.

### 13. small-lfm26-q5km — LFM2.5-2.6B Code Q5_K_M — efficient small
- **topic** 7/8 (.88) · **F1** .502 · **obj** .958 · **rel** **7/8** (.88) · **avg** 18.8s · **det** 1.0
- size 1.94G · VRAM ~2.4G (fits fully) · est. ~350 tok/s · Q5_K_M · `bunnycore/LMF-2.5-2B-Code-GGUF` `LFM2.5-2.6B.Q5_K_M.gguf`
- **Why it wins:** strong obj .958 and perfect rel 7/8 for 1.94G; fully fits VRAM, efficient. But slow (18.8s, 121k think).
- **Best for:** small `self_relevance` where grounding matters and 2G footprint is priority.

### 14. small-8b-gaston-q4km — LFM2.5-8B-A1B Gaston Uncensored Q4_K_M — perfect topic
- **topic** **8/8 (1.00)** · **F1** .479 · **obj** .975 · **rel** 4/8 (.50) · **avg** 14.4s · **det** 1.0 · bench 9/9
- size 4.80G · VRAM ~5.3G (partial) · est. ~580 tok/s · Q4_K_M · `gaston-parravicini/LFM2.5-8B-A1B-Uncensored-Gaston-GGUF` `LFM2.5-8B-A1B-Uncensored-Gaston-Q4_K_M.gguf`
- **Why it wins:** only small with perfect topic 8/8, near-perfect obj .975, uncensored Gaston variant. Good for `tag_topic` at small scale.
- **Best for:** small `tag_topic` bulk where category accuracy is priority.

### 15. small-8b-unsloth-q4km — LFM2.5-8B-A1B Unsloth Q4_K_M — perfect obj
- **topic** 6/8 (.75) · **F1** .441 · **obj** **1.000** · **rel** 5/8 (.62) · **avg** 10.4s · **det** 1.0 · bench 9/9
- size 4.96G · VRAM ~5.5G (partial, tight) · est. ~650 tok/s · Q4_K_M · `unsloth/LFM2.5-8B-A1B-GGUF` `LFM2.5-8B-A1B-UD-Q4_K_M.gguf`
- **Why it wins:** only small with perfect obj 1.0 (zero fabrication), decent F1 .441. 8B MoE Unsloth UD quant.
- **Best for:** small `extract_entities` where fabrication-free > raw F1.

### 16. small-qwen35-q4 — Qwen3.5-4B-Super-Coder Q4_0 — completes #12 (duplicate of #5)
- **topic** 7/8 (.88) · **F1** .289 · **obj** .753 · **rel** 5/8 (.62) · **avg** 6.0s · **det** 1.0 · bench 9/9
- size 2.43G · VRAM ~2.9G (fits fully) · est. ~1030 tok/s · Q4_0 · `jica98/qwen3.5-4B-super-coder` `qwen3.5-4B-super-coder.Q4_0.gguf`
- **Why:** completes `models_small.md` #12 `jica98/qwen3.5-4B-super-coder` as `small-qwen35-q4`; same underlying model as rank #5 `qwen3.5-4b-super-coder-q4` (duplicate kept for small 5/5 completeness per user request).
- **Best for:** small fast coder, clean JSON (9/9 valid).

---

## Suggested pipeline wiring (per-card)
| card | model | why |
|---|---|---|
| tag_topic (bulk) | **nemotron3-nano** | best topic + F1, fast |
| extract_entities (bulk) | **nemotron3-nano** | best F1/obj balance |
| self_relevance | **granite-4.2-3b-q8** (alt polari-v1) | obj 1.0, tie-best rel |
| deep-review tier | **qwen3-4b-thinking-2507-q8** | best rel + obj 1.0 |
| topic-only high-volume | **granite-4.1-3b-q8** | 0.6s, 7/8 topic |
| ensemble vote | nemotron + polari + qwen3.5-supercoder | diversity |

---

## Deleted (do not use) — with reason
| model | numbers | reason |
|---|---|---|
| qwen35-4b (AtomicChat) | topic 5/8, obj 0.0, 73s | no valid JSON at all |
| ai21-jamba-reasoning-3b | topic 0/8, obj 0.0 | no valid JSON |
| agents-a1-4b | valid 2/9, obj 0.0, 67.6s | huge think, broken schema |
| vibethinker (both) | 0/9, obj 0.0 | marketing 'thinking', no output |
| claude-sonnet5-llama3.2 | topic 3/8, F1 0, obj .5 | fails schema |
| llama3.2-abliterated | topic 4/8, F1 0, obj .375 | weak/unreliable output |
| h2o-danube3.1-4b | topic 4/8, obj .333 | weak |
| nanbeige4.2-3b | valid 3/9, 75k think | slow + broken |
| asl-4b | valid 5/9, 83k think | heavy think, weak |
| phi4-mini-reasoning | 0/9 | broken JSON |
| falcon3-7b | 8/9 valid but 0 think | not reasoning |
| jan-nano-4b | 0 think | not reasoning |
| nuextract-v1.5 | topic 5/12 | weak overall |
| gemma-4-E2B | — | download failed ×3 (never evaluated) |
| small-lfm26-think-q8 | topic 7/8, F1 .475, obj .583, 20.4s | deleted per user 2026-08-31; superseded by Q4_K_M 8B |
| small-lfm12-nova-q8 | topic 2/8, F1 .225, obj .250, 0.6s | deleted per user 2026-08-31; weak |
| small-lfm12-f16 | topic 5/8, F1 .188, obj .188, 1.1s | deleted per user 2026-08-31; weak |

---

## Storage & perf notes
- **VRAM budget:** only models ≤ ~4.4G blob fit 100% GPU (nemotron, granite-4.1/4.2, polari, qwen3.5-super, qwen38, r1:1.5b, qwen3:4b). Larger (r1:7b 4.7G ~partial, ornith 5.8G no) straddle CPU → slower.
- **Reasoning cost:** thinking-heavy models (qwen3:4b, ornith, r1:7b, qwen3-thinking) are 4–15× slower per task than nemotron/granite-4.1.
- **C-drive:** freed ~47GB (19G scratch + 28G orphan blobs); 756G free. Models kept on ext4 `ollama-models` (fast), winners archived as GGUF on `<archive-dir>`.
- Downloads now route to `<archive-dir>/ggufs` (user preference); `<archive-dir>` ~16G free (after small 8B Q4_K_M batch, 97G used of 112G).
