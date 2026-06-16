import time
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total', ['service','status'])
REQUEST_LATENCY = Histogram('http_request_duration_seconds', 'Latency', ['service'])

@app.route("/health")
def health():
    REQUEST_COUNT.labels("inventory-svc","200").inc()
    return jsonify({"status": "ok", "service": "inventory-svc"})

@app.route("/check")
def check():
    start = time.time()
    time.sleep(0.03)
    REQUEST_LATENCY.labels("inventory-svc").observe(time.time()-start)
    REQUEST_COUNT.labels("inventory-svc","200").inc()
    return jsonify({"status": "ok", "stock": 100})

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8083)
