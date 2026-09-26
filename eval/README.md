# Scout Evaluation Harness

Benchmarks Scout against gold tables to measure Item-F1, Row-F1, and coverage calibration.

## Directory layout

```
eval/
  tasks.json          # benchmark task definitions
  run_bench.py        # run Scout on all tasks, write results/
  score.py            # score results, print ablation table, generate calibration plot
  gold/               # gold tables (JSON) for each task
  results/            # output files from run_bench.py
  page_cache/         # (future) cached page HTML so all ablation runs see the same data
```

## Gold table format

Place a JSON file in `eval/gold/<task_id>.json`:

```json
{
  "entities": ["Google", "GitHub", "Polygon"],
  "rows": [
    {"name": "Google", "sponsorship_tier": "Title", "contact_url": "https://developers.google.com"},
    {"name": "GitHub", "sponsorship_tier": "Gold",  "contact_url": "https://github.com/about"}
  ]
}
```

## Running the benchmark

```bash
cd D:\data-intelligence-platform

# Run all tasks (blocks until done)
python eval/run_bench.py

# Run only task 0
python eval/run_bench.py --task 0

# Score all results
python eval/score.py

# Coverage calibration error + plot
python eval/score.py --calibration --plot
```

## Ablation table

To compare Scout versions, collect results in separate directories and call `build_ablation_table`:

```python
from eval.score import load_results, build_ablation_table

build_ablation_table({
    "v0 (baseline)":            load_results(Path("eval/results_v0")),
    "v1 (+bug fixes)":          load_results(Path("eval/results_v1")),
    "v2 (+coverage loop)":      load_results(Path("eval/results_v2")),
    "v3 (+verifier)":           load_results(Path("eval/results_v3")),
    "v4 (+entity resolution)":  load_results(Path("eval/results_v4")),
})
```

## WideSearch tasks

1. Download the WideSearch English task set from widesearch-seed.github.io
2. Convert each task's gold table to the format above
3. Add entries to `tasks.json`
4. Run `run_bench.py` to evaluate

The target is 30+ tasks for a statistically meaningful calibration plot.
