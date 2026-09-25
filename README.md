# Scout — AI Data Intelligence Platform

Code Cubicle 6.0 · Problem Statement 01. Full ideation in [`IDEATION.md`](./IDEATION.md).

Describe a dataset in plain English. Scout parses your intent into a schema, searches the
web for it, fetches and extracts records from permitted sources, and returns a clean,
deduplicated table where **every cell is traceable back to the exact quote and page it
came from**.

This is the first working draft of the core loop described in IDEATION.md's Phase 1:
`prompt → DataSpec → discover → fetch → extract (with evidence) → validate → dedupe → table`.
Not yet built: the DAG plan editor, live SSE run view, selector caching, monitors, and the
full Next.js frontend — those are Phase 2/3 per the ideation doc.

## How it works

1. **Intent parsing** (`api/app/llm.py::parse_intent`) — Llama 3.3 70B (via Groq) turns your
   prompt into a `DataSpec`: an entity name, 5-8 columns with descriptions, filters, and search queries.
2. **Discovery** (`discover_urls`) — each search query runs through DuckDuckGo (free, no API
   key); candidate URLs are deduplicated.
3. **Compliance Gate** (`api/app/compliance.py`) — every URL is checked against robots.txt
   and a small deny list before it's ever fetched. Every decision is logged to the Source
   Ledger.
4. **Fetch** (`api/app/fetcher.py`) — plain `httpx` + BeautifulSoup text extraction.
5. **Extraction** (`extract_records`) — Llama 3.1 8B (via Groq) reads one page at a time and returns
   records where every field value carries a verbatim quote from the page as evidence.
   A value whose quote can't be found on the page (word-for-word, whitespace-normalized)
   is dropped rather than trusted.
6. **Dedupe** (`api/app/db.py::upsert_record`) — records are merged by a slug of their
   primary field; a field confirmed by a second source is marked `✓✓` (corroborated).
7. **Storage & export** — SQLite for now; CSV export built in, per-cell provenance kept
   alongside every value.

## Running it

### 1. Set up

```bash
cd api
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Copy `.env.example` to `.env` in the repo root and set `GROQ_API_KEY`
(get one free at [console.groq.com](https://console.groq.com/keys)).

### 2a. Quick CLI demo (no server)

```bash
python scripts/demo.py "Find companies that sponsored student hackathons in India in 2025, with sponsorship tier and a contact URL"
```

Prints the plan, a live-ish console table, the source ledger, and writes a CSV to `output/`.

### 2b. Full API + dashboard

```bash
cd api
uvicorn app.main:app --reload --port 8000
```

Then open `http://127.0.0.1:8000/` in a browser — the dashboard is served by the API itself
(`web/index.html` opened directly also works). Start a run, watch status poll, click any cell
to see its source quote in the provenance drawer, export to CSV.

## Project layout

```
api/app/
  config.py       # env, model choices (llama 70b for intent, llama 8b for extraction)
  models.py       # DataSpec / FieldSpec pydantic models
  llm.py          # all AI calls: Groq intent/extraction, DuckDuckGo discovery
  compliance.py   # robots.txt + deny list — the Compliance Gate
  fetcher.py      # httpx + BeautifulSoup page fetching
  pipeline.py     # orchestrates the steps above, writes to SQLite
  db.py           # sqlite storage: runs, records (with provenance), source ledger
  main.py         # FastAPI: POST /api/runs, GET /api/runs/{id}, GET .../export.csv
scripts/demo.py   # terminal-only end-to-end run, no server needed
web/index.html    # single-file dashboard: prompt bar, live status, table, provenance drawer
IDEATION.md       # full product design, architecture, build plan, demo script
```

## Next up (see IDEATION.md for the full plan)

- Editable plan/DAG preview before a run executes
- Live run view over SSE instead of polling
- Selector caching for cheaper repeat extraction
- Cross-source conflict flags, not just corroboration
- Monitors (scheduled re-runs with diffing) and Google Sheets export
