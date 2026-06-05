from fastapi import FastAPI, Request
from collections import deque
import json, statistics, uvicorn, time
from datetime import datetime, timezone

app = FastAPI()
ALERTS_FILE = "alerts.jsonl"
WINDOW_SIZE = 20

# Cooldown config (giây thực) — sau bao lâu thì cho phép fire lại cùng loại alert
COOLDOWN = {
    "warning": 120,   # warning: fire lại sau 2 phút
    "critical": 60,   # critical: fire lại sau 1 phút (quan trọng hơn → nhắc nhanh hơn)
}

windows = {
    "memory_usage_bytes": deque(maxlen=WINDOW_SIZE),
    "cpu_usage_percent": deque(maxlen=WINDOW_SIZE),
    "http_requests_per_sec": deque(maxlen=WINDOW_SIZE),
    "http_p99_latency_ms": deque(maxlen=WINDOW_SIZE),
    "http_5xx_rate": deque(maxlen=WINDOW_SIZE),
    "jvm_gc_pause_ms_avg": deque(maxlen=WINDOW_SIZE),
    "queue_depth": deque(maxlen=WINDOW_SIZE),
    "upstream_timeout_rate": deque(maxlen=WINDOW_SIZE),
}

# Trạng thái alert — lưu thời điểm fire gần nhất của từng loại
# key: "memory_leak_critical", value: timestamp thực (time.time())
last_fired: dict = {}
tick_count = 0


def can_fire(alert_key: str, severity: str) -> bool:
    """Trả về True nếu alert này được phép fire (chưa fire hoặc đã hết cooldown)."""
    last = last_fired.get(alert_key)
    if last is None:
        return True
    return (time.time() - last) >= COOLDOWN[severity]


def write_alert(timestamp, fault_type, severity, message):
    alert_key = f"{fault_type}_{severity}"
    alert = {
        "timestamp": timestamp,
        "type": fault_type,
        "severity": severity,
        "message": message,
    }
    with open(ALERTS_FILE, "a") as f:
        f.write(json.dumps(alert) + "\n")
    last_fired[alert_key] = time.time()  # ghi nhận thời điểm fire
    cooldown = COOLDOWN[severity]
    print(f"[🚨 ALERT] type={fault_type} | severity={severity} | {message} (cooldown={cooldown}s)")


def detect(metrics, logs, timestamp):
    global tick_count
    tick_count += 1

    # Cập nhật window cho từng metric
    for key in windows:
        windows[key].append(metrics[key])

    # Chưa đủ data để detect baseline
    if len(windows["memory_usage_bytes"]) < WINDOW_SIZE:
        print(f"[tick {tick_count}] Warming up... ({len(windows['memory_usage_bytes'])}/{WINDOW_SIZE})")
        return

    mem = metrics["memory_usage_bytes"]
    mem_limit = metrics["memory_limit_bytes"]
    mem_pct = mem / mem_limit * 100

    gc = metrics["jvm_gc_pause_ms_avg"]
    rps = metrics["http_requests_per_sec"]
    queue = metrics["queue_depth"]
    latency = metrics["http_p99_latency_ms"]
    err_rate = metrics["http_5xx_rate"]
    timeout_rate = metrics["upstream_timeout_rate"]

    # Baseline từ window (bỏ 3 điểm cuối để tránh bị fault contaminate)
    baseline_rps = statistics.mean(list(windows["http_requests_per_sec"])[:-3])
    baseline_latency = statistics.mean(list(windows["http_p99_latency_ms"])[:-3])
    baseline_timeout = statistics.mean(list(windows["upstream_timeout_rate"])[:-3])

    # Log heartbeat mỗi 10 tick
    if tick_count % 10 == 0:
        print(f"[tick {tick_count}] mem={mem_pct:.1f}% | rps={rps} | "
              f"gc={gc:.0f}ms | queue={queue} | 5xx={err_rate:.1f}% | timeout={timeout_rate:.1f}%")

    # ─────────────────────────────────────────────
    # Detect: memory_leak
    # Dấu hiệu: memory > 75% limit VÀ GC tăng vọt
    # ─────────────────────────────────────────────
    if mem_pct > 80 and gc > 80 and can_fire("memory_leak_critical", "critical"):
        write_alert(timestamp, "memory_leak", "critical",
                    f"Memory at {mem_pct:.1f}% of limit ({mem/1e9:.2f}GB), GC pause={gc:.0f}ms — possible memory leak")
        return

    if mem_pct > 65 and gc > 30 and can_fire("memory_leak_warning", "warning"):
        write_alert(timestamp, "memory_leak", "warning",
                    f"Memory growing: {mem_pct:.1f}% of limit, GC pause={gc:.0f}ms")

    # ─────────────────────────────────────────────
    # Detect: traffic_spike
    # Dấu hiệu: RPS tăng > 3x baseline VÀ queue tăng cao
    # ─────────────────────────────────────────────
    if rps > baseline_rps * 3 and queue > 50 and can_fire("traffic_spike_critical", "critical"):
        write_alert(timestamp, "traffic_spike", "critical",
                    f"Traffic spike: {baseline_rps:.0f} → {rps:.0f} req/s ({rps/baseline_rps:.1f}x), queue_depth={queue}")
        return

    if rps > baseline_rps * 2 and queue > 20 and can_fire("traffic_spike_warning", "warning"):
        write_alert(timestamp, "traffic_spike", "warning",
                    f"Traffic rising: {rps:.0f} req/s ({rps/baseline_rps:.1f}x baseline), queue={queue}")

    # ─────────────────────────────────────────────
    # Detect: dependency_timeout
    # Dấu hiệu: upstream_timeout_rate tăng vọt VÀ 5xx tăng
    # ─────────────────────────────────────────────
    if timeout_rate > 15 and err_rate > 5 and can_fire("dependency_timeout_critical", "critical"):
        write_alert(timestamp, "dependency_timeout", "critical",
                    f"Upstream timeout rate={timeout_rate:.1f}%, 5xx_rate={err_rate:.1f}% — dependency failure cascade")
        return

    if timeout_rate > 5 and err_rate > 2 and can_fire("dependency_timeout_warning", "warning"):
        write_alert(timestamp, "dependency_timeout", "warning",
                    f"Upstream timeouts rising: {timeout_rate:.1f}%, 5xx={err_rate:.1f}%")

    # ─────────────────────────────────────────────
    # Detect từ LOGS — phát hiện sớm hơn metrics
    # ─────────────────────────────────────────────
    _detect_from_logs(logs, timestamp)


def _detect_from_logs(logs: list, timestamp: str):
    """Phân tích log entries để phát hiện anomaly sớm hơn metrics."""
    for log in logs:
        level = log.get("level", "")
        msg = log.get("message", "")

        # ── Memory leak signals ──
        if level == "ERROR" and "OutOfMemoryWarning" in msg:
            if can_fire("memory_leak_critical", "critical"):
                write_alert(timestamp, "memory_leak", "critical",
                            f"[LOG] OOM warning detected: {msg}")

        elif level == "WARN" and "GC pause exceeded" in msg:
            if can_fire("memory_leak_warning", "warning"):
                write_alert(timestamp, "memory_leak", "warning",
                            f"[LOG] GC pressure detected: {msg}")

        # ── Traffic spike signals ──
        elif level == "ERROR" and "server overloaded" in msg:
            if can_fire("traffic_spike_critical", "critical"):
                write_alert(timestamp, "traffic_spike", "critical",
                            f"[LOG] Server overload detected: {msg}")

        elif level == "WARN" and "Queue depth high" in msg:
            if can_fire("traffic_spike_warning", "warning"):
                write_alert(timestamp, "traffic_spike", "warning",
                            f"[LOG] Queue buildup detected: {msg}")

        # ── Dependency timeout signals ──
        elif level == "ERROR" and "Circuit breaker OPEN" in msg:
            if can_fire("dependency_timeout_critical", "critical"):
                write_alert(timestamp, "dependency_timeout", "critical",
                            f"[LOG] Circuit breaker opened: {msg}")

        elif level == "WARN" and "Upstream timeout" in msg:
            if can_fire("dependency_timeout_warning", "warning"):
                write_alert(timestamp, "dependency_timeout", "warning",
                            f"[LOG] Upstream timeout rising: {msg}")


@app.post("/ingest")
async def ingest(request: Request):
    payload = await request.json()
    detect(payload["metrics"], payload["logs"], payload["timestamp"])
    return {"status": "ok"}


@app.get("/health")
async def health():
    now = time.time()
    cooldown_status = {
        k: f"{max(0, COOLDOWN['critical' if 'critical' in k else 'warning'] - (now - v)):.0f}s remaining"
        for k, v in last_fired.items()
    }
    return {"status": "ok", "ticks": tick_count, "last_fired": cooldown_status}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
