# Scout — Technical Improvement Plan

> Date: 2026-10-01 · Scope: `api/app/*` (search → fetch → extract → verify → dedupe → coverage)
> Inputs: code review of the repo, 4 fresh runs on the live site (https://scout-platform.onrender.com),
> the same 4 tasks run through Claude Code `WebSearch`, and a short survey of how deep/wide research agents do search.
> Companion docs: [`SCOUT_VS_CLAUDE_CODE_WEBSEARCH.md`](./SCOUT_VS_CLAUDE_CODE_WEBSEARCH.md) (comparison),
> raw run data in [`eval/live_compare/`](./eval/live_compare/).

The plan is split into **MUST DO** (Scout gives wrong answers or no answer without these) and **OPTIONAL**
(makes it better, faster, or more competitive). Each item has: *problem → evidence → fix → how to verify*.
Items marked **[DONE locally]** are already implemented in the working tree (not committed or deployed yet).

---

## 0. Why Scout failed in the live runs (TL;DR)

| Case | Live result | Root cause |
|---|---|---|
| A — open-source VLMs | 0 rows, status `done` | Search returned eye-health pages ("vision"); J1 gated 151/153 chunks |
| B — Indian edtech seed rounds | 0 rows, status `done` | Search returned seed.com, probiotics, garden seeds; J1 gated 127/127 |
| C — Django/Flask/FastAPI versions | 2 rows, Django = `3.0.14` (wrong), Flask/FastAPI missing | Picked an old version from a list page; stopped after 2 captures with "100% coverage" |
| D — Student hackathons India 2025 | 7 rows, 4 are the same event, `�` in text | Weak entity resolution; encoding bug |

The common thread: **the search layer is the weakest link, and the rest of the pipeline doesn't notice when it
fails.** The verification layers (quotes, Jev, KBT) are good, but they can only filter; they cannot fix bad
discovery. On these four tasks, a single Claude Code `WebSearch` call gave more correct answers in seconds.

Five concrete causes, in order of impact:

1. **Over-constrained queries.** The intent prompt told the LLM to use `site:` operators, and it stacked quoted phrases:
   `site:inc42.com "seed" "edtech startup" "India" 2024`. Through the same `ddgs` library, 2 of 3 such queries returned
   *No results found*. The junk pages in the live runs most likely came from what the engine returned for these
   over-constrained queries; I can't see the Render logs to confirm exactly how.
2. **Static plan.** Queries are fixed after intent parsing; the loop only recombines them with engines/source types.
   Nothing reacts to "this capture found nothing".
3. **Fake capture occasions.** `_ENGINES = ["ddg","brave","searxng"]` × `_SOURCE_TYPES = ["general","news"]`, but
   unconfigured engines fall back to DDG and `news` changes nothing → the same search is run up to 6× and counted
   as 6 independent Chao2 captures.
4. **Silent failure.** 0 records → `status="done"`, `error=null`.
5. **Weak post-processing.** Stopping rule fires on 2 captures/2 entities; entity keys don't merge
   "X (SIH) 2025" / "X 2025: tagline"; "latest" fields take an arbitrary list item; text decoded with replacement chars.

---

## 1. How others build web search for agents (what to borrow)

Short survey; links at the end.

- **Iterative ReAct loop, not a one-shot plan.** Deep-research agents alternate *think → search → read → re-plan*:
  each observation changes the next query ([Survey 2508.12752], [2506.18959]). Scout plans once.
- **Query decomposition.** Turn a request into atomic sub-questions / facets (per year, per sub-sector, per source
  type) and search each ([dHiebl/Deep_Research], [Survey]).
- **Rerank before reading.** A cheap reranker scores (sub-query, result) pairs and only the top-k are fetched; this
  cuts token cost a lot ([Rerank Before You Reason 2601.14224], [Search-rubric reranker 2608.03527]). Scout fetches in
  engine order and uses Jev only *after* fetching.
- **Wide search = map-reduce.** For "find all X" tasks, WideSearch shows even frontier agents reach only a ~5% full-table
  success rate; Item-F1 improves with more parallel attempts, but completeness is the bottleneck ([WideSearch 2508.07999]).
  Agentic MapReduce / swarm approaches split the entity space and fill rows in parallel ([A-MapReduce 2602.01331],
  [WebSwarm 2607.08662]). This is exactly Scout's niche: **discover entities wide, then fill columns deep.**
- **Use an LLM-oriented search API.** Tavily returns cleaned, LLM-ready snippets; Exa does embedding (neural) search
  and can return page contents in the same call; Brave has an independent index with a free tier
  ([comparison 1], [comparison 2]). `ddgs` is an unofficial scraper and breaks or degrades on datacenter IPs.

---

## 2. MUST DO

### M1. Plain-language, operator-light queries **[DONE locally]**

- **Problem:** `INTENT_SYSTEM` pushed `site:` queries; the model stacked quotes → empty or junk results.
- **Fix (done):** `llm.py::INTENT_SYSTEM` now asks for 4–10 word natural-language queries, no quotes or boolean operators,
  at most one `site:` (preferably none), different *angles* (roundups, news, directories/leaderboards, primary sources),
  and always including entity type + key filters.
- **Verify:** run `parse_intent` on the 4 benchmark prompts; assert no query contains `"` and ≤1 contains `site:`.

### M2. Query relaxation fallback **[DONE locally]**

- **Problem:** even good planners sometimes over-constrain.
- **Fix (done):** `llm.relax_query()` strips `site:`, quotes, `AND/OR/NOT`, `-term`. `discover_urls_for_query()` now
  searches once and, if `< 3` results, retries with the relaxed query and merges.
- **Evidence:** relaxed versions of the failing queries returned 8 and 7 results locally (one still returned none,
  which M3 handles).
- **Tests:** `test_search_quality.py::test_relax_query_*`.

### M3. Adaptive re-planning when a capture yields nothing **[DONE locally]**

- **Problem:** the loop burned 12 captures on the same bad query pool.
- **Fix (done):** after a capture with `new_in_capture == 0`, `llm.replan_queries(dataspec, tried, found_entities)`
  asks the intent model for 3 *new-angle* queries (operator-free, deduped against tried ones), appended to
  `dataspec.search_queries`; capped by `MAX_REPLANS = 3`; emits an SSE `replan` event.
- **Next step (still to do):** also re-plan when **≥ 90% of a capture's chunks were gated by J1**. That is a stronger
  "off-topic" signal than zero new records and costs nothing extra (Jev already computed it).
  Show `replan` events in the dashboard timeline.

### M4. Count only real, independent capture occasions **[DONE locally]**

- **Problem:** Chao2 assumes independent captures; Scout counted DDG-fallback reruns and no-op `news` as new ones →
  coverage looked better than it was.
- **Fix (done):** `_available_engines()` only returns engines that are configured (`ddg` + Brave if key + SearXNG if URL);
  `_SOURCE_TYPES = ["general"]`.
- **Still to do:** inside `_discover_brave/_discover_searxng`, when they fall back to DDG, record the *actual* engine
  in `log_capture` and skip the capture if `(query, actual_engine)` was already used.

### M5. Fail loudly on empty runs **[DONE locally]**

- **Problem:** 0 records looked like success.
- **Fix (done):** at the end of `run_pipeline`, 0 rows → `status="failed"` with a human-readable error
  ("N pages found over K searches, but G of T sections were judged irrelevant… try a more specific topic/place/year").
  This uses the graceful error UI from #29.

### M6. Stopping rule needs enough evidence **[DONE locally]**

- **Problem:** case C stopped with "100% (CI 0.8–1.0)" after 2 captures and 2 rows (one a duplicate).
- **Fix (done):** stop on coverage only if `T >= MIN_CAPTURES_FOR_STOP (3)` and `s_obs >= min(target_count, 5)`.
- **Still to do (UI):** hide the coverage % or show "insufficient data" when `T < 3` or `f2 == 0` (Chao2 bias-corrected
  form is unstable then). Show `s_obs / ŝ` as "found ~X of ~Y" instead of a precise-looking %.

### M7. Strict value-in-quote matching **[DONE locally]**

- **Problem:** `_value_in_quote` matched substrings (`"2"` in `"12"`), so numbers could pass with unrelated quotes;
  the benchmark doc shows `61.2` "supported" by a quote that doesn't contain it.
- **Fix (done):** whole-token match (regex with alnum boundaries); every value token must be a token in the quote.
- **Still to do:** numeric normalisation (`$4M` ≈ `4 million` ≈ `4,000,000`; `₹ 30 Cr`), percentage and date
  normalisation (`2024/11/14` ≈ `2024-11-14`), so correct values aren't rejected.

### M8. Entity resolution for real-world names **[partly DONE locally]**

- **Problem:** 4 rows for Smart India Hackathon; "Django (web framework)" separate from "Django". Duplicates also
  inflate Chao2.
- **Done:** `normalize_name` drops parentheticals and taglines after `:` / `–` / `—` / ` - ` / ` | `.
- **Still to do:**
  1. Containment merge: if one key's tokens ⊂ another's, and the extra tokens are a year/edition word or the other key
     has no distinguishing tokens, merge (union-find already exists in `entity_resolution.py`).
  2. The planned **J4**: ask Jev "same entity?" for pairs with token-Jaccard 0.5–0.9.
  3. Re-key existing records in the DB when a merge happens (currently keys are fixed at upsert time).
- **Tests:** `test_entity_key_merges_taglines_and_parentheticals`.

### M9. "Latest/current" fields **[partly DONE]**

- **Problem:** Django `3.0.14` chosen from a download page listing every version; KBT then resolved conflicts *within
  one URL*, where source trust means nothing.
- **Done:** extraction prompt says "if a page lists many values over time, return only the current/most recent one
  unless history is asked for", and "use the short canonical name".
- **Still to do:**
  1. In `truth.py`, group claims by `(entity, field, source_url)` first; multiple values from the same URL are **one
     vote** (keep the one the extractor marked first / latest), never several.
  2. Let `FieldSpec` carry `temporal: "latest" | "any"`; for `latest`, prefer values with the newest date nearby and
     prefer primary sources (official site, PyPI, GitHub releases).

### M10. Encoding **[DONE locally]**

- **Problem:** `Ministry of Education�s`.
- **Fix (done):** `fetcher._decode()` honours the declared charset, then UTF-8, then cp1252; never returns U+FFFD from a
  mis-guessed charset. **Test:** `test_decode_cp1252_no_replacement_char`.

### M11. Replace or harden `ddgs` as the default engine

- **Problem:** `ddgs` scrapes DuckDuckGo; on a Render datacenter IP it rate-limits, returns nothing, or returns odd
  results. It is the only engine on the live site unless keys are set.
- **Fix:** make an official API the default and keep DDG as the fallback:
  - **Brave Search API** (already coded; just set `BRAVE_API_KEY` on Render; there is a free tier) — fastest win.
  - Add a `tavily` or `exa` engine adapter (same `_discover_*` shape). Exa's neural search fits "find entities like X"
    queries; both can return page text, which can skip a fetch.
  - Add retries with backoff and log "engine X returned 0" into the Source Ledger so empty searches are visible.

### M12. Deploy + regression benchmark

- Commit the above, deploy to Render, and re-run `eval/live_compare/run_scout.py` (4 cases). Pass bar:
  A ≥ 10 rows, B ≥ 3 rows, C = 3 correct rows (Django 6.1.x, Flask 3.1.x, FastAPI 0.14x), D ≥ 5 unique events with no `�`.
- Add the 4 prompts + expected facts as a CI smoke test (mock search with cached results so it is deterministic).
- **Status:** unit tests pass (`pytest app` → 42 passed). A local end-to-end re-run of the 4 cases was started
  (`eval/live_compare/local_rerun.py` → `local_after_fix.json`), but I haven't checked its results, so the fixes
  are **not yet proven end-to-end**.

---

## 3. OPTIONAL (high value)

### O1. Rerank search results before fetching
Score each `(query, title+snippet+url)` with a cheap model or Jev ("is this page likely to list <entity> matching
<filters>?") and fetch only the top-k. Today the 20-URL budget is spent in engine order, and J1 discards pages only
after they're downloaded. Expected effect: fewer wasted fetches, and more relevant pages within the same budget
(see the reranking-cost papers).

### O2. Two-phase "wide then deep" (map-reduce) pipeline
1. **Discover phase:** search for list/roundup/leaderboard/directory pages and extract **only entity names**
   (cheap, many pages). Chao2 is computed here, on names.
2. **Fill phase:** for each entity with null cells, run targeted queries (`"<entity> <field>"`, official site,
   Wikipedia, GitHub) and extract only the missing fields. Run in parallel, bounded by the Groq rate limit.

This matches how WideSearch-style systems scale, and it directly fixes the many null cells (e.g. city/sponsor in case D).

### O3. Facet decomposition in the planner
Have `parse_intent` emit facets (`years: [2024, 2025]`, `sub_segments: [...]`, `source_types: [news, directory,
official]`) and generate queries per facet. This makes captures more independent (better for Chao2) and raises recall.

### O4. Hybrid "answer engine" seed
Use an answer-style search (Tavily/Exa/Perplexity-type, or an LLM with web search) to get a quick candidate list and
authoritative URLs in seconds. Scout then verifies and fills them with its quote → Jev → KBT pipeline. This combines the
speed of a single web-search call with Scout's provenance.

### O5. Fast mode for small asks
If `target_count <= 10` or the prompt is a lookup ("latest version of…"), skip the Chao2 loop: 1–2 queries, top-5 pages,
extract, verify, done. Case C should take seconds, not a minute.

### O6. Domain priors and primary-source preference
Keep a per-topic domain-trust table across runs (on the roadmap) and seed it with obvious primary sources: official
sites, PyPI/npm, GitHub releases, arXiv, government portals, Crunchbase-style directories. Use it in KBT's prior (J5)
and in reranking (O1).

### O7. Coverage honesty in the UI
Show coverage as a range with the number of captures and a reliability badge ("low: 2 captures"). Hide PPI accuracy
until N human labels exist, and label any unlabelled estimate "judge-estimated, unverified".

### O8. Freshness
Pass date filters to engines (Brave `freshness`, Tavily `time_range`), store `retrieved_at` and the page's
published/updated date, and flag stale sources for "latest" fields.

### O9. Page cache + reproducible eval
Implement `eval/page_cache/` and commit `eval/gold/` so ablations (v0 vs. +M1–M10 vs. +O1/O2) run on identical pages.
Report Item-F1, Row-F1, cells-with-quote %, cost, and latency, like WideSearch.

### O10. Ops
- Render free tier: a keep-warm ping or a "waking up" state for the first request.
- Global cancel flag: `llm.request_cancel()` / `clear_cancel()` are process-wide, so starting a new run clears another
  run's cancel and cancelling one can stall another's LLM waits. Make cancellation per-run.
- Rate-limit visibility: surface Groq 429s and Jev timeouts in the run's stats.

---

## 4. Suggested order of work

| Step | Items | Effort | Expected impact |
|---|---|---|---|
| 1 | Commit + deploy M1–M7, M10 (done locally), set `BRAVE_API_KEY` (M11) | < 1 h | A/B stop returning 0 rows; no silent empties |
| 2 | M3 J1-gated re-plan trigger, M4 actual-engine logging, M12 benchmark | ½ day | Proven fix; measurable regression gate |
| 3 | M8 containment merge + re-keying, M9 same-URL KBT voting, M7 numeric normalisation | 1 day | Correct C, deduped D, honest coverage |
| 4 | O1 rerank, O5 fast mode | 1 day | Lower cost/latency, better use of the URL budget |
| 5 | O2 wide-then-deep, O3 facets, O4 hybrid seed | 2–4 days | Largest jump in rows and filled cells |
| 6 | O6–O10 | ongoing | Trust, freshness, ops |

---

## 5. Files changed so far (uncommitted)

| File | Change |
|---|---|
| `api/app/llm.py` | New intent query guidance; `relax_query`; relax-retry in `discover_urls_for_query` (`_discover_once`); `replan_queries`; extraction prompt: canonical names + latest-only |
| `api/app/pipeline.py` | `_available_engines()`; single source type; adaptive re-plan (`MAX_REPLANS`); stopping rule needs evidence; empty run → `failed` with message; whole-token `_value_in_quote` |
| `api/app/entity_resolution.py` | Strip parentheticals and taglines in `normalize_name` |
| `api/app/fetcher.py` | `_decode()` charset-safe decoding |
| `api/app/test_search_quality.py` | New tests for all of the above (42/42 pass) |
| `eval/live_compare/*` | Live-run script, raw Scout outputs, local re-run script |

---

## Sources

- Deep Research: A Survey of Autonomous Research Agents — https://arxiv.org/html/2508.12752v1
- From Web Search towards Agentic Deep Research — https://arxiv.org/html/2506.18959v1
- Rerank Before You Reason (reranking cost in deep search agents) — https://arxiv.org/html/2601.14224v2
- Training Documents Reranker with Search Rubrics for Deep Research Agent — https://arxiv.org/pdf/2608.03527
- SAGE: Benchmarking and Improving Retrieval for Deep Research Agents — https://arxiv.org/pdf/2602.05975
- AgentIR: Reasoning-Aware Retrieval for Deep Research Agents — https://arxiv.org/pdf/2603.04384
- dHiebl/Deep_Research (decomposition + hybrid retrieval + rerank) — https://github.com/dHiebl/Deep_Research
- WideSearch: Benchmarking Agentic Broad Info-Seeking — https://arxiv.org/pdf/2508.07999 · https://widesearch-seed.github.io/
- A-MapReduce: Executing Wide Search via Agentic MapReduce — https://arxiv.org/pdf/2602.01331
- WebSwarm: Recursive Multi-Agent Orchestration for Deep-and-Wide Web Search — https://arxiv.org/pdf/2607.08662
- Exa vs Tavily vs Serper vs Brave for AI agents — https://dev.to/supertrained/exa-vs-tavily-vs-serper-vs-brave-search-for-ai-agents-an-score-comparison-2l1g
- Agentic Search: Benchmark 8 Search APIs for Agents — https://aimultiple.com/agentic-search
