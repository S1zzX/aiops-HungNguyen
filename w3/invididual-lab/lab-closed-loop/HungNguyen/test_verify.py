import json
from engine.verify import verify_service

baseline = json.load(open("data/baseline.json"))
result = verify_service(
    prometheus_url="http://localhost:9090",
    service="payment-svc",
    baseline=baseline,
    timeout_s=30,
    poll_interval_s=5,
    min_samples=2,
)
print("RESULT:", "PASS" if result else "FAIL")
