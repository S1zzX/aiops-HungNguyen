import time
from flask import Flask, jsonify
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total', ['service','status'])
INGESTION_LAG = Gauge('log_ingestion_lag_seconds', 'Log ingestion lag')

@app.route("/health")
def health():
    INGESTION_LAG.set(0.1)
    REQUEST_COUNT.labels("log-collector","200").inc()
    return jsonify({"status": "ok", "service": "log-collector", "lag_seconds": 0.1})

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8087)
