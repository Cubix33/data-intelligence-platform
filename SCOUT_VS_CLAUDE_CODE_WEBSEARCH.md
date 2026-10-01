# Scout vs. Claude Code Web Search — Comparison & Improvement Suggestions

> Date: 2026-10-01 · Target: https://scout-platform.onrender.com (repo `Cubix33/data-intelligence-platform`)
> Method: static read of the Scout source (`api/app/*.py`, README, `eval/WEBATTACK_BENCHMARK.md`) + one live
> Claude Code `WebSearch` call on the same prompt Scout was benchmarked on.
> **Not done:** I did not run a fresh Scout job on the live site; Scout figures below come from the repo and its
> own recorded benchmark (run `e40a1f3bff95`, 2026-09-29). Claude Code figures come from one search call, so
> treat them as illustrative, not statistical.

---

## 0. UPDATE — fresh live runs (2026-10-01) — read this first

I ran **4 fresh jobs on https://scout-platform.onrender.com** (via `POST /api/runs`, target coverage 0.8, sequential)
and ran the equivalent **Claude Code `WebSearch`** query for each. Raw Scout output is saved in
`eval/live_compare/scout_*.json` (+ `run_scout.py`, `scout_log.txt`). One trial per case — indicative, not statistical.

| Case | Prompt (short) | Scout live result | Claude Code WebSearch result |
|---|---|---|---|
| **A** | Open-source VLMs 2024–25 (name, org, score) | **0 records** · 142 s · status `done`, no error. 36 URLs found, 26 gated by Jev J1 (151 of 153 chunks), 10 empty. Pages were off-topic (eye-health sites: Cleveland Clinic, CDC, Merriam-Webster "vision") | ~5 models in prose (R1V2 73.6 MMMU, Qwen2.5-VL, Pixtral, SmolVLM2, Molmo); only 1 concrete score; 9 source links |
| **B** | Indian edtech seed rounds 2024 (company, amount, investor) | **0 records** · 94 s · `done`, no error. 42 URLs, **all 42 gated at 0.01**: seed.com, a seed-probiotic review, Johnny's Seeds, Wikipedia "Seed" | Sparkl Edventure $4M (Rainmatter + Deepinder Goyal), Invest4Edu $3M (family offices), Ingenium (Lead Angels) + context stats; 9 links |
| **C** | Latest Django/Flask/FastAPI versions + dates | **2 rows, 1 wrong.** Django = `3.0.14` (wrong; its own conflict list contained `6.1.1`, and the quote "Documentation version: 6.1" doesn't contain 3.0.14), release_date `2026-08-05` has no quote. 2nd row is a duplicate "Django (web framework)" with nulls. **Flask and FastAPI missing.** Run stopped after 2 captures with "coverage 100% (CI 0.8–1.0)" · 63 s | Django 6.1.1 (2 Sep 2026), Flask 3.1.3 (19 Feb 2026), FastAPI 0.141.1 (29 Jul 2026) — all three, no verification |
| **D** | Student hackathons in India 2025 (name, city, sponsor) | **7 rows**, coverage claimed **91.3%** · 173 s. But 4 of 7 rows are the same Smart India Hackathon (not merged); mojibake (`Ministry of Education�s`); "sponsor" for the KLH row is a venue address; several cells null | 6 events with dates/cities (Innerve X, HackWave, GenAI Hackathon Chandigarh, India NSM, SIH, TechJam); HackWave sponsors (Devfolio, GitHub, Polygon Labs…) — mixed 2025/2026 |

### What this changes in my earlier conclusion
- The README/benchmark story ("50 models, 270 cells") **did not reproduce** on the same VLM prompt two days later:
  0 records. On these four tasks Scout produced useful output on **1 of 4** (D, and only partly), and a *wrong*
  answer on C. For quick factual / small-list asks, **Claude Code's WebSearch was faster (seconds vs 1–3 min) and more
  correct** in all four cases.
- Scout's real strengths (per-cell quote provenance, ledger, compliance) are still real — but only matter if the
  discovery stage returns relevant pages. In 2 of 4 runs it did not.

### Root causes observed (evidence-backed)
1. **Search returns junk or nothing for heavily-quoted / `site:` queries.** The planner's queries are good in
   spirit but over-constrained: `site:inc42.com "seed" "edtech startup" "India" 2024`, `"2024" "seed" "edtech" "India" "lead investor"`,
   `site:github.com "vision language model" 2024 "open source" benchmark`. I ran them locally through the same
   `ddgs` library: the 2nd and 3rd return **"No results found"** and the 1st returns a single hit. When DDG yields
   nothing, the live run's result lists were dominated by single-keyword matches ("seed", "vision") — consistent with a
   fallback or relaxed query. (I cannot see the Render server logs, so *which* path produced the junk is inferred.)
2. **Silent failure.** Runs with 0 records end `status=done` with `error=null`; no "search returned irrelevant pages"
   message, no retry with relaxed queries, and the capture loop still burned all 12 captures on the same bad pool.
3. **Jev J1 gate works as designed but hides the upstream problem** (gating 151/153 and 127/127 chunks is correct —
   the pages *were* irrelevant); the pipeline treated "everything gated" as normal rather than a signal to re-plan.
4. **Truth-discovery picked the wrong value (case C).** KBT chose `3.0.14` from a list of versions on one download page
   (all conflicts cite the *same URL*), so source-trust is meaningless within a single page. Rows should be extracted as
   "the *latest* version", not every version listed.
5. **Entity resolution misses semantic dupes (case D):** "Smart India Hackathon 2025", "Smart India Hackathon (SIH) 2025",
   "Smart India Hackathon", "…Where Bold Student Ideas Become National Solutions" stayed 4 rows; this also inflates
   Chao2 (s_hat 7.7 / coverage 91%) — the estimate is only as good as dedup.
6. **Coverage claim can be vacuous (case C):** "100%, CI 0.8–1.0" from 2 captures with 2 rows (one a duplicate).
7. **Encoding bug:** `�` in extracted text (D) — the fetcher/charset handling drops the apostrophe.

### Where Claude Code's WebSearch was weaker in these runs
- No per-claim evidence; some figures unsourced (e.g. MMBench ">80%" in case A).
- Snippet-level only: B and D gave 3–6 items, not an exhaustive table; D mixed 2025 and 2026 events; C's versions are
  unverified against the primary page.
- Not exportable/structured and no completeness estimate.

### Priority fixes triggered by these runs (added to §6)
- **P0:** Fail loudly when a run yields 0 records or >90% of chunks are gated: re-plan with relaxed/unquoted queries
  (drop `site:` and quotes, use natural-language queries), then show "no relevant pages found" to the user.
- **P0:** Relax query generation: at most one operator per query, no stacked quoted phrases; test every planned query
  against the engine and replace empty ones before using them as capture occasions.
- **P0:** For "latest/current" fields, add a rule + schema (`is_latest: bool`) so only the newest value is kept; don't run
  KBT across values from the same URL.
- **P1:** Merge near-duplicate entity names (the planned J4 Jev entity-resolution) before computing Chao2; require ≥ N
  independent captures before reporting coverage.
- **P1:** Fix response decoding (use `resp.encoding`/`charset-normalizer`).
- **P1:** Add a **hybrid mode**: use a general web-search/answer path (like Claude Code's) to seed entities and
  authoritative URLs, then let Scout's fetch → quote → verify pipeline fill and prove the cells.
- **P2:** Re-run this 4-case comparison weekly as a regression benchmark (script is in `eval/live_compare/run_scout.py`).

---

## 1. What each system actually is

| | **Scout** | **Claude Code `WebSearch`** (this session) |
|---|---|---|
| Purpose | Turn a prompt into a **sourced table** (entities × fields) with coverage + accuracy estimates | General-purpose lookup tool inside a coding agent; the model reads results and writes an answer |
| Search backends | DuckDuckGo (`ddgs`), Brave API, SearXNG (`llm.py:_discover_*`); Brave/SearXNG silently fall back to DDG if not configured | One managed backend, US-only, returns title+URL blocks; supports `allowed_domains` / `blocked_domains` |
| Query planning | LLM → `DataSpec` with ~5 queries; planner iterates query × engine × {general, news} (`pipeline.py:_next_capture_params`) | The calling model writes each query by hand, one call at a time |
| Page reading | **Fetches every page** (httpx + BeautifulSoup, Playwright fallback), chunks to 9k chars | Search call returns only snippets/titles; full page needs a separate fetch step |
| Extraction | Per-chunk LLM extraction with mandatory **verbatim quote per cell**; values not found in the page are dropped | Free-text synthesis by the model; no per-claim quote check |
| Verification | Jev J1/J2/J3 (relevance gate, claim support, filter check), DeBERTa fallback, Knowledge-Based Trust for conflicts | None built in; relies on the model's judgement and citing sources |
| Completeness | Chao2 estimator + bootstrap CI, loops until coverage lower bound ≥ target (default 80%) | None; stops when the model decides it has enough |
| Accuracy reporting | PPI audit from human labels | None |
| Safety / compliance | robots.txt, SSRF guard (public-IP pinning), Source Ledger of allow/block decisions | Tool-level permissions; no robots.txt handling needed because it only returns search results |
| Output | SQLite + live dashboard (SSE) + CSV, per-cell provenance drawer | Markdown text with a "Sources:" list |
| Cost / latency | Many Groq + Jev calls per run, minutes per job; free-tier Render | One call, seconds |
| Interactivity | Fire-and-forget batch job | Conversational; can follow up, refine, reason across results |

## 2. Side-by-side on the same prompt

Prompt (from Scout's own benchmark): *"Top open-source vision language models released in 2024 and 2025 with
model name, organization, and benchmark score."*

| Dimension | Scout (recorded run) | Claude Code WebSearch (today) |
|---|---|---|
| Entities returned | **50** models (structured rows) | **~5** named in prose (R1V2, Qwen2.5-VL, Pixtral, SmolVLM2, Molmo) |
| Fields per entity | name, org, year, benchmark, score, license, repo URL | Mostly name + a vague descriptor; only 1 concrete MMMU number (R1V2 = 73.6) |
| Per-cell evidence | Quote + URL for every cell (270 cells) | None per claim; 9 source links listed at the bottom, not tied to claims |
| Pages actually read | 30 fetched, 19 gated out, 12 robots-blocked | 0 read in the search call (snippets only) |
| Completeness signal | Coverage % with CI | None |
| Unsupported claims | Mechanically filtered (quote must appear) — but see §3 | Present: "MMBench >80%, MM-Vet >75%" and "reduced inference costs by up to 60%" appear with no linked source |
| Recency | Depends on crawl; benchmark was 2 days ago | Results included 2026 items (e.g. "Best … of 2026", 2026 arXiv IDs) — fresher and broader than the prompt asked |
| Time | Minutes | Seconds |

**Honest takeaway (superseded in part by §0 — the live re-runs did not reproduce this recorded benchmark):** for *bulk, auditable table-building* when discovery works, Scout is ahead of a single WebSearch call. For
*quick answers, exploration, and judgement across sources*, WebSearch + a capable model is faster and more flexible.
They are complementary, not direct substitutes. A fair like-for-like would be Claude Code running a *loop* of
searches + page fetches — which would close much of the entity-count gap but still lack Scout's quote-gating,
coverage estimate and ledger.

## 3. Weaknesses I found in Scout's own evidence

These matter because Scout's pitch is "every cell is trustworthy".

1. **Quote doesn't contain the value, yet scored 0.89.** In the benchmark table, `InternVL2-40B / benchmark_score = 61.2`
   has quote *"InternVL2-40B achieved SOTA performance on Video-MME"* — the number 61.2 isn't in the quote, and J2 gave it
   0.89. Similarly `release_year = 2024` from *"2024/11/14"* (0.65) is fine, but the 61.2 case shows J2 (or the
   `_value_in_quote` check in `pipeline.py:74`) is letting through quotes that don't support the number. This is the
   single most important thing to fix: it undermines the headline claim.
2. **PPI headline is not an accuracy measurement.** The benchmark reports "53.0% (95% CI 51–55%)" from *unlabelled* data.
   A 2-point CI around the mean of the judge's own scores says the judge's average confidence, not true accuracy; the
   narrow interval is misleading. Without human labels it should be shown as "judge-estimated, unverified", not as a
   95% CI on accuracy. Also, 53% implies nearly half the cells are doubtful — that deserves a visible warning.
3. **Claimed benchmark vs. repo drift.** README says extraction model is `llama-3.1-8b-instant`; the benchmark doc says
   `openai/gpt-oss-120b`. Pick one source of truth.
4. **Search is the thinnest layer.** Only 8 results/query, 6 URLs/capture, 20 URLs/run (`config.py`), and engines silently
   degrade to DDG when Brave/SearXNG keys are absent — so "3 engines" may in practice be 1 engine queried three times,
   which also **inflates Chao2 capture count with non-independent occasions** and biases the coverage estimate upward.
5. **Capture occasions aren't independent.** Chao2 assumes independent sampling occasions; query × engine × "news/general"
   combinations of the same search topic are heavily correlated. Coverage % is likely optimistic.
6. **Source type "news" is cosmetic.** `source_type` is logged but I found no code path in `_discover_*` that changes the
   search by it (no news endpoint / date filter), so the 2× multiplier mostly re-runs identical searches.
7. **Eval gaps.** `eval/README.md` points to `eval/gold/` and `page_cache/`, but `gold/` isn't in the tracked files, so
   the F1/calibration numbers described can't be reproduced from the repo.

## 4. Where Claude Code's search is better (things to borrow)

- **Query adaptivity:** the model reads results and *reformulates* based on what it learned (e.g. finds "MMMU-Pro" and
  searches that). Scout's queries are fixed up-front (5 queries) and only recombined across engines.
- **Domain steering:** `allowed_domains` / `blocked_domains` — cheap way to say "only arxiv.org, huggingface.co, github.com".
- **Cross-source reasoning:** the model can notice that two leaderboards use different benchmark variants.
- **Low latency for easy asks.** Scout always spins up a full pipeline even for a 5-row question.

## 5. Where Scout is better (keep / market these)

- Verbatim-quote enforcement + per-cell provenance drawer.
- Quantified completeness (coverage CI) and stop criterion.
- Compliance: robots.txt + SSRF hardening + auditable Source Ledger.
- Structured, exportable, deduplicated output with entity resolution and conflict resolution.
- Cost control via the J1 page gate.

## 6. Suggested improvements to Scout (prioritised)

### P0 — correctness / credibility
1. **Make "value must appear in quote" strict and numeric-aware.** Normalise numbers/units and require the value token
   (61.2, 73.6%, "$4M") to occur in the quote; otherwise drop or mark `unsupported`. Add a regression test using the
   InternVL2-40B example.
2. **Don't present unlabelled PPI as accuracy.** Show "Judge-estimated 53% (unverified)" until ≥ N human labels exist;
   compute the CI from labelled rows only; warn when mean support < ~0.7.
3. **Fix the Chao2 independence problem.** Use genuinely different captures (different queries, different engines that
   actually ran, different time windows). Record which engine *really* served each capture (not the requested one) and
   exclude fallbacks from the occasion count. Report a coverage *range* with a caveat.
4. **Reconcile docs vs. config** (extraction model, defaults) and commit `eval/gold/` so the benchmark is reproducible.

### P1 — search quality (the part directly comparable to Claude Code's web search)
5. **Adaptive query planning.** After each capture, give the LLM the entities found so far + gaps (empty fields, missing
   years) and have it propose the next 2–3 queries, instead of a static list. This is the biggest gap vs. an agentic search.
6. **Site-targeted discovery.** Let the DataSpec carry `preferred_domains` (arxiv, huggingface, github, paperswithcode,
   Wikipedia lists) and use `site:` or domain filters; fetch known "list"/leaderboard pages directly.
7. **Real source-type behaviour.** Implement `news` via Brave news endpoint / DDG news with date filters, or remove it.
8. **Follow links from list pages** (leaderboards, awesome-lists) to entity pages to fill missing fields — "enrich" pass
   for rows with null cells, rather than only widening search.
9. **Raise or auto-scale limits** (`MAX_URLS_PER_RUN=20`, 6 per capture) based on target coverage and remaining budget.
10. **Rank candidates before fetching** (title/snippet relevance with a cheap model or Jev) so the 20-URL budget goes to
    the best pages; currently the order is search-engine order.

### P2 — product / UX
11. **Fast mode** for small asks: single search → fetch top pages → extract, skipping the Chao2 loop; show the row count
    live and an "ask a follow-up to refine" box (conversational refinement like Claude Code).
12. **Show engine health** in the UI ("Brave: not configured → used DDG") so users know what actually ran.
13. **Freshness controls:** date range filter and a per-row `retrieved_at`; flag stale sources.
14. **Source quality prior:** persistent domain trust (already on roadmap) + primary-source preference (paper > blog > aggregator).
15. **Cell-level "needs review" queue** sorted by lowest J2 score, so human labelling effort feeds PPI where it matters most.
16. **Caching of pages across runs** (the `page_cache/` idea) for repeatable evals and lower cost.

### P3 — hardening / ops
17. Retry/backoff and per-engine rate-limit handling for `ddgs` (it is an unofficial scraper and breaks often); surface
    failures instead of silently continuing (`pipeline.py` `continue` on discovery error).
18. Render free tier cold starts: add a keep-warm ping or a loading state for the first request.
19. Add the tests that matter for the above (quote/numeric check, engine-fallback accounting, adaptive planner).

## 7. A fairer way to benchmark next

Run all three on the same 10 WideSearch-style tasks and compare **Item-F1, Row-F1, cost, time, and % of cells with a
supporting quote**:
1. Scout (current),
2. Scout + adaptive planner (P1-5/6),
3. Claude Code with a search+fetch loop and a "return JSON with quotes" instruction.

Hypothesis: (3) is competitive on small tasks (<15 rows) and faster; Scout wins on large (>30 rows) tasks and on
auditability; (2) should beat both on recall at similar cost.

## 8. Limits of this comparison

- One search call, one prompt; no repeated trials; Claude Code's WebSearch returns snippets and the model's summary, not
  full pages, so it is under-powered compared with a search+fetch loop.
- Scout numbers are from its own benchmark file (self-reported) and I did not re-run or independently verify them.
- I did not test the live site's current availability or behaviour.
