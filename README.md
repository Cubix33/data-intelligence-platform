# Scout — AI Data Intelligence Platform

> **Code Cubicle 6.0 · Problem Statement 01**

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Cubix33/data-intelligence-platform)

[LIVE LINK](https://scout-platform.onrender.com/)

[DEMO VIDEO](https://www.loom.com/share/96879afe174c45ee94682f75a4933638)

<img width="3200" height="1800" alt="scout-vs-search" src="https://github.com/user-attachments/assets/66335e57-39c8-47b2-90be-caaac68bf99c" />

Scout turns a plain-English data request into a clean, sourced spreadsheet. Describe the dataset you need — companies, jobs, products, research papers, anything — and Scout plans the search, crawls permitted pages, extracts every field with a verbatim quote as proof, deduplicates the records, and hands you a table where **every single cell traces back to the exact sentence it came from**.

It also tells you how complete the result is, using statistics borrowed from ecology, and lets you audit its accuracy without labelling every row.

---

## Why Scout is different from a normal AI scraper

Most AI scraping demos answer "what did you find?" Scout answers two harder questions:

| Question                           | How Scout answers it                                                                                                                                                                                                                                                                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Can I trust this cell?**   | Every field value must come with a verbatim quote found on the source page. Values without a matching quote are dropped. A separate judge model (Jev) scores whether the quote really supports the value — it never generates text, so it can't hallucinate.                       |
| **Have I found everything?** | A Chao2 species-richness estimator — the same method ecologists use to estimate how many species exist in a forest from repeat sightings — estimates the total population of matching entities and keeps searching until its lower-bound confidence interval reaches your target. |

Three techniques make this concrete:

### 1 · Coverage estimation (Chao2)

Each search query is treated as a **capture occasion**. When the same entity turns up in multiple captures, the overlap lets us estimate how many entities we haven't found yet. Scout keeps running until the lower bound of that estimate hits the coverage target you set (default 80%). You see this live in the dashboard as a progress bar with a confidence interval.

File: `api/app/coverage.py`

### 2 · Jev decision layer

Scout uses two AI systems for two different jobs, and they never swap roles:

- **Groq LLM** — reads pages and extracts values, each paired with a verbatim quote.
- **Jev** (TypeSafe AI System One) — judges yes/no questions and returns a calibrated probability. It never writes text.

The separation matters: the model that could hallucinate a value is always checked by one that can't. Jev runs three checks (J1–J3) during each pipeline run:

| Check                         | Where             | What it does                                                                                                                                                                                                                                                                    |
| ----------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **J1 — Page gate**     | Before extraction | Scores whether the page chunk actually names any relevant entity. Chunks scoring below 0.35 are skipped, saving Groq API calls.                                                                                                                                                 |
| **J2 — Claim support** | After extraction  | Scores a whole batch of claims in one call: does this quote actually support this value? Replaces the old per-claim DeBERTa call, which was ~10× slower and got list formatting wrong (scoring`"Gokul Rajaram\nAmit Singhal"` vs `"Gokul Rajaram, Amit Singhal"` as 0.02). |
| **J3 — Filter check**  | After extraction  | Checks every record against your prompt's filters (e.g. "seed funding", "2024", "Indian edtech"). Records that clearly fail are dropped; borderline ones are kept with a`filter_uncertain` badge.                                                                             |

If Jev is unavailable, J1 lets every page through, J2 falls back to DeBERTa, and J3 is skipped. **A run never fails because Jev is down.**

File: `api/app/jev.py` · Full design rationale: [`ADDITION.md`](./ADDITION.md)

### 3 · Knowledge-Based Trust

When two sources give different values for the same cell, Scout doesn't just keep whichever it scraped first. An EM-style trust iteration (based on Dong et al. 2015) estimates which sources are generally reliable and picks the winner from conflicting claims accordingly. Both values are shown in the provenance drawer so you can see the disagreement.

File: `api/app/truth.py`

---

## Pipeline walkthrough

Here's what happens, in order, when you run a prompt:

```
Your prompt
   │
   ▼
[1] Intent parsing (Groq LLM)
    Turns plain English into a DataSpec:
    entity type · field names & descriptions · filters · search queries · coverage target
   │
   ▼
[2] Capture loop (pipeline.py)
    Runs (query × search engine × source type) combinations.
    Each combination is one "capture occasion" for the Chao2 estimator.
    Search engines: DuckDuckGo · Brave · SearXNG
   │
   ▼
[3] Compliance gate (compliance.py)
    Checks robots.txt and blocks destinations that resolve to loopback, private,
    link-local, or other non-public IPs. Redirects, pagination, robots.txt, and
    browser-rendered page requests are checked before each connection and pinned.
    Every decision — allowed or blocked — is logged to the Source Ledger.
   │
   ▼
[4] Fetch (fetcher.py)
    httpx + BeautifulSoup. Falls back to Playwright for JavaScript-heavy pages.
   │
   ▼
[5] J1 — Jev page gate (jev.py)
    "Does this page chunk name a relevant entity?"
    noul < 0.35 → skip and log. Saves Groq quota on irrelevant pages.
   │
   ▼
[6] Extraction (llm.py)
    Groq LLM extracts records. Every field value must include a verbatim quote.
    Quote not found word-for-word on the page → value is dropped.
   │
   ▼
[7] J3 — Jev filter check (jev.py)
    Batch-checks all extracted records against your prompt's filters.
    Clearly failing records are dropped; uncertain ones are flagged.
   │
   ▼
[8] J2 — Jev claim support (jev.py)
    Batch-scores all claims: does the quote actually support the value?
    Fallback: DeBERTa NLI cross-encoder (verifier.py).
   │
   ▼
[9] Entity resolution (entity_resolution.py)
    Normalises names: strips legal suffixes, fixes casing and punctuation.
    "Google LLC" and "google inc." become the same row.
   │
   ▼
[10] Dedupe + truth discovery (db.py, truth.py)
     Merges records by resolved entity key.
     A field confirmed by a second source gets a ✓✓ corroborated badge.
     Conflicting values are resolved via Knowledge-Based Trust.
   │
   ▼
[11] Coverage check (coverage.py)
     Chao2 + bootstrap CI estimates total population and coverage.
     Coverage lower bound < target → loop back to [2] with new queries.
     Coverage lower bound ≥ target → stop and finalize.
   │
   ▼
[12] Results
     SQLite storage with per-cell provenance (URL + quote).
     Live table in the dashboard. CSV export. PPI accuracy audit.
```

---

## Getting started

### Prerequisites

- Python 3.11+
- A free [Groq API key](https://console.groq.com/keys) — required
- A [TypeSafe AI / Jev API key](https://typesafe.ai) — optional but recommended

### 1. Install

```bash
git clone <repo>
cd data-intelligence-platform/api

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` in the repo root and fill it in:

```bash
# .env

# Required
GROQ_API_KEY=gsk_...

# Optional — enables the Jev decision layer (J1/J2/J3 checks)
JEV_ENABLED=true
JEV_API_KEY=your_jev_key_here

# Optional overrides (defaults shown)
# SCOUT_MODEL_INTENT=llama-3.3-70b-versatile
# SCOUT_MODEL_EXTRACT=llama-3.1-8b-instant
# SCOUT_SEARCH_RESULTS=8
# SCOUT_MAX_URLS=20
# SCOUT_MAX_PAGE_CHARS=9000
# SCOUT_DB_PATH=./scout.db
```

Without `JEV_API_KEY`, Scout falls back to DeBERTa for claim scoring and skips the page gate and filter checks. It still works; it's just slower and less accurate.

### 3a. Quick demo — terminal only, no server needed

```bash
python scripts/demo.py "Find companies that sponsored student hackathons in India in 2025, with sponsorship tier and a contact URL"
```

This prints:

- The parsed `DataSpec` (entity, fields, filters, queries)
- A live table that fills in as records are found
- The Source Ledger (including any Jev page-gate skips and robots.txt blocks)
- A summary of coverage and stats

Output CSV is written to `output/`.

### 3b. Full dashboard

```bash
cd api
uvicorn app.main:app --reload --port 8000
```

Open `http://127.0.0.1:8000/` in a browser. The API serves the dashboard from `web/index.html`.

**Dashboard features:**

- Prompt bar with coverage-target slider (50%–95%)
- Example prompts to get started quickly
- Live status updates and coverage bar over SSE (no polling)
- Results table that streams in as records arrive
- **Cell provenance drawer** — click any cell to see the exact quote, source URL, corroboration status, and any conflicting values with their trust scores
- Source Ledger showing every URL Scout touched, whether it was allowed or blocked, and why
- Accuracy audit — label a handful of cells and get a PPI accuracy estimate with a 95% confidence interval
- CSV export

### 4. Tests

```bash
cd api
pytest app -v
```

---

## Accuracy audit (PPI)

After a run, click **Load 10 cells** in the accuracy audit panel. Mark each cell as correct or wrong. Scout uses **Prediction-Powered Inference** (PPI) to extrapolate from your handful of labels to an accuracy estimate for the whole table:

- The Jev J2 support score (`f(x)`) acts as the predictor
- Your human labels calibrate it
- The result is something like: *"91% accurate, 95% CI: 85%–96%"*

You don't need to label every cell. A few dozen labels are enough for a meaningful interval, and the interval narrows as you add more.

File: `api/app/ppi.py`

---

## Project layout

```
data-intelligence-platform/
│
├── api/
│   ├── app/
│   │   ├── config.py              # env vars, model names, Jev thresholds
│   │   ├── models.py              # DataSpec / FieldSpec Pydantic models
│   │   ├── main.py                # FastAPI app: POST /api/runs, SSE stream, CSV export
│   │   │
│   │   ├── llm.py                 # Groq: intent parsing + record extraction
│   │   ├── compliance.py          # robots.txt and safe-destination checks
│   │   ├── fetcher.py             # guarded HTTP fetching, parsing, Playwright fallback
│   │   ├── ssrf.py                # public-IP validation and pinned outbound requests
│   │   │
│   │   ├── jev.py                 # Jev client: J1 page gate, J2 claim support, J3 filter check
│   │   ├── verifier.py            # DeBERTa NLI fallback for claim support (no Jev key)
│   │   │
│   │   ├── pipeline.py            # Orchestrates the full capture loop
│   │   ├── db.py                  # SQLite: runs, records, provenance, source ledger, claims
│   │   │
│   │   ├── entity_resolution.py   # Name normalisation + union-find for entity merging
│   │   ├── truth.py               # Knowledge-Based Trust conflict resolution (Dong et al. 2015)
│   │   ├── coverage.py            # Chao2 estimator + bootstrap CI
│   │   ├── ppi.py                 # Prediction-Powered Inference accuracy audit
│   │   │
│   │   ├── test_jev.py            # Unit tests for the Jev client (uses a fake transport)
│   │   └── test_ssrf.py           # Outbound destination and redirect safety tests
│   │
│   └── requirements.txt
│
├── scripts/
│   └── demo.py                    # Terminal-only end-to-end run, no server
│
├── web/
│   └── index.html                 # Single-file dashboard (served by the API)
│
├── eval/
│   ├── tasks.json                 # Benchmark tasks
│   ├── run_bench.py               # Run all tasks and collect results
│   └── score.py                   # Score results against gold sets
│
├── ADDITION.md                    # Full design doc for the Jev decision layer
├── .env.example                   # Config template
└── README.md
```

---

## Environment variables reference

| Variable                 | Required | Default                     | Description                                               |
| ------------------------ | -------- | --------------------------- | --------------------------------------------------------- |
| `GROQ_API_KEY`         | ✅       | —                          | Groq API key for LLM calls                                |
| `JEV_API_KEY`          | —       | —                          | TypeSafe AI key; enables J1/J2/J3                         |
| `JEV_ENABLED`          | —       | `true`                    | Set to`false` to force DeBERTa fallback even with a key |
| `JEV_MODEL`            | —       | `jev-latest`              | Jev model name                                            |
| `JEV_TIMEOUT_MS`       | —       | `5000`                    | Per-call timeout for Jev requests                         |
| `SCOUT_MODEL_INTENT`   | —       | `llama-3.3-70b-versatile` | Groq model for intent parsing                             |
| `SCOUT_MODEL_EXTRACT`  | —       | `llama-3.1-8b-instant`    | Groq model for record extraction                          |
| `SCOUT_SEARCH_RESULTS` | —       | `8`                       | Search results per query                                  |
| `SCOUT_MAX_URLS`       | —       | `20`                      | Max URLs to fetch per run                                 |
| `SCOUT_MAX_PAGE_CHARS` | —       | `9000`                    | Max characters per page chunk                             |
| `SCOUT_DB_PATH`        | —       | `./scout.db`              | SQLite database location                                  |
| `SCOUT_VERIFIER`       | —       | `jev`                     | `jev` or `deberta` — which claim verifier to use     |

---

## API reference

The FastAPI server exposes these endpoints:

| Method     | Path                          | Description                                                                                                         |
| ---------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `POST`   | `/api/runs`                 | Start a new run. Body:`{"prompt": "...", "target_coverage": 0.8}`                                                 |
| `GET`    | `/api/runs/{id}`            | Get the current state of a run (records, sources, stats)                                                            |
| `DELETE` | `/api/runs/{id}`            | Cancel a running run                                                                                                |
| `GET`    | `/api/runs/{id}/stream`     | SSE stream of live events:`status_change`, `capture_start`, `coverage`, `record_found`, `done`, `error` |
| `GET`    | `/api/runs/{id}/export.csv` | Download results as CSV                                                                                             |
| `GET`    | `/api/runs/{id}/claims`     | List claims for the accuracy audit                                                                                  |
| `POST`   | `/api/labels`               | Submit a human label for a claim                                                                                    |
| `GET`    | `/api/runs/{id}/accuracy`   | Get the current PPI accuracy estimate                                                                               |
| `GET`    | `/`                         | Serves the dashboard (`web/index.html`)                                                                           |

---

## Roadmap

These are described in detail in [`ADDITION.md`](./ADDITION.md):

- **J4 — Jev entity resolution** — for grey-zone name pairs with Jaccard similarity 0.5–0.9 (e.g. "Physics Wallah" vs "PhysicsWallah Pvt Ltd"), ask Jev whether they're the same entity before merging. Prevents both false merges and false duplicates, which directly improves Chao2 coverage accuracy.
- **J5 — Jev prior for KBT** — seed Knowledge-Based Trust's initial value probabilities with Jev's `choice` output rather than a flat 0.5 prior. Makes conflict resolution content-aware (e.g. catches unit/currency mismatches like "$4M" vs "Rs 4 crore").
- **Persistent domain trust** — carry source reliability scores across runs on the same topic rather than re-learning them each time.
- **Fine-tuned verifier** — once enough human-labelled cells exist, fine-tune a lighter-weight verifier checkpoint on the task distribution instead of relying on the general-purpose DeBERTa model.

---

## Technical notes

**Why Chao2 and not just "stop at N pages"?**

Chao2 uses the number of entities seen exactly once (singletons) vs. exactly twice (doubletons) across capture occasions to estimate how many remain unseen. If most entities are singletons, you're still early in the discovery curve. If the singleton count is low relative to the total, you're near saturation. This gives a principled stopping criterion that adapts to the actual distribution of entities, rather than a fixed crawl budget.

**Why a separate judge model instead of asking the LLM to verify itself?**

Asking the same model that extracted a value to also verify it is circular — it tends to confirm what it just said. Jev is a separate system trained specifically to output calibrated probabilities for yes/no questions (using RLCD). Its probability of 0.91 actually means "this is true about 91% of the time when I say 0.91", which the LLM's softmax outputs do not.

**Why verbatim quotes?**

Requiring a verbatim quote is the simplest possible hallucination check that actually works at scale. If the value cannot be located word-for-word on the page (after whitespace normalisation), it was inferred rather than read, and it's dropped. This catches most fabrications without requiring a second LLM call for every field.

---

## Acknowledgements

- [Groq](https://groq.com) — LLM inference
- [TypeSafe AI](https://typesafe.ai) — Jev decision model
- [Chao (1984)](https://www.jstor.org/stable/2531049) — species richness estimator
- [Dong et al. (2015)](https://dl.acm.org/doi/10.14778/2752939.2752947) — Knowledge-Based Trust
- [Angelopoulos et al. (2023)](https://arxiv.org/abs/2311.01453) — Prediction-Powered Inference
