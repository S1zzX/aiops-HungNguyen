#!/usr/bin/env python3
"""
End-to-end outage scenario runner — Cloudflare WAF regex (2019-07-02) reproduction.
Windows + Docker Desktop version — uses `docker compose` instead of raw uvicorn/pkill,
so it works regardless of host OS as long as Docker is running.

Run this from inside the `reproduction/` folder (the one with docker-compose.yml),
AFTER you have already done:
    docker compose up -d          (starts the api service with EVIL_REGEX_ACTIVE=0)
    cd ../pipeline && docker build -t aiops-pipeline . && docker run -d -p 8000:8000 --name pipeline aiops-pipeline

Produces (written into the current folder):
  - timeline.json          (>= 8 UTC-timestamped events)
  - alerts_observed.json   (GET /alerts from pipeline)
  - rca_observed.json      (POST /rca from pipeline)
"""
import json
import time
import subprocess
from datetime import datetime, timezone

import httpx

REPRO_URL = "http://localhost:8888"
PIPE_URL = "http://localhost:8000"

events = []


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(source, event, ts=None):
    e = {"ts": ts or now_iso(), "source": source, "event": event}
    events.append(e)
    print(f"[{e['ts']}] ({source}) {event}")
    return e


def probe_latency(url, timeout=20.0):
    """Synthetic prober — what a real blackbox monitor does: hit an endpoint, measure latency.

    Under catastrophic backtracking, the worker can become so unresponsive that Docker/Windows
    forcibly aborts the socket (httpx.ReadError / WinError 10053) before our own timeout fires.
    That abort IS the symptom — treat it the same as a timeout: record elapsed time, no status.
    """
    t0 = time.time()
    try:
        r = httpx.get(url, timeout=timeout)
        dt = time.time() - t0
        return dt, r.status_code
    except (httpx.TimeoutException, httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
        dt = time.time() - t0
        print(f"  (probe did not get a clean response after {dt:.1f}s: {type(exc).__name__} — treating as outage symptom)")
        return dt, None


def run(cmd, **kw):
    """Run a command, print it, raise on failure unless check=False is passed."""
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, shell=False, capture_output=True, text=True, **kw)


def main():
    window_start = int(time.time())

    # 0. Sanity check both services are reachable before starting
    dt, code = probe_latency(f"{REPRO_URL}/healthz", timeout=5)
    if code != 200:
        print("ERROR: reproduction app not reachable at :8888 — run 'docker compose up -d' first.")
        return
    dt, code = probe_latency(f"{PIPE_URL}/health", timeout=5)
    if code != 200:
        print("ERROR: pipeline not reachable at :8000 — start the pipeline container first.")
        return

    # 1. Baseline measurement (T0)
    log_event("scenario", "baseline measurement starting — WAF rule not yet deployed")
    dt, code = probe_latency(f"{REPRO_URL}/healthz")
    log_event("probe", f"GET /healthz baseline latency={dt*1000:.1f}ms status={code}")

    # 2. Deploy trigger — flip EVIL_REGEX_ACTIVE=1 via docker compose (recreate container with new env)
    log_event("scenario", "deploy: new WAF rule pushed globally (no canary) — EVIL_REGEX_ACTIVE=1")
    import os
    env = os.environ.copy()
    env["EVIL_REGEX_ACTIVE"] = "1"
    subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "api"], env=env, check=False)
    log_event("docker", "container recreated: api (EVIL_REGEX_ACTIVE=1)")

    # Wait for the container to actually be ready (it re-runs `pip install` on every start,
    # per docker-compose.yml's command, so 3s is not always enough).
    print("Waiting for container to come back up...")
    ready = False
    for attempt in range(20):
        time.sleep(2)
        dt_wait, code_wait = probe_latency(f"{REPRO_URL}/healthz", timeout=4)
        if code_wait == 200:
            ready = True
            print(f"  container ready after ~{(attempt+1)*2}s")
            break
    if not ready:
        print("WARNING: container did not become ready in 40s — continuing anyway.")

    # 3. First symptom — adversarial input pins CPU (no trailing '=' forces exponential backtracking)
    #    Measured on the pack's exact regex in a Linux sandbox: n=20 -> 0.13s, n=25 -> 4.17s (ratio ~2x/char).
    #    n=23 sits right on the boundary and was observed NOT to blow up on a Windows run (CPU/Python-version
    #    dependent). n=26 extrapolates to ~8s — clearly past the boundary, still comfortably inside timeout.
    log_event("scenario", "first user-visible symptom window begins")
    adversarial_query = "x" * 26
    t_request_start = now_iso()
    dt, code = probe_latency(f"{REPRO_URL}/?q={adversarial_query}", timeout=25.0)
    log_event("probe", f"GET /?q=<26x> latency={dt*1000:.1f}ms status={code}", ts=t_request_start)

    # 4. Repeat probe — confirm sustained degradation
    dt2, code2 = probe_latency(f"{REPRO_URL}/?q={adversarial_query}", timeout=25.0)
    log_event("probe", f"GET /?q=<26x> repeat latency={dt2*1000:.1f}ms status={code2}")

    fire_ts = int(time.time())

    # 5. Synthetic monitor reports to pipeline
    log_event("monitor", f"latency probe flags p99 breach (observed {dt*1000:.0f}ms vs 500ms SLO threshold)")
    ingest_payload = {
        "service": "api-gateway",
        "fault_class": "cpu_saturation",
        "severity": "critical",
        "fire_ts": fire_ts,
    }
    try:
        r = httpx.post(f"{PIPE_URL}/ingest", json=ingest_payload, timeout=10)
        log_event("pipeline", f"ingested alert: {ingest_payload} -> {r.json()}")
    except httpx.HTTPError as exc:
        log_event("pipeline", f"FAILED to ingest alert {ingest_payload}: {type(exc).__name__}")

    # 6. Downstream symptom
    ingest_payload_2 = {
        "service": "frontend",
        "fault_class": "cascade_latency",
        "severity": "warning",
        "fire_ts": fire_ts + 2,
    }
    try:
        r2 = httpx.post(f"{PIPE_URL}/ingest", json=ingest_payload_2, timeout=10)
        log_event("pipeline", f"ingested alert: {ingest_payload_2} -> {r2.json()}")
    except httpx.HTTPError as exc:
        log_event("pipeline", f"FAILED to ingest alert {ingest_payload_2}: {type(exc).__name__}")

    window_end = int(time.time()) + 5

    # 7. Query pipeline /alerts and /rca
    log_event("scenario", "querying pipeline /alerts and /rca for the incident window")
    alerts_observed = []
    rca_observed = {}
    try:
        alerts_resp = httpx.get(f"{PIPE_URL}/alerts", params={"since": window_start}, timeout=15)
        alerts_observed = alerts_resp.json()
        log_event("pipeline", f"GET /alerts returned {len(alerts_observed)} alert(s)")
    except httpx.HTTPError as exc:
        log_event("pipeline", f"FAILED to query /alerts: {type(exc).__name__}")
    with open("alerts_observed.json", "w") as f:
        json.dump(alerts_observed, f, indent=2)

    try:
        rca_resp = httpx.post(
            f"{PIPE_URL}/rca",
            json={"window_start": window_start, "window_end": window_end},
            timeout=15,
        )
        rca_observed = rca_resp.json()
        log_event("pipeline", f"POST /rca returned root_service={rca_observed.get('root_service')} confidence={rca_observed.get('confidence')}")
    except httpx.HTTPError as exc:
        log_event("pipeline", f"FAILED to query /rca: {type(exc).__name__}")
    with open("rca_observed.json", "w") as f:
        json.dump(rca_observed, f, indent=2)

    # 8. Mitigation — roll back the WAF rule
    env["EVIL_REGEX_ACTIVE"] = "0"
    run(["docker", "compose", "up", "-d", "--force-recreate", "api"], env=env, check=False)
    log_event("scenario", "mitigation: WAF rule rolled back globally (EVIL_REGEX_ACTIVE=0)")

    # 9. Verify recovery — retry a few times, since the old worker may still be finishing
    #    its CPU-pinned request even after force-recreate is issued.
    dt3, code3 = None, None
    for attempt in range(5):
        time.sleep(2)
        dt3, code3 = probe_latency(f"{REPRO_URL}/healthz", timeout=5)
        if code3 == 200:
            break
    log_event("probe", f"GET /healthz post-rollback latency={dt3*1000:.1f}ms status={code3} — recovery confirmed")

    # Write timeline.json
    events.sort(key=lambda e: e["ts"])
    with open("timeline.json", "w") as f:
        json.dump(events, f, indent=2)
    print(f"\nWrote timeline.json with {len(events)} events")
    print(f"Wrote alerts_observed.json with {len(alerts_observed)} alerts")
    print(f"Wrote rca_observed.json: root_service={rca_observed.get('root_service')}")


if __name__ == "__main__":
    main()