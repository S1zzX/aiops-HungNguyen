import time
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total', ['service','status'])
REQUEST_LATENCY = Histogram('http_request_duration_seconds', 'Latency', ['service'])

@app.route("/health")
def health():
    REQUEST_COUNT.labels("payment-svc","200").inc()
    return jsonify({"status": "ok", "service": "payment-svc"})

@app.route("/pay", methods=["POST"])
def pay():
    start = time.time()
    time.sleep(0.05)
    REQUEST_LATENCY.labels("payment-svc").observe(time.time()-start)
    REQUEST_COUNT.labels("payment-svc","200").inc()
    return jsonify({"status": "ok", "txn_id": "txn_123"})

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082)
