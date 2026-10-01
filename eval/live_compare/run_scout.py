import json, sys, time, urllib.request
BASE = "https://scout-platform.onrender.com"
CASES = {
 "A_vlm_list": "Top open-source vision language models released in 2024 and 2025 with model name, organization, and benchmark score",
 "B_india_seed": "Indian edtech startups that raised seed funding in 2024 with company name, amount raised, and lead investor",
 "C_fresh_versions": "Latest stable versions of popular Python web frameworks (Django, Flask, FastAPI) with version number and release date",
 "D_longtail_events": "Student hackathons held in India in 2025 with event name, city, and a sponsor",
}
def call(method, path, body=None):
    req = urllib.request.Request(BASE+path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))
for name, prompt in CASES.items():
    t0 = time.time()
    rid = call("POST", "/api/runs", {"prompt": prompt, "target_coverage": 0.8})["id"]
    print(name, rid, flush=True)
    while True:
        time.sleep(15)
        try: r = call("GET", f"/api/runs/{rid}")
        except Exception as e: print("poll err", e, flush=True); continue
        el = time.time()-t0
        print(f"  {name} {int(el)}s status={r['status']} records={len(r['records'])}", flush=True)
        if r["status"] in ("done","failed","cancelled"): break
        if el > 600:
            call("DELETE", f"/api/runs/{rid}"); print("  timeout -> cancel", flush=True)
            time.sleep(20); r = call("GET", f"/api/runs/{rid}"); break
    r["_elapsed_s"] = round(time.time()-t0); r["_prompt"] = prompt
    json.dump(r, open(f"scout_{name}.json","w"), indent=1)
print("ALL DONE")
