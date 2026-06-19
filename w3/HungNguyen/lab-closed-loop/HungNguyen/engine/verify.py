import time
import requests
from engine.logger import JsonLogger

log = JsonLogger("verify")

def query_prometheus(prometheus_url: str, promql: str) -> float | None:
    try:
        resp = requests.get(f"{prometheus_url}/api/v1/query",
                            params={"query": promql}, timeout=5)
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception as exc:
        log.error("PROMETHEUS_QUERY_ERROR", query=promql, error=str(exc))
    return None

def verify_service(prometheus_url, service, baseline, timeout_s, poll_interval_s, min_samples) -> bool:
    thresholds = baseline["verify_thresholds"]
    queries = baseline["prometheus_queries"]
    latency_q = queries["latency_p99"].replace("{service}", service)
    up_q = queries["up"].replace("{service}", service)
    # NOTE: baseline.json's error_rate_pct query references "http_errors_total",
    # a metric the mock service never emits. Using the real metric instead:
    error_q = (
        f'rate(http_requests_total{{service="{service}",status="500"}}[2m]) '
        f'/ (rate(http_requests_total{{service="{service}"}}[2m]) + 0.001) * 100'
    )

    deadline = time.time() + timeout_s
    passes = 0
    samples = 0
    log.info("VERIFY_START", service=service, timeout_s=timeout_s)

    while time.time() < deadline:
        latency = query_prometheus(prometheus_url, latency_q)
        up = query_prometheus(prometheus_url, up_q)
        error_rate = query_prometheus(prometheus_url, error_q)
        samples += 1

        latency_ok = latency is not None and latency < thresholds["latency_p99_max_ms"]
        up_ok = up is not None and up >= thresholds["up_required"]
        error_ok = error_rate is not None and error_rate < thresholds["error_rate_max_pct"]

        log.info("VERIFY_SAMPLE", service=service, sample=samples,
                 latency_p99_ms=latency, up=up, error_rate_pct=error_rate,
                 latency_ok=latency_ok, up_ok=up_ok, error_ok=error_ok)

        if latency_ok and up_ok:
            passes += 1
            if passes >= min_samples:
                log.info("VERIFY_PASS", service=service, samples=samples)
                return True
        else:
            passes = 0
        time.sleep(poll_interval_s)

    log.warning("VERIFY_FAIL", service=service, samples=samples)
    return False