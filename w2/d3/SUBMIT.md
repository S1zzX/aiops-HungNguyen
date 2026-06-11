# SUBMIT.md — EOD Checkpoint Reflection (w2/d3)

## 1. Latency budget của endpoint (p99)? Phase nào chiếm thời gian nhất?

p99 thực tế đo được:
- **LLM path (Groq)**: ~1–4s — Groq nhanh hơn OpenAI đáng kể nhờ hardware inference riêng
- **Graph-only path**: < 20ms

Phase chiếm thời gian nhất: **Groq LLM call (~95% total latency)**. L1 correlate + L2 PageRank cộng lại < 15ms — không đáng tối ưu so với LLM.

Optimization đã apply: TTLCache(ttl=3600) — cùng cluster fingerprint trong 1h trả cached response, latency ~0ms.

## 2. Endpoint xử lý 5 alert vs 500 alert — latency khác nhau thế nào?

**Gần như không đổi** trên LLM path vì bottleneck là Groq API call (fixed cost ~2s).

Chi tiết:
- **L1 correlate**: O(N²) trong topology_group (pairwise service check) — 5 alerts ~0.1ms, 500 alerts ~10ms. Vẫn nhỏ so với LLM.
- **L2 PageRank**: O(V+E) trên subgraph alerting services — không scale theo N alerts, chỉ scale theo số service unique.
- **L3 Groq**: fixed ~2s bất kể số alert — prompt chỉ tăng vài KB.

Kết luận: 5 vs 500 alerts → latency tăng < 1% trên LLM path. Nếu AIOPS_USE_LLM=false, 500 alerts có thể thấy ~50ms vs ~5ms (10× nhưng vẫn fast).

## 3. LLM provider down giữa lúc đang chạy — hệ thống behave ra sao?

**Behavior hiện tại**: `call_groq_rca()` raise exception (timeout/connection error/HTTP 5xx) → `run_rca()` catch tất cả exceptions → gọi `_graph_only_result()` → endpoint trả **200 OK** với graph+retrieval result.

SRE vẫn nhận được root_cause (từ PageRank), class (từ keyword retrieval history), actions (từ incident tương tự nhất). Chất lượng thấp hơn (không có LLM reasoning) nhưng pipeline **không down**.

**Phương án dự phòng**:
1. `AIOPS_USE_LLM=false` env var → restart uvicorn → 100% graph-only, zero Groq dependency
2. Timeout 30s trong `urllib.urlopen` — request không hang vô thời hạn
3. TTLCache — requests đã cached trong 1h không bị ảnh hưởng dù Groq down

## 4. /healthz và /readyz khác nhau gì?

| | `/healthz` | `/readyz` |
|---|---|---|
| Câu hỏi | Process còn sống không? | Có thể nhận traffic không? |
| Check | Always 200 nếu uvicorn chạy | graph loaded + history loaded |
| Fail → | Kubernetes restart container | Load balancer bỏ pod khỏi rotation |
| Dùng cho | `livenessProbe` | `readinessProbe` |

Trong rolling deploy: pod mới start → graph chưa load → `/readyz` 503 → LB không route traffic → graph load xong → `/readyz` 200 → traffic bắt đầu vào. Zero downtime.

`/healthz` không check dependencies vì nếu graph load fail, liveness probe không nên restart container (restart sẽ loop vô tận nếu dataset bị lỗi).

## 5. POST 4 request đồng thời — endpoint handle ổn không? Bottleneck đầu tiên?

Với `--workers 1` (single worker, sync `def`):
- 4 request concurrent → xử lý **tuần tự** trong 1 thread
- Tổng thời gian ≈ 4 × ~2s = ~8s cho request cuối

**Bottleneck đầu tiên**: Groq LLM call block thread trong ~2s → 3 request còn lại xếp hàng chờ.

**Fix nếu cần**: chuyển `post_incident` sang `async def` + `aiohttp` thay `urllib` cho Groq call → event loop overlap 4 LLM calls → p99 ≈ ~2s thay vì ~8s.

Với `--workers 1` máy yếu vẫn OK cho demo và grading — bottleneck chỉ thấy ở load test thực sự (ab -c 10+).

---

## Files nộp

```
w2/d3/
├── serve.py              # FastAPI app — endpoints, middleware, schemas
├── pipeline.py           # Glue layer: correlate → rca → format
├── correlate.py          # L1 từ w2/d1 (session window + topology grouping)
├── rca.py                # L2+L3 từ w2/d2 (graph scoring + Groq LLM)
├── DESIGN.md
├── SUBMIT.md
└── dataset/
    ├── services.json         # copy từ w2/d1 dataset
    └── incidents_history.json # copy từ w2/d2 dataset
```
