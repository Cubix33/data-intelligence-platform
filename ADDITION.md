# ADDITION: Jev inside Scout

Using Jev (TypeSafe AI's System One decision model) as Scout's **decision layer**, the same role it plays in our PS02 build (`H:\code cubicle\web\src\adapters\jev\`).

**Rule:** the LLM (Groq gpt-oss-120b) *extracts* values; deterministic code *measures* (quote on page, Chao2, dedupe); **Jev *decides*** every yes/no or pick-one judgement, and returns a calibrated probability for it. Jev never writes text, so it can't hallucinate a value. It only judges values the LLM already pulled out.

## Why it's a strong fit (not decoration)

| Problem we hit in the 27 Sep end-to-end test | What Jev fixes |
|---|---|
| Groq free-tier TPM limit (8,000 tokens/min) stalled runs; we had to add 5-key rotation | Jev pre-screens pages, so irrelevant pages never reach Groq. Fewer extraction calls, less 429. |
| DeBERTa NLI verifier runs one CPU forward pass **per claim**, synchronously. That was the root cause of "Stop Searching" hanging for minutes. | One Jev call scores a whole batch of claims (e.g. 8–20 claims per request) in about 70–300 ms. No local model, no GPU needed. |
| DeBERTa gave **0.02** to FinX `investors = "Gokul Rajaram, Amit Singhal, Prashant Sharma"` against quote `"Gokul Rajaram\nAmit Singhal\nPrashant Sharma"`, a correct cell scored as wrong because of list formatting | Jev reads the field description plus the whole claim. Formatting differences don't fool a question written as "same people?" |
| DataSpec `filters` ("seed funding", "2024", "Indian edtech") are passed to the extractor but **never checked** afterwards | One `noul` per (record, filter), batched. |
| PPI and Learn-then-Test both need a **calibrated** score `f(x)`. DeBERTa's softmax isn't calibrated for our task. | Jev is trained for calibrated probabilities (RLCD). Better calibration means narrower PPI intervals and a looser conformal threshold, so fewer cells are thrown away for the same guarantee. **Test this on our labelled cells before claiming it.** |
| KBT truth discovery picks winners by domain trust only | Jev `choice` across the conflicting claims gives a content-aware vote that feeds into KBT as the per-claim prior. |

Pricing and latency (from our research notes in `archive/allabout jev/`): about $0.042 per million input tokens, output free, 70–300 ms per call, and all questions in one call are evaluated in parallel.

---

## Where Jev plugs in (exact pipeline)

```
prompt
  └─► [Groq] parse_intent → DataSpec
        └─► capture loop (query × engine × source_type)
              └─► search → URLs
                    └─► compliance gate (robots.txt)                 [code]
                          └─► fetch → chunks                          [code]
 J1 ───────────────────────► [JEV] page_gate(chunk)  ── skip if noul < 0.35
                                └─► [Groq] extract_records (value + verbatim quote)
                                      └─► quote-on-page + value-in-quote  [code]
 J2 ─────────────────────────────────► [JEV] claim_support(batch of claims) → support_score
 J3 ─────────────────────────────────► [JEV] filter_check(batch of records × filters)
                                            └─► entity key (normalize)     [code]
 J4 ─────────────────────────────────► [JEV] same_entity(grey-zone pairs)
                                                  └─► upsert + claims + Chao2 [code]
 end of run:
 J5 ──► [JEV] pick_value(conflicting claims) → prior for KBT truth.py
        └─► PPI audit uses J2 scores as f(x)                               [code]
        └─► Learn-then-Test threshold on J2 scores                         [code]
```

J1, J2 and J3 are the must-haves. J4 and J5 are nice-to-have.

### Transport

Scout is Python. PS02 used the TypeScript SDK `@typesafe-ai/sdk` (`client.systemOne({state, questions})`). For Scout, call the REST endpoint directly with `httpx`. That avoids depending on a Python SDK whose package name (`typesafe-sdk`) comes only from our research notes and has **not been verified**.

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer $TYPESAFE_API_KEY          # JEV_API_KEY in code cubicle/.env
{ "model": "jev-latest", "state": {...}, "questions": {...} }
```

Answer shapes (same as `adapters/jev/client.ts`):
- `noul` → `{"type":"noul","noul":0.91}`
- `choice` → `{"type":"choice","choice":"x","confidence":0.8,"probabilities":{...}}`
- `score` → `{"type":"score","score":1.4,"confidence":..,"probabilities":{...}}`

---

## J1: Page relevance gate (before Groq extraction)

**Where:** `pipeline.py`, inside `for chunk_text in chunks:`, before `llm.extract_records`.

**Input**
```json
{
  "model": "jev-latest",
  "state": {
    "wanted_entity": "Indian edtech startups",
    "wanted_filters": ["seed funding", "2024", "Indian edtech"],
    "page_url": "https://foundersday.co/startupfunding/india-vc-funding-tracker-for-edtech-startups",
    "page_text": "India VC Funding Tracker for EdTech Startups ... FinX ... $6M raised in this seed round ... December 12, 2024 ... Gokul Rajaram ..."
  },
  "questions": {
    "lists_entities": {
      "type": "noul",
      "instructions": "Does `page_text` name one or more specific `wanted_entity` (by name) that could satisfy `wanted_filters`? Navigation text, ads or a generic article about the topic without named entities is false."
    }
  }
}
```
**Expected output**
```json
{ "answers": { "lists_entities": { "type": "noul", "noul": 0.93 } }, "model": "jev-latest", "usage": { "input_tokens": 2150 } }
```
**Rule:** `noul < 0.35`: skip the chunk, log the source as `skipped: jev page gate (0.12)`. `0.35–0.6`: extract anyway (we'd rather over-extract). Log every gate decision to the source ledger so skips are visible in the UI.

**Expected effect:** fewer Groq calls per run. Measure it: log `chunks_gated / chunks_total` in `stats`.

---

## J2: Claim support (replaces the per-claim DeBERTa call)

**Where:** `pipeline.py`. Collect all claims for one page, send one Jev call, and write `support_score` before `db.insert_claim`. Replace `verifier.score_support` with a `jev_verifier.score_claims(batch)`, and keep DeBERTa as the offline fallback (`SCOUT_VERIFIER=jev|deberta`).

**Input** (the two cells DeBERTa got wrong in our real run, plus a normal one)
```json
{
  "model": "jev-latest",
  "state": {
    "claims": [
      { "entity": "FinX", "field": "investors",
        "field_description": "Comma-separated list of investors participating in the seed round",
        "value": "Gokul Rajaram, Amit Singhal, Prashant Sharma",
        "quote": "Gokul Rajaram\nAmit Singhal\nPrashant Sharma" },
      { "entity": "Monster Energy", "field": "company_name",
        "field_description": "Name of a company that sponsored a student hackathon in India in 2025",
        "value": "Monster Energy",
        "quote": "Monster Energy" },
      { "entity": "FinX", "field": "funding_round",
        "field_description": "Type of financing round (e.g., Seed)",
        "value": "Seed",
        "quote": "$6M raised in this seed round." }
    ]
  },
  "questions": {
    "c0": { "type": "noul", "instructions": "Does `claims[0].quote` state that the `claims[0].field_description` of `claims[0].entity` is `claims[0].value`? Formatting differences (commas vs line breaks, case) do not matter; missing or different facts do." },
    "c1": { "type": "noul", "instructions": "Does `claims[1].quote` state that the `claims[1].field_description` of `claims[1].entity` is `claims[1].value`? Formatting differences (commas vs line breaks, case) do not matter; missing or different facts do." },
    "c2": { "type": "noul", "instructions": "Does `claims[2].quote` state that the `claims[2].field_description` of `claims[2].entity` is `claims[2].value`? Formatting differences (commas vs line breaks, case) do not matter; missing or different facts do." }
  }
}
```
**Expected output**
```json
{ "answers": {
    "c0": { "type": "noul", "noul": 0.90 },
    "c1": { "type": "noul", "noul": 0.45 },
    "c2": { "type": "noul", "noul": 0.95 }
} }
```
How to read it:
- `c0`: the list formatting is handled (DeBERTa scored this cell 0.02).
- `c1`: the quote is just the name. It doesn't say Monster Energy *sponsored a hackathon*, so the probability should sit near the middle. That is the **honest** answer, and it routes the cell to the audit queue. It's a better signal than DeBERTa's 0.04, which reads as "definitely false".

**Important:** give the quote *plus about 300 chars of surrounding page text* (`page_context`), so c1 can see "Sponsors: Youtube, Physics Wallah, GeeksforGeeks, Monster Energy" and score high. The current verifier never passes context (`score_claims_batch` builds the premise with `""`).

**Batch size:** 8–20 claims per call, the same pattern as `triageBatchSize: 8` in PS02. Split `usage.input_tokens` across the batch for per-cell cost, like `tokensEach` in `client.ts`.

---

## J3: Filter check (fixes "filters never checked")

**Where:** after a record is built and before `upsert_record`. Batch across the records from one page.

**Input**
```json
{
  "model": "jev-latest",
  "state": {
    "records": [
      { "startup_name": "Sparkl Edventure", "funding_round": "Seed", "investors": "Elevar Equity, Himanshu Vyapak",
        "evidence": "Aakash Chaudhry's new edtech startup just raised $4M ... seed funding" },
      { "startup_name": "FinX", "funding_round": "Seed", "funding_date": "2024-12-12",
        "evidence": "FinX ... $6M raised in this seed round ... December 12, 2024" }
    ],
    "filters": ["seed funding", "2024", "Indian edtech"]
  },
  "questions": {
    "r0_f0": { "type": "noul", "instructions": "Does the evidence for `records[0]` satisfy the filter `filters[0]`?" },
    "r0_f1": { "type": "noul", "instructions": "Does the evidence for `records[0]` satisfy the filter `filters[1]`?" },
    "r0_f2": { "type": "noul", "instructions": "Does the evidence for `records[0]` satisfy the filter `filters[2]`?" },
    "r1_f0": { "type": "noul", "instructions": "Does the evidence for `records[1]` satisfy the filter `filters[0]`?" },
    "r1_f1": { "type": "noul", "instructions": "Does the evidence for `records[1]` satisfy the filter `filters[1]`?" },
    "r1_f2": { "type": "noul", "instructions": "Does the evidence for `records[1]` satisfy the filter `filters[2]`?" }
  }
}
```
**Expected output**
```json
{ "answers": {
    "r0_f0": {"type":"noul","noul":0.94}, "r0_f1": {"type":"noul","noul":0.50}, "r0_f2": {"type":"noul","noul":0.85},
    "r1_f0": {"type":"noul","noul":0.95}, "r1_f1": {"type":"noul","noul":0.96}, "r1_f2": {"type":"noul","noul":0.40}
} }
```
**Rule:** a record passes if every filter has `noul ≥ 0.5`. Anything `< 0.2` is dropped. In between, keep it with a `filter_uncertain` badge. Our real run shows why this matters. The prompt said "edtech", but the foundersday tracker page surfaced **FinX**, whose name suggests fintech. Only a filter check catches that.

Store the per-filter probabilities in `provenance["_filters"]` so the provenance drawer can show why a record is in or out.

---

## J4: Same entity? (grey-zone entity resolution)

**Where:** `entity_resolution.py`, for pairs whose normalized names differ but are close (token Jaccard 0.5–0.9, or one contains the other).

**Input**
```json
{
  "model": "jev-latest",
  "state": {
    "a": { "name": "Physics Wallah", "source": "https://theelites.in/hackathons" },
    "b": { "name": "PhysicsWallah Pvt Ltd", "source": "https://innerve-x.devpost.com/" }
  },
  "questions": {
    "same": { "type": "noul", "instructions": "Do `a` and `b` refer to the same real-world organisation?" }
  }
}
```
**Expected output:** `{"answers":{"same":{"type":"noul","noul":0.96}}}`. At `≥ 0.8`, `UnionFind.union(a, b)`.

This matters for pillar A: a missed merge counts one entity twice, the second copy is "found once", Q1 goes up, and Chao2 **underestimates coverage**. Jev-backed entity resolution makes the coverage number more honest.

---

## J5: Pick value for a conflicting cell (prior for KBT)

**Where:** `truth.py`. Before the trust iteration, seed each value's probability with Jev's choice probability instead of the flat `support_score or 0.5`.

**Input**
```json
{
  "model": "jev-latest",
  "state": {
    "entity": "Sparkl Edventure", "field": "funding_amount_usd",
    "claims": [
      { "value": "4000000", "quote": "just raised $4M", "source": "theentrepreneurstory.com" },
      { "value": "400000",  "quote": "raised Rs 4 crore", "source": "foundersday.co" }
    ]
  },
  "questions": {
    "winner": { "type": "choice",
      "instructions": "Which value is best supported as the `field` of `entity`, reading each `quote`? Watch units and currency.",
      "criteria": { "v0": "4000000", "v1": "400000", "neither": "Quotes don't support either value" } }
  }
}
```
**Expected output**
```json
{ "answers": { "winner": { "type": "choice", "choice": "v0", "confidence": 0.7,
  "probabilities": { "v0": 0.78, "v1": 0.17, "neither": 0.05 } } } }
```
Feed `probabilities` in as `value_prob` initial values in `resolve_conflicts`. Show them in the conflict drawer: "Jev: 78% for $4M (unit check)".

---

## How this strengthens the research claims

1. **PPI (pillar B).** `f(x)` = J2 `noul`. With a better-calibrated `f`, `Var(y − f)` shrinks and the accuracy interval narrows for the same number of human labels. **Experiment:** on the same 300 labelled cells, compare PPI interval width with DeBERTa vs Jev as `f`. That gives one plot and a concrete claim.
2. **Conformal threshold.** Run Learn-then-Test on J2 scores. Report "cells accepted at α = 5%" for DeBERTa vs Jev.
3. **Coverage (pillar A).** J4 reduces false "found once" entities, which improves Chao2 calibration on WideSearch gold sets.
4. **Cost.** Log `usage.input_tokens` per Jev call and Groq tokens per run, and show "$ per verified cell". The target is to beat Groq-only extraction on cost, not just accuracy.

## Implementation checklist

- [ ] `api/app/jev.py`: `ask(state, questions) -> answers` via `httpx.post`, 8 s timeout. On connection error, timeout, 429 or 5xx, raise `JevUnavailable` (same idea as `DecisionsUnavailable` in PS02).
- [ ] `config.py`: `TYPESAFE_API_KEY` (or `JEV_API_KEY`), `SCOUT_JEV_ENABLED`, thresholds `JEV_PAGE_GATE=0.35`, `JEV_FILTER_DROP=0.2`, `JEV_SAME_ENTITY=0.8`.
- [ ] J1 page gate, then J2 batched claim support (with page context), then J3 filter check.
- [ ] Fallbacks: if Jev is unavailable, J1 lets every page through, J2 uses DeBERTa, J3 skips the check. **A run never fails because Jev is down.**
- [ ] Record every Jev decision (question, state, answer, probabilities, latency, tokens) in a `decisions` table. PS02's `Decision<T>` type is the template. The drawer can then show *why*.
- [ ] Unit tests with a fake Jev (PS02 has `test/fakes.ts` and `jev-adapter.test.ts` to copy the pattern from).
- [ ] Before/after eval on 5–10 tasks: Groq calls per run, cells kept, PPI interval width, wall time.

## Caveats

- The "Expected output" numbers above are **illustrative**, not measured. Run them against the real API before quoting any of them.
- Pricing and latency figures come from our own research notes in `archive/allabout jev/`. Check them against TypeSafe's current docs.
- Jev only judges. Value extraction stays with the LLM.
