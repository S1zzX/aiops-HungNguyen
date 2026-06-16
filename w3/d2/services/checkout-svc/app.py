import os, time, requests
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total', ['service','status'])
REQUEST_LATENCY = Histogram('http_request_duration_seconds', 'Latency', ['service'])
PAYMENT  = os.getenv("PAYMENT_URL",  "http://payment-svc:8082")
INVENTORY= os.getenv("INVENTORY_URL","http://inventory-svc:8083")

@app.route("/health")
def health():
    start = time.time()
    try:
        p = requests.get(f"{PAYMENT}/health", timeout=2)
        i = requests.get(f"{INVENTORY}/health", timeout=2)
        ok = p.status_code == 200 and i.status_code == 200
    except Exception:
        ok = False
    REQUEST_LATENCY.labels("checkout-svc").observe(time.time()-start)
    code = 200 if ok else 503
    REQUEST_COUNT.labels("checkout-svc", str(code)).inc()
    return jsonify({"status": "ok" if ok else "degraded", "service": "checkout-svc"}), code

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8084)
