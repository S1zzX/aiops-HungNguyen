import os, time, requests
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total requests', ['service','status'])
REQUEST_LATENCY = Histogram('http_request_duration_seconds', 'Latency', ['service'])
API_GW = os.getenv("API_GATEWAY_URL", "http://api-gateway:8081")

@app.route("/")
def index():
    start = time.time()
    try:
        r = requests.get(f"{API_GW}/health", timeout=2)
        status = str(r.status_code)
    except Exception:
        status = "500"
    REQUEST_COUNT.labels("frontend", status).inc()
    REQUEST_LATENCY.labels("frontend").observe(time.time() - start)
    return jsonify({"service": "frontend", "gateway_status": status})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "frontend"})

@app.route("/checkout/health")
def checkout_health():
    start = time.time()
    try:
        r = requests.get(f"{API_GW}/checkout/health", timeout=2)
        ok = r.status_code == 200
    except Exception:
        ok = False
    latency = time.time() - start
    if ok and latency < 0.5:
        return jsonify({"status": "ok", "latency_ms": int(latency*1000)})
    return jsonify({"status": "degraded", "latency_ms": int(latency*1000)}), 503

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
