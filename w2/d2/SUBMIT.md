# SUBMIT.md — W2-D2 RCA: Graph, Causal & LLM-Augmented

## Câu 1: Confidence và threshold auto-rollback

Confidence của top-1 trong cluster lớn nhất (`c-000-000`) là **0.8166** theo graph+temporal combined score cho `payment-svc`. Trong `rca_output.json` field `confidence` được set bằng `ranked[0][1]` — giá trị round của combined score đó.

Nếu phải set threshold để auto-rollback mà không cần SRE xác nhận, tôi chọn **0.85**. Lý do: rollback là write action có tác động production trực tiếp. Threshold 0.85 cover được incident này vì payment-svc là terminal node duy nhất trong alerting subgraph (out_degree=0) với timestamp sớm nhất — hai tín hiệu độc lập cùng trỏ vào nó, score cách biệt rõ so với #2 checkout-svc (0.5279). Không đặt cao hơn 0.90 vì service map thiếu edge sẽ làm score bị kéo xuống dù top-1 vẫn rõ ràng; không đặt thấp hơn 0.80 vì sẽ trigger auto-rollback trên cluster mà top-1 và top-2 sát nhau, tăng false positive.

---

## Câu 2: Variant classifier đã chọn — A (rule-based / kNN top-1)

Tôi dùng **variant A**: kNN top-1 từ keyword retrieval — lấy `root_cause_class` và `actions` verbatim từ incident có similarity score cao nhất trong `incidents_history.json`.

**Chạy thực tế ra sao (từ Step 6 notebook output):**

- `c-000-000` (payment-svc): kNN trả INC-2025-11-08 làm top-1 (score 1.00, cùng root_cause_service + severity critical), class=`connection_pool_exhaustion`. Đúng.
- `c-000-001` (cart-svc): kNN top-1 là INC-2025-07-19 (`eviction`) — nhưng `eviction` không trong VALID_CLASSES enum, fallback sang `other`. kNN bị giới hạn bởi enum không cover hết class trong history.
- `c-000-002` (notification-svc): top-1 INC-2026-02-08 (`downstream_provider`), không trong enum, fallback `other`. Hợp lý: Kafka queue backlog không khớp sạch với class cố định.
- `c-000-003` (recommender-svc): top-1 INC-2025-08-02 (`memory_leak`), trong enum, class gán được.
- `c-000-004` (search-svc): top-1 INC-2026-01-29 (`n_plus_1`), không trong enum, fallback `other`. Signal `catalog_db_query_time_ms|warn` rõ ràng là slow query nhưng kNN bỏ qua vì chỉ nhìn vào class label của incident lịch sử.

**Trade-off vs variant C (LLM — đã chạy Bonus 3 với Groq llama-3.3-70b):**

| Cluster | kNN top-1 | Groq LLM | Match |
|---------|-----------|----------|-------|
| c-000-000 | connection_pool_exhaustion | connection_pool_exhaustion | same |
| c-000-001 | other | memory_leak | differ |
| c-000-002 | other | other | same |
| c-000-003 | memory_leak | memory_leak | same |
| c-000-004 | other | slow_query | differ |

LLM vượt trội ở c-000-001 và c-000-004 vì nó suy luận từ metric name trực tiếp (`catalog_db_query_time_ms` thành `slow_query`) thay vì bị giới hạn bởi enum. Trade-off: kNN deterministic, free, không cần API key, dễ audit; LLM xử lý edge case tốt hơn nhưng thêm latency (~1-2s) và cần validate hallucination guard (root_cause phải trong cluster.services).

---

## Câu 3: Industry landscape — pipeline gần product nào nhất

Pipeline tôi xây gần nhất với **Dynatrace Davis**: giả định service graph tin được (tương đương Smartscape = `services.json`), dùng topology + temporal signal để rank candidate deterministic, trả kết quả dưới 1 giây.

**Trong domain GeekShop (e-commerce, alert volume cao, service map ổn định ~10 service), lựa chọn hợp lý vì:**

Service graph GeekShop ít thay đổi — 10 service với critical path ổn định `edge-lb → checkout-svc → payment-svc`. Pattern cascade này có thể dự đoán, graph traversal xử lý chuẩn xác. Tốc độ quan trọng trong incident; graph-only cho kết quả dưới 1 giây so với full agentic loop có thể mất 10-30 giây.

**Khi nào nên đổi:** Nếu GeekShop chuyển sang serverless hoặc event-driven nặng (Lambda ephemeral, Kafka-heavy), service graph sẽ không đáng tin. Khi đó **Causely** (causal AI học edge từ time-series, không giả định topology) phù hợp hơn. Nếu scale lên 50+ service với nhiều vendor alerts, **BigPanda** (ML cluster + multi-vendor ingestion, agnostic topology) xử lý noise reduction tốt hơn — đánh đổi bằng mất causal direction. Với GeekShop hiện tại, graph-first là lựa chọn đúng.
