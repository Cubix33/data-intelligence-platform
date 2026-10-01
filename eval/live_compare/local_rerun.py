import os, sys, json, time, uuid
os.environ["SCOUT_DB_PATH"] = os.path.join(os.environ.get("TEMP","."), "scout_rerun.db")
sys.path.insert(0, ".")
from app import db, pipeline
CASES = {
 "A_vlm_list": "Top open-source vision language models released in 2024 and 2025 with model name, organization, and benchmark score",
 "B_india_seed": "Indian edtech startups that raised seed funding in 2024 with company name, amount raised, and lead investor",
 "C_fresh_versions": "Latest stable versions of popular Python web frameworks (Django, Flask, FastAPI) with version number and release date",
 "D_longtail_events": "Student hackathons held in India in 2025 with event name, city, and a sponsor",
}
db.init_db() if hasattr(db, "init_db") else None
out = {}
for name, prompt in CASES.items():
    rid = uuid.uuid4().hex[:12]; db.create_run(rid, prompt); t = time.time()
    pipeline.run_pipeline(rid, prompt)
    r = db.get_run(rid); recs = db.list_records(rid)
    out[name] = {"status": r["status"], "error": r.get("error"), "secs": round(time.time()-t), "stats": r["stats"],
                 "rows": [x["fields"] for x in recs]}
    print(name, r["status"], round(time.time()-t), "s", len(recs), "rows", flush=True)
    for x in recs[:12]: print("   ", json.dumps(x["fields"], ensure_ascii=False)[:200], flush=True)
json.dump(out, open("/h/codecubicleps1/eval/live_compare/local_after_fix.json","w"), indent=1, ensure_ascii=False)
print("ALL DONE")
