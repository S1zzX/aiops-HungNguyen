# DESIGN.md — AIOps Incident Pipeline (w2/d3)

## Pipeline Architecture

```
POST /incident
     │
     ▼ Pydantic validation (422 on bad input)
[serve.py]
     │ Latency middleware → X-Response-Time-Ms header
     ▼
[pipeline.py] process_batch()
     │
     ├── L1: correlate() [correlate.py — từ w2/d1]
     │       session_groups(gap_sec=120) → topology_group(max_hop=2)
     │       Output: list[cluster]  (cluster_id, services, time_range, alerts)
     │
     ├── L2: rca_combined() [rca.py — từ w2/d2]
     │       Terminal score + PageRank trên reverse subgraph
     │       + Temporal score (alert earliest = higher)
     │       Output: ranked (service, score)
     │
     └── L3: Groq LLM llama-3.3-70b [rca.py — từ w2/d2 bonus]
             Prompt = cluster + graph top3 + similar incidents
             Output: root_cause, class, confidence, reasoning, actions
             Fallback: graph+retrieval nếu Groq fail hoặc AIOPS_USE_LLM=false
```

## Latency Budget Breakdown

| Phase | p99 ước tính | Ghi chú |
|---|---|---|
| Pydantic validation | < 1ms | In-memory |
| L1 correlate (session + topology) | < 10ms | Union-Find trên ≤20 alerts, graph 10 nodes |
| L2 rca_combined (PageRank) | < 5ms | Subgraph nhỏ |
| L3 Groq LLM call | 1–4s | Llama-3.3-70b nhanh hơn GPT-4o ~2×, free tier |
| Response serialization | < 1ms | Pydantic |
| **Tổng p99 (LLM path)** | **< 5s** | Groq nhanh hơn OpenAI đáng kể |
| **Tổng p99 (graph-only)** | **< 20ms** | Khi AIOPS_USE_LLM=false |

LLM call vẫn là bottleneck (~95% latency), nhưng Groq với llama-3.3-70b cho latency thấp hơn GPT-4o khoảng 2–3×. TTLCache (1h) xử lý repeat requests không tốn thêm latency.

## Production Concern: Fault Tolerance

**Vấn đề**: Groq API có thể timeout hoặc trả lỗi trong giờ cao điểm. Nếu pipeline crash → SRE không có incident report khi đang xử lý outage thật.

**Giải pháp implemented — graceful degradation**:

```
call_groq_rca()
  ├── Success          → full result (root_cause + class + reasoning + actions)
  ├── Timeout / Error  → except block trong run_rca()
  └── Fallback         → _graph_only_result() → vẫn trả 200 với graph+retrieval
```

Ngoài ra:
- `AIOPS_USE_LLM=false` + restart → bypass hoàn toàn LLM, chạy graph-only trong <1s
- `TTLCache(ttl=3600)` — cùng cluster fingerprint trong 1h → không gọi Groq lần 2
- Timeout 30s trong `urllib.urlopen` — không để request hang vô thời hạn

**Validation output**: `validate_output()` kiểm tra root_cause có trong cluster services, confidence trong [0,1], class hợp lệ — nếu LLM trả sai format thì fallback thay vì crash.

## Framework Choice: FastAPI vs Flask vs BentoML

**Chọn FastAPI** vì:

1. **Pydantic v2 validation native**: Input schema với 7 fields (id, ts, service, metric, severity, value, threshold) — Pydantic tự trả 422 với field nào thiếu/sai type, không cần code thêm. Flask cần marshmallow hoặc manual check.

2. **OpenAPI auto-generate**: `/docs` endpoint hoạt động ngay, SRE có thể test POST /incident trực tiếp trên browser mà không cần curl.

3. **Type hints + response_model**: `response_model=IncidentResponse` đảm bảo response luôn đúng schema — catch serialization bug tại runtime thay vì production.

**BentoML rejected**: Pipeline không phải single ML model — là graph algorithm + LLM call. BentoML's model versioning và runner abstraction không map cleanly, thêm learning curve không cần thiết.

**Flask rejected**: Sync-only, không có native validation, phải thêm marshmallow + manual OpenAPI.

## Concrete Decisions

- **gap_sec=120**: toàn bộ incident trong dataset span ~6 phút với burst liên tục, 120s đủ để gom. Tăng lên 300s sẽ merge incidents khác nhau; giảm xuống 60s sẽ split cùng 1 incident.
- **max_hop=2**: bắt được cascade trực tiếp (payment → checkout → edge-lb) mà không kéo services xa hơn (catalog-svc, recommender-svc không liên quan cascade).
- **w_graph=0.6, w_time=0.4**: graph topology reliable hơn temporal signal (alert có thể bị delay), nhưng temporal giúp phân biệt khi nhiều service cùng score graph.
