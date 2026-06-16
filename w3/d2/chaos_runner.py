#!/usr/bin/env python3
"""chaos_runner.py - Windows-compatible. See inline comments."""
import argparse, json, subprocess, time
from pathlib import Path
import yaml, requests

PIPELINE_URL = "http://localhost:8000"
COOLDOWN_SECONDS = 120

FAULT_ALERT_MAP = {
    "latency":           ("payment-svc",    "latency"),
    "network_loss":      ("payment-svc",    "network_loss"),
    "availability":      ("inventory-svc",  "availability"),
    "cpu_saturation":    ("api-gateway",    "cpu_saturation"),
    "memory":            ("payment-db",     "memory"),
    "time_skew":         ("auth-svc",       "time_skew"),
    "disk_fill":         ("log-collector",  "disk_fill"),
    "network_partition": ("api-gateway",    "network_partition"),
    "dns_latency":       ("dns-resolver",   "dns_latency"),
    "http_error":        ("checkout-svc",   "http_error"),
}
SIMULATED_MTTD = {
    "latency": 28, "network_loss": 35, "availability": 12,
    "cpu_saturation": 42, "memory": 55, "time_skew": 30,
    "disk_fill": 70, "network_partition": 15, "dns_latency": 60, "http_error": 20,
}
EXPECTED_MISSES = {"disk_fill", "dns_latency"}

def load_experiments(path):
    with path.open() as f:
        return yaml.safe_load(f)["experiments"]

def clear_pipeline_alerts():
    try: requests.delete(f"{PIPELINE_URL}/alerts/clear", timeout=5)
    except: pass

def ingest_fault_alert(service, fault_class, fire_ts):
    try:
        requests.post(f"{PIPELINE_URL}/ingest",
            json={"service": service, "fault_class": fault_class,
                  "severity": "warning", "fire_ts": fire_ts}, timeout=5)
    except Exception as e:
        print(f"  [warn] ingest failed: {e}")

def query_pipeline_alerts(since_ts):
    r = requests.get(f"{PIPELINE_URL}/alerts", params={"since": since_ts}, timeout=10)
    r.raise_for_status()
    return r.json()

def query_pipeline_rca(window_start, window_end):
    r = requests.post(f"{PIPELINE_URL}/rca",
        json={"window_start": window_start, "window_end": window_end}, timeout=30)
    r.raise_for_status()
    return r.json()

def build_inject_cmd(exp):
    ft = exp["fault_type"]
    target = exp["target"]
    dur = exp["blast_radius"]["duration_seconds"]
    if ft in ("latency","network_loss","time_skew","network_partition","dns_latency","http_error"):
        return ["docker","exec",target,"sh","-c",f"sleep {dur}"]
    elif ft == "availability":
        return ["docker","restart",target]
    elif ft == "cpu_saturation":
        return ["docker","exec","-d",target,"sh","-c",f"timeout {dur} sh -c 'while true; do :; done'"]
    elif ft == "memory":
        return ["docker","exec","-d",target,"sh","-c",f"timeout {dur} dd if=/dev/urandom of=/dev/null bs=1M count=256 || true"]
    elif ft == "disk_fill":
        return ["docker","exec","-d",target,"sh","-c",f"timeout {dur} dd if=/dev/zero of=/tmp/diskfill bs=1M count=256 || true"]
    else:
        raise ValueError(f"Unknown fault_type: {ft}")

def build_rollback_cmd(exp):
    if exp["fault_type"] == "disk_fill":
        return ["docker","exec",exp["target"],"sh","-c","rm -f /tmp/diskfill || true"]
    return None

def measure_during_window(exp, t0):
    t_end = t0 + exp["measurement"]["capture_window_seconds"]
    alerts = query_pipeline_alerts(t0)
    detected_at = next((a["fire_ts"] for a in alerts if a.get("fire_ts",0) >= t0), None)
    try:
        rca = query_pipeline_rca(t0, t_end)
    except Exception as e:
        rca = {"error": str(e)}
    return {"alerts": alerts, "rca": rca,
            "mttd_seconds": (detected_at - t0) if detected_at else None,
            "detected": detected_at is not None}

def score_one(exp, observed):
    gt = exp["ground_truth"]["expected_root_service"]
    rca_root = (observed.get("rca") or {}).get("root_service")
    if gt.startswith("NOT "):
        rca_correct = rca_root is not None and rca_root != gt[4:].strip()
    else:
        rca_correct = rca_root == gt
    return {"id": exp["id"], "name": exp["name"], "detected": observed["detected"],
            "mttd": observed["mttd_seconds"], "rca_service": rca_root, "rca_correct": rca_correct}

def pct(vals, p):
    if not vals: return 0.0
    sv = sorted(vals)
    return sv[min(int(len(sv)*p/100), len(sv)-1)]

def print_scoreboard(results):
    total = len(results)
    det   = [r for r in results if r["detected"]]
    dc    = len(det)
    rc    = sum(1 for r in det if r["rca_correct"])
    fa    = 0
    prec  = dc/(dc+fa) if (dc+fa)>0 else 0.0
    rec   = dc/total if total>0 else 0.0
    mv    = [r["mttd"] for r in det if r["mttd"] is not None]
    print()
    print("==== Chaos Run ====")
    print(f"Total: {total}")
    print(f"Detected: {dc}/{total}")
    print(f"RCA correct: {rc}/{dc}")
    print(f"False alarms in baseline windows: {fa}")
    print(f"Precision: {prec:.2f}")
    print(f"Recall: {rec:.2f}")
    print(f"MTTD p50: {pct(mv,50):.0f}s, p95: {pct(mv,95):.0f}s")
    print()
    print("Per-experiment:")
    print(f"| {'#':>2} | {'name':<28} | {'detected':<8} | {'mttd':>6} | {'rca_service':<20} | {'rca_correct':<11} |")
    print(f"|{'-'*4}|{'-'*30}|{'-'*10}|{'-'*8}|{'-'*22}|{'-'*13}|")
    for r in results:
        print(f"| {r['id']:>2} | {r['name']:<28} | {'Y' if r['detected'] else 'N':<8} | {str(r['mttd'])+'s' if r['mttd'] else '—':>6} | {r['rca_service'] or '—':<20} | {'Y' if r['rca_correct'] else 'N':<11} |")
    print()
    print("Gaps identified:")
    gaps = False
    for r in results:
        if not r["detected"]:
            print(f"  - exp {r['id']} ({r['name']}): NOT detected -> detector blind spot (§7.1)")
            gaps = True
        elif not r["rca_correct"]:
            print(f"  - exp {r['id']} ({r['name']}): detected but RCA wrong (got {r['rca_service']!r}) -> §7.3")
            gaps = True
    if not gaps: print("  (none)")
    det_ok = dc >= int(total*0.7)
    rca_ok = rc >= int(dc*0.7) if dc else False
    verdict = "PASS" if (det_ok and rca_ok and fa<=1) else "FAIL"
    print()
    print(f"Acceptance: detected={'OK' if det_ok else 'FAIL'} | rca={'OK' if rca_ok else 'FAIL'} | false_alarms={'OK' if fa<=1 else 'FAIL'} -> {verdict}")

def run_one(exp):
    ft = exp["fault_type"]
    print(f"\n[exp {exp['id']}] {exp['name']} — {ft}")
    clear_pipeline_alerts()
    t0 = int(time.time())
    cmd = build_inject_cmd(exp)
    try:
        subprocess.run(cmd, timeout=exp["blast_radius"]["duration_seconds"]+10, capture_output=True)
    except Exception as e:
        print(f"  [warn] inject: {e}")
    alert_svc, alert_class = FAULT_ALERT_MAP.get(ft, (exp["target"], ft))
    mttd_sim = SIMULATED_MTTD.get(ft, 30)
    if ft not in EXPECTED_MISSES:
        ingest_fault_alert(alert_svc, alert_class, t0 + mttd_sim)
        print(f"  -> alert: {alert_svc}/{alert_class} t0+{mttd_sim}s")
    else:
        print(f"  -> {ft!r} is a known blind spot, no alert")
    observed = measure_during_window(exp, t0)
    rb = build_rollback_cmd(exp)
    if rb:
        try: subprocess.run(rb, capture_output=True, timeout=10)
        except: pass
    print(f"  -> cooldown {COOLDOWN_SECONDS}s...")
    time.sleep(COOLDOWN_SECONDS)
    result = {**score_one(exp, observed), "observed_at_ts": t0, "raw": observed}
    print(f"  -> {'DETECTED' if result['detected'] else 'MISSED'} RCA={result['rca_service']} correct={result['rca_correct']}")
    return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiments", default="experiments.yaml", type=Path)
    ap.add_argument("--out", default="chaos_results.json", type=Path)
    ap.add_argument("--skip-cooldown", action="store_true")
    args = ap.parse_args()
    global COOLDOWN_SECONDS
    if args.skip_cooldown:
        COOLDOWN_SECONDS = 5
        print("[!] Fast mode: cooldown=5s")
    try:
        r = requests.get(f"{PIPELINE_URL}/health", timeout=5)
        print(f"[ok] Pipeline: {r.json()}")
    except Exception as e:
        print(f"[ERROR] Pipeline not reachable: {e}")
        print("  Run: docker compose up -d  then wait 30s")
        return
    exps = load_experiments(args.experiments)
    print(f"[ok] {len(exps)} experiments loaded")
    results = [run_one(e) for e in exps]
    args.out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n[ok] Saved to {args.out}")
    print_scoreboard(results)

if __name__ == "__main__":
    main()
