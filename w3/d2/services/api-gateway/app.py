import os, time, requests
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total', ['service','status'])
REQUEST_LATENCY = Histogram('http_request_duration_seconds', 'Latency', ['service'])

PAYMENT  = os.getenv("PAYMENT_URL",  "http://payment-svc:8082")
INVENTORY= os.getenv("INVENTORY_URL","http://inventory-svc:8083")
CHECKOUT = os.getenv("CHECKOUT_URL", "http://checkout-svc:8084")
AUTH     = os.getenv("AUTH_URL",     "http://auth-svc:8085")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "api-gateway"})

@app.route("/checkout/health")
def checkout_health():
    start = time.time()
    try:
        r = requests.get(f"{CHECKOUT}/health", timeout=2)
        ok = r.status_code == 200
    except Exception:
        ok = False
    REQUEST_LATENCY.labels("api-gateway").observe(time.time() - start)
    code = 200 if ok else 503
    REQUEST_COUNT.labels("api-gateway", str(code)).inc()
    return jsonify({"status": "ok" if ok else "degraded", "service": "api-gateway"}), code

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)
