# SUBMIT.md — EOD Checkpoint Reflection (w2/d3)

## 1. Latency budget của endpoint (p99)? Phase nào chiếm thời gian nhất?

p99 ước tính: **< 10s** trên LLM path, **< 15ms** trên graph-only path.

Breakdown:
- L1 (correlation): ~2–5ms — pure in-memory graph traversal, 9 nodes
- L2 (PageRank RCA): ~2–5ms — networkx trên subgraph nhỏ
- L3 (LLM call): **3–8s** — chiếm ~95% tổng latency

Phase LLM call là bottleneck tuyệt đối. Tối ưu 50ms ở correlation = noise. Tối ưu LLM (cache, smaller model, skip when confidence high) = meaningful improvement.

Middleware `X-Response-Time-Ms` header được thêm để đo từng request mà không cần instrumentation riêng.

## 2. Endpoint xử lý 5 alert vs 500 alert — latency khác nhau thế nào?

**Không linear.** Chi phí chia làm 2 phần:

- **Fixed cost**: LLM call (3–8s) — không đổi dù 5 hay 500 alert vì pipeline chỉ gọi LLM 1 lần cho primary cluster.
- **Variable cost**: correlation + graph traversal — O(N × V) với N là số alert, V là số node trong graph. 5 vs 500 alert → correlation đắt hơn ~100× nhưng chỉ tăng từ 2ms lên khoảng 200ms → vẫn nhỏ so với LLM.

Kết luận: 5 alert vs 500 alert → latency tăng < 5% trong điều kiện LLM enabled. Nếu LLM disabled (graph-only), 500 alert có thể thấy ~10× latency increase (~150ms vs ~15ms).

## 3. LLM provider down giữa lúc đang chạy — hệ thống behave ra sao? Phương án dự phòng?

**Hành vi hiện tại**: `call_llm_rca()` raise exception (timeout hoặc connection error) → `run_rca()` catch exception → fallback sang `_graph_only_rca()` → endpoint vẫn trả `200 OK` với graph-based root cause.

SRE nhận được câu trả lời chất lượng thấp hơn (không có natural-language reasoning, không classify failure class) nhưng pipeline **không down**.

**Phương án dự phòng**:
1. **Feature flag** `AIOPS_USE_LLM=false` — set env var + restart → toàn bộ traffic dùng graph-only, không LLM call nào được thực hiện. Restart < 30s với uvicorn.
2. **Timeout** `OpenAI(timeout=10.0)` — LLM call không bao giờ hang quá 10s, sau đó fallback tự động.
3. **TTLCache** — request giống nhau trong vòng 1 giờ dùng cached response → LLM provider down không ảnh hưởng cached traffic.
4. **Future**: multi-provider fallback (OpenAI → Anthropic → local model), hoặc pre-generated action templates per failure class.

## 4. /healthz và /readyz khác nhau gì? Khi nào dùng cái nào?

| | `/healthz` | `/readyz` |
|---|---|---|
| **Câu hỏi** | Process còn sống không? | Có thể nhận traffic không? |
| **Check** | Always 200 nếu process chạy | Graph loaded? History loaded? |
| **Dùng khi** | Liveness probe (container restart nếu fail) | Readiness probe (remove khỏi load balancer nếu fail) |
| **Fail behavior** | Kubernetes restart container | Kubernetes không route traffic đến pod này |

Kubernetes deployment nên config:
- `livenessProbe` → `/healthz` (restart nếu process crash/deadlock)
- `readinessProbe` → `/readyz` (remove from Service endpoints khi pod mới chưa load xong graph)

Trong rolling deploy: pod mới start → graph load mất 1-2s → `/readyz` trả 503 trong thời gian này → load balancer không route traffic → graph loaded xong → `/readyz` trả 200 → traffic bắt đầu vào. Zero downtime deploy.

## 5. POST 4 request đồng thời — endpoint handle ổn không? Bottleneck đầu tiên?

**Handle được**, nhưng có giới hạn.

Với single-worker uvicorn (default):
- Nếu endpoint là `async def`: event loop xử lý concurrent — 4 request cùng lúc, mỗi cái await LLM call → tổng thời gian ≈ max(latency của 4 request) thay vì sum.
- Nếu endpoint là `def` (sync): 4 request xử lý tuần tự — tổng thời gian ≈ 4 × latency của 1 request.

**Endpoint hiện tại là sync `def`** → 4 concurrent request thực ra xử lý tuần tự trong 1 worker.

**Bottleneck đầu tiên**: LLM call là IO-bound nhưng trong sync endpoint, nó block thread. Worker thread bị giữ trong 8s khi gọi LLM → 3 request kia xếp hàng chờ.

**Fix**: chuyển `post_incident` sang `async def` + dùng async OpenAI client (`AsyncOpenAI`) → event loop overlap multiple LLM calls. Với 4 concurrent request + async, p99 ≈ 1 LLM latency thay vì 4×.

**Bottleneck thứ 2** (khi fix async): TTLCache không thread-safe với multiple coroutines — cần `asyncio.Lock` hoặc dùng `aiocache` library.

---

## Files nộp

```
aiops-<tên>/w2/d3/
├── serve.py           # Main FastAPI service
├── pipeline.py        # Glue layer (correlate → rca → format)
├── correlate.py       # L1: Alert correlation
├── rca.py             # L2+L3: Graph RCA + LLM enrichment
├── requirements.txt
├── Makefile
├── DESIGN.md          # Architecture + decisions
├── SUBMIT.md          # This file
├── dataset/
│   ├── services.json
│   └── incidents_history.json
└── tests/
    ├── test_correlate.py   # 9 unit tests (pure functions)
    └── test_serve.py       # 12 integration tests (HTTP endpoints)
```

Test results: **21/21 passed** (`pytest tests/ -v`)
