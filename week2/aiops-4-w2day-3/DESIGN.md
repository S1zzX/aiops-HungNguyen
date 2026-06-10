# DESIGN.md — AIOps Incident Pipeline (w2/d3)

## 1. Pipeline Architecture

```
POST /incident
     │
     ▼
[serve.py]  ←─── Pydantic validation (422 on bad input)
     │             Latency middleware (X-Response-Time-Ms header)
     │
     ▼
[pipeline.py]  process_batch()
     │
     ├──► L1: correlate()  [correlate.py]
     │         Sort alerts by ts → union-find grouping by (time gap + graph hop distance)
     │         Output: list of clusters
     │
     ├──► L2: rca_graph()  [rca.py]
     │         Build subgraph of alerting services → reverse → PageRank
     │         Output: ranked (service, confidence) candidates
     │
     └──► L3: call_llm_rca()  [rca.py]
               Build structured prompt with cluster + graph candidates + history
               Call gpt-4o-mini → parse JSON response
               Fallback: graph-only if LLM fails or confidence ≥ 0.9
               Output: root_cause, class, reasoning, actions, similar_incidents
```

## 2. Latency Budget Breakdown

| Phase | Estimated p99 | Notes |
|---|---|---|
| Pydantic validation | < 1ms | Pure in-memory |
| L1 Correlation (9 nodes) | < 5ms | Graph traversal is O(V+E), tiny graph |
| L2 PageRank RCA | < 5ms | NetworkX on subgraph of ≤9 nodes |
| L3 LLM call (gpt-4o-mini) | 3–8s | Dominates — IO-bound, network dependent |
| Response serialization | < 1ms | Pydantic model_dump |
| **Total p99 target** | **< 10s** | LLM path; graph-only ≈ 15ms |

The LLM call accounts for ~95% of end-to-end latency. Optimizations applied:
- **TTLCache** on LLM calls (sha256 of prompt → response, TTL=1h): repeat incidents with same fingerprint hit cache
- **Skip LLM** if graph confidence ≥ 0.9 (saves the full round-trip)
- **Feature flag** `AIOPS_USE_LLM=false` for instant fallback during provider outages
- 10s timeout + 1 retry on OpenAI client prevents indefinite hangs

## 3. Production Concern: Fault Tolerance

**Problem**: LLM provider outages are not uncommon (OpenAI has had multiple incidents in 2025–2026). If the LLM call hangs or errors, we cannot let the entire endpoint return 500 — SREs need *some* answer, even a degraded one.

**Solution implemented (graceful degradation)**:

```
LLM call
  ├── Success → full root_cause with reasoning, action classification, similar incidents
  ├── Timeout (>10s) → raises OpenAI timeout → caught in run_rca()
  └── Any exception → fallback to graph-only RCA
       └── PageRank score + history-matched actions → still returns 200
```

The endpoint never returns 500 due to LLM failure. The response has lower quality (no natural-language reasoning, generic actions) but the pipeline stays available.

**Tested**: `test_llm_failure_falls_back_gracefully` mocks `call_llm_rca` raising an exception and asserts the endpoint still returns 200 with a valid root cause.

## 4. Framework Choice: FastAPI vs Flask vs BentoML

**Chose FastAPI.** Rationale:

- **Async support**: LLM calls are IO-bound. FastAPI's async endpoint (`async def`) lets the event loop handle other requests while waiting on OpenAI — Flask's sync model would block the worker thread entirely.
- **Pydantic v2 validation built-in**: Input schema enforcement (422 on bad input) requires zero extra code. Flask needs manual validation or marshmallow.
- **OpenAPI auto-generated**: `/docs` endpoint works immediately with no extra configuration, useful for SRE teams testing the API.
- **Type hints throughout**: `response_model=IncidentResponse` ensures the response always matches schema — catch bugs at serialization time, not in production.

**BentoML rejected because**: pipeline is not a single ML model — it's graph algorithms + LLM calls. BentoML's model versioning and batching primitives don't map cleanly to this workload, and the learning curve adds overhead without commensurate benefit for ≤20 services.

## 5. Service Graph Lifecycle

Graph is loaded once at module import from `dataset/services.json` and cached in-memory. A background daemon thread reloads it every 300 seconds. Maximum staleness: 5 minutes.

Trade-off accepted: at scale (100+ services), this should migrate to an event-driven reload triggered by the service registry. At current scale (9 services), polling every 5 minutes is acceptable.

`GET /version` exposes `graph_version` (MD5 of file content), `graph_loaded_at`, and node/edge counts so operators can immediately verify which topology version is active without digging into logs.

## 6. Concurrency Model

Current deployment: single-worker (`uvicorn serve:app --port 8000`).

In-memory cache (`TTLCache`) is not shared across workers. Multi-worker deployment (`--workers 4`) would give each process its own cache — acceptable since cache is an optimization, not correctness. For correctness-critical shared state, Redis would be the right solution.

Production scale-out: `uvicorn serve:app --workers 4` for CPU-bound work parallelism. For LLM-heavy workloads, async concurrency within a single worker is often more efficient than multiple blocking workers.
