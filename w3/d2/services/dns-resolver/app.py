import time
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)
REQUEST_COUNT = Counter('http_requests_total', 'Total', ['service','status'])
DNS_LATENCY = Histogram('dns_resolution_seconds', 'DNS resolution time')

@app.route("/health")
def health():
    start = time.time()
    time.sleep(0.01)
    DNS_LATENCY.observe(time.time()-start)
    REQUEST_COUNT.labels("dns-resolver","200").inc()
    return jsonify({"status": "ok", "service": "dns-resolver"})

@app.route("/resolve")
def resolve():
    start = time.time()
    time.sleep(0.01)
    DNS_LATENCY.observe(time.time()-start)
    return jsonify({"status": "ok", "ip": "10.0.0.1"})

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8088)
