import time
from flask import Flask, jsonify
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total', ['service','status'])

@app.route("/health")
def health():
    REQUEST_COUNT.labels("auth-svc","200").inc()
    return jsonify({"status": "ok", "service": "auth-svc"})

@app.route("/validate")
def validate():
    REQUEST_COUNT.labels("auth-svc","200").inc()
    return jsonify({"valid": True, "user": "user_123"})

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8085)
