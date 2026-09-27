# Scout — AI Data Intelligence Platform

Code Cubicle 6.0 · Problem Statement 01.

Describe a dataset in plain English. Scout parses your intent into a schema, searches the
web for it, fetches and extracts records from permitted sources, and returns a clean,
deduplicated table where **every cell is traceable back to the exact quote and page it
came from** — plus a statistical estimate of how complete the result is.

## What makes this different

Most "AI web scraper" demos stop at extraction. Scout adds three things most don't:

1. **Coverage estimation, not just extraction.** A Chao2 estimator (`api/app/coverage.py`)
   treats each search as a capture occasion and estimates how many entities are still
   undiscovered, with a bootstrap confidence interval. Scout keeps searching until the
   lower-bound coverage estimate hits your target, then stops — instead of guessing when
   "enough" pages have been crawled.
2. **A decision layer separate from the extractor.** [Jev](https://typesafe.ai) (TypeSafe
   AI's System One model) judges every yes/no or pick-one question — "is this page even
   relevant?", "does this quote actually support this value?", "does this record pass the
   user's filters?" — and returns a calibrated probability. The Groq LLM only *extracts*
   values with a verbatim quote; Jev never writes text, so it can't hallucinate one. See
   [`ADDITION.md`](./ADDITION.md) for the full design and the concrete bugs it fixes (list-
   formatting false negatives, unchecked filters, one-CPU-forward-pass-per-claim latency).
3. **Knowledge-Based Trust for conflicting sources** (`api/app/truth.py`) — when two sources
   disagree on a value, an EM-style trust iteration (Dong et al. 2015) picks a winner instead
   of just keeping whichever was scraped first.

## How it works

1. **Intent parsing** (`api/app/llm.py::parse_intent`) — an LLM (`openai/gpt-oss-120b` via
   Groq) turns your prompt into a `DataSpec`: an entity name, columns with descriptions,
   filters, target coverage, and search queries.
2. **Capture loop** (`api/app/pipeline.py`) — each (query × search engine × source type)
   combination is a *capture occasion*, logged so Chao2 can estimate total coverage.
3. **Compliance gate** (`api/app/compliance.py`) — every URL is checked against robots.txt
   before it's ever fetched; every decision is logged to the Source Ledger.
4. **Fetch** (`api/app/fetcher.py`) — `httpx` + BeautifulSoup, with a JS-render fallback for
   thin pages.
5. **J1 — Jev page gate** (`api/app/jev.py`) — before spending a Groq call, Jev scores
   whether a fetched chunk actually names an entity worth extracting. Low-scoring chunks are
   skipped and logged (`skipped: jev page gate (0.07)`), cutting wasted extraction calls.
6. **Extraction** (`extract_records`) — the LLM reads a page chunk and returns records where
   every field value carries a verbatim quote as evidence. A value whose quote can't be
   found on the page (word-for-word, whitespace-normalized) is dropped rather than trusted.
7. **J3 — Jev filter check** — every extracted record is batch-checked against the prompt's
   filters (e.g. "seed funding", "2024", "Indian edtech"); records that clearly fail are
   dropped, borderline ones are flagged `filter_uncertain` instead of silently kept or lost.
8. **J2 — Jev claim support** — one batched Jev call scores every claim in a chunk for
   whether its quote actually supports the extracted value (falls back to a DeBERTa NLI
   cross-encoder, `api/app/verifier.py`, when `SCOUT_VERIFIER=deberta` or Jev is unavailable
   — a Jev outage never fails a run).
9. **Entity resolution** (`api/app/entity_resolution.py`) — name normalization (legal
   suffixes, casing, punctuation) so "Google LLC" and "Google" merge instead of duplicating.
10. **Dedupe & truth discovery** (`api/app/db.py`, `api/app/truth.py`) — records are merged
    by resolved entity key; a field confirmed by a second source is marked corroborated,
    and conflicting values are resolved via Knowledge-Based Trust at the end of a run.
11. **Coverage estimate** (`api/app/coverage.py`) — after each capture, Chao2 + bootstrap CI
    decide whether to keep searching or stop.
12. **Storage & export** — SQLite, with per-cell provenance (source URL + quote) kept
    alongside every value, plus a PPI-based accuracy audit (`api/app/ppi.py`) once you label
    a few cells.

## Running it

### 1. Set up

```bash
cd api
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` in the repo root and set `GROQ_API_KEY` (get one free at
[console.groq.com](https://console.groq.com/keys)). Optionally set `JEV_API_KEY` (from
[typesafe.ai](https://typesafe.ai)) to enable the Jev decision layer — without it, Scout
falls back to the pre-Jev behavior automatically (every page is extracted, filters aren't
checked, and DeBERTa scores claim support instead).

### 2a. Quick CLI demo (no server)

```bash
python scripts/demo.py "Find companies that sponsored student hackathons in India in 2025, with sponsorship tier and a contact URL"
```

Prints the plan, a live-ish console table, the source ledger (including any Jev page-gate
skips), and writes a CSV to `output/`.

### 2b. Full API + dashboard

```bash
cd api
uvicorn app.main:app --reload --port 8000
```

Then open `http://127.0.0.1:8000/` in a browser — the dashboard is served by the API itself
(`web/index.html` opened directly also works). Start a run, watch live status over SSE,
click any cell to see its source quote in the provenance drawer, label cells for the
accuracy audit, export to CSV.

### Tests

```bash
cd api
pytest app/test_jev.py -v
```

## Project layout

```
api/app/
  config.py            # env, model choices, Jev thresholds
  models.py            # DataSpec / FieldSpec pydantic models
  llm.py                # Groq intent parsing + extraction, DuckDuckGo/Brave/SearXNG discovery
  compliance.py         # robots.txt gate — the Compliance Gate
  fetcher.py             # httpx + BeautifulSoup page fetching, JS-render fallback
  jev.py                 # Jev (TypeSafe AI System One) client — J1 page gate, J2 claim
                          #   support, J3 filter check
  verifier.py            # DeBERTa NLI fallback for claim support when Jev is off
  entity_resolution.py   # name normalization + union-find for entity merges
  truth.py                # Knowledge-Based Trust conflict resolution
  coverage.py             # Chao2 estimator + bootstrap CI for "how complete is this?"
  ppi.py                  # prediction-powered inference accuracy audit
  pipeline.py             # orchestrates the capture loop above, writes to SQLite
  db.py                   # sqlite storage: runs, records (with provenance), source ledger, claims
  main.py                 # FastAPI: POST /api/runs, GET .../stream (SSE), GET .../export.csv
  test_jev.py             # unit tests for the Jev client against a fake transport
scripts/demo.py           # terminal-only end-to-end run, no server needed
web/index.html            # single-file dashboard: prompt bar, live status, table, provenance drawer
eval/                      # benchmark tasks + scoring harness
ADDITION.md                # design doc for the Jev decision layer — why it's a fit, where
                            #   it plugs into the pipeline, and what it fixed in practice
```

## Next up

- J4/J5 from `ADDITION.md`: Jev-backed entity resolution for grey-zone name pairs, and a
  Jev `choice` prior feeding Knowledge-Based Trust instead of a flat 0.5 prior
- Persistent domain trust across runs on the same topic
- Fine-tuned verifier checkpoint once enough human-labelled cells exist
