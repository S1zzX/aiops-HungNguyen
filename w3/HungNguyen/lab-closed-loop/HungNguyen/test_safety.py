from engine.safety import BlastRadiusGuard, CircuitBreaker, ServiceMutex

print("--- BlastRadiusGuard ---")
guard = BlastRadiusGuard(max_per_minute=3, max_restarts_per_hour=5)
for i in range(4):
    ok, reason = guard.check("payment-svc")
    print(i, ok, reason)
    if ok:
        guard.record("payment-svc")

print("--- CircuitBreaker ---")
cb = CircuitBreaker(threshold=3)
for i in range(4):
    print("before fail", i, "is_open:", cb.is_open())
    cb.record_failure()
print("after 4 failures, is_open:", cb.is_open())

print("--- ServiceMutex ---")
mutex = ServiceMutex()
print("acquire 1:", mutex.try_acquire("payment-svc"))
print("acquire 2:", mutex.try_acquire("payment-svc"))
mutex.release("payment-svc")
print("acquire 3:", mutex.try_acquire("payment-svc"))
