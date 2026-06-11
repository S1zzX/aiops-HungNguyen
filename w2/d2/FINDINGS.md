# FINDINGS.md — W2-D2 RCA Analysis

## Cluster c-000-000 — Root Cause Analysis (Cluster chính)

Cluster lớn nhất: **15 alerts**, 3 services (`payment-svc`, `checkout-svc`, `edge-lb`), window `09:42:01Z → 09:48:30Z`, max severity `crit`.

**Root cause xác định: `payment-svc`**, class `connection_pool_exhaustion`, confidence **0.8166** (graph+temporal combined).

### Tại sao payment-svc là culprit?

Hai tín hiệu độc lập cùng trỏ về payment-svc:

**Tín hiệu 1 — Vị trí graph:** Trong alerting subgraph gồm 3 service, `payment-svc` có `out_degree=0` — nó không gọi bất kỳ service nào khác đang alert. Đây là terminal node. `checkout-svc` gọi vào `payment-svc` (1 hop), `edge-lb` gọi vào `checkout-svc` (2 hop). PageRank trên reversed subgraph cho `payment-svc` score cao nhất vì nó là điểm tích lũy flow từ cả 2 upstream caller. Terminal score = `1/(1+0) = 1.0`, PageRank normalized = 1.0, graph combined score = 1.0.

**Tín hiệu 2 — Temporal:** `payment-svc` fire alert đầu tiên lúc `09:42:01Z`. `checkout-svc` theo sau lúc `09:42:45Z` (44s sau). `edge-lb` còn muộn hơn. Temporal score: payment-svc = 1.0 (earliest), checkout-svc = 0.13, edge-lb = 0.0 (latest). Cascade direction rõ ràng: lỗi bắt đầu từ payment-svc rồi lan ngược call graph.

Final combined score (60% graph + 40% temporal):
- `payment-svc`: 0.6×1.0 + 0.4×1.0 = 1.0 → normalize → **0.8166**
- `checkout-svc`: 0.6×0.5 + 0.4×0.13 = 0.352 → **0.5279**
- `edge-lb`: 0.6×0.25 + 0.4×0.0 = 0.15 → **0.4500**

### Incident retrieval

Top-1 similar: **INC-2025-11-08** (similarity score 1.00) — `connection_pool_exhaustion` trên `payment-svc v3.2`, pool 50/50 bị đầy trong 5 phút, cascade xuống `checkout-svc`, notification queue backed up. Đây gần như là replay chính xác của scenario hiện tại — cùng service, cùng class, cùng cascade pattern.

Top-2: **INC-2026-03-20** (score 1.00) — DDoS trên edge-lb. Score cao vì cùng services_involved (edge-lb + checkout + payment) nhưng root cause khác hoàn toàn. Đây là false positive của keyword retrieval — service overlap cao không đồng nghĩa cùng root cause class.

Top-3: **INC-2025-09-05** (score 0.80) — cùng `connection_pool_exhaustion` trên payment-svc (version v2.6). Cùng pattern, version khác.

Remediation từ INC-2025-11-08: rollback về v3.1, scale pool 50→100, thêm alert monitor pool khi vượt 80%.

### Confidence — có dám auto-remediate không?

Confidence 0.8166 từ graph+temporal, cách biệt rõ so với #2 (0.5279). Tôi set threshold auto-rollback tại **0.85** — incident này nằm dưới ngưỡng một chút (0.8166 < 0.85) nên sẽ không auto-rollback, cần SRE confirm. Đây là hành vi đúng: confidence 0.82 với #2 ở 0.53 là gap đủ rõ để gợi ý mạnh, nhưng rollback production cần một người đọc reasoning trước khi bấm nút.

---

## Các cluster nhỏ — phân tích ngắn

**c-000-001 (cart-svc, 1 alert, warn):** Single-service cluster — graph score = 1.0 (không cạnh tranh). kNN top-1 là INC-2025-07-19 (`cart-redis eviction`), nhưng class `eviction` không trong VALID_CLASSES enum, fallback `other`. Đây là blind spot: lỗi tầng store (cart-redis) được map lên service app phía trên (cart-svc). Nếu muốn phân biệt, cần include store node vào alerting subgraph và đánh dấu type.

**c-000-002 (notification-svc, 2 alerts, crit):** queue_depth + queue_lag_ms crit. Single-service cluster. kNN top-1 INC-2026-02-08 (`downstream_provider`), không trong enum, fallback `other`. LLM bonus cũng trả `other`. Pattern này (Kafka queue backed up) thường là hệ quả của downstream slowdown, không phải origin. Nếu checkout-svc được gom vào cluster này thì notification-svc sẽ là victim rõ ràng hơn.

**c-000-003 (recommender-svc, 1 alert, warn):** `cpu_utilization|warn` — kNN top-1 INC-2025-08-02 (`memory_leak`). Class `memory_leak` trong enum, gán được. Nhưng chỉ 1 warn alert — confidence thấp về semantic. LLM cũng trả `memory_leak`. Alert note ghi "concurrent batch retrain" → khả năng cao là false alarm (CPU spike bình thường khi retrain).

**c-000-004 (search-svc, 1 alert, warn):** `catalog_db_query_time_ms|warn` — kNN trả `other` vì INC-2026-01-29 (`n_plus_1`) không trong enum. LLM đúng hơn khi trả `slow_query` vì đọc được tên metric. Đây là case LLM vượt kNN rõ nhất.

---

## Bonus Paths — Kết quả và So sánh

Đã implement đủ cả 3 bonus trong `assignment_with_bonus.ipynb`.

### Bonus 1 — Decision Tree Classifier

Train `DecisionTreeClassifier(max_depth=4)` trên 29 incidents. Feature vector: one-hot 14 nodes (10 service + 4 store), `severity_encoded` (low=0 đến critical=3), `n_services`, `time_burst` (1 nếu n_services >= 3). 17 features tổng.

**Kết quả thực tế:**
- Train accuracy: **41.38%** (overfit)
- Leave-One-Out CV accuracy: **6.90%** — gần random
- Prediction cho c-000-000: DT dự đoán `ddos` — sai (đúng là `connection_pool_exhaustion`)

**Tại sao DT thất bại:** 29 samples với 25 unique class — trung bình 1.16 sample/class. DT không thể học decision boundary có ý nghĩa. kNN top-1 thắng hoàn toàn ở scale này vì chỉ cần structural similarity (service overlap + severity) mà không cần học. DT sẽ cạnh tranh khi history đạt ~300+ incidents với mỗi class có >= 10 examples.

### Bonus 2 — TF-IDF Embedding + Cosine Similarity

`TfidfVectorizer(ngram_range=(1,2), max_features=300, sublinear_tf=True)` fit trên corpus `summary + remediation + root_cause_class + root_cause_service + services_involved`. Query doc build từ fingerprints + alert metrics của cluster.

**Kết quả thực tế cho c-000-000:**

| Rank | TF-IDF (cosine) | Keyword heuristic |
|------|-----------------|-------------------|
| #1 | INC-2025-09-05 (0.3393) | INC-2025-11-08 (1.00) |
| #2 | INC-2025-11-08 (0.2793) | INC-2026-03-20 (1.00) |
| #3 | INC-2026-03-20 (0.2774) | INC-2025-09-05 (0.80) |

Cả hai method đưa ra cùng 2 `connection_pool_exhaustion` incidents trong top-3, chỉ khác thứ tự. TF-IDF xếp INC-2025-09-05 lên #1 thay vì INC-2025-11-08 — cả hai đều đúng về class. Keyword heuristic xếp INC-2026-03-20 (DDoS) lên #2 vì service overlap cao, TF-IDF đặt nó ở #3 — hợp lý hơn vì TF-IDF đọc được term `connection pool` và `db pool` từ query doc. TF-IDF ưu điểm: không hand-tune weights, bigrams bắt được multi-word term. Nhược điểm: cosine score thấp (0.33 max) cho thấy vocabulary mismatch — sẽ cải thiện khi corpus lớn hơn.

### Bonus 3 — LLM Enrichment (Groq llama-3.3-70b)

Pattern Augmented LLM (single call với retrieval context đã chuẩn bị): graph top-3 candidates + fingerprints + top-3 similar incidents → LLM trả JSON với `root_cause`, `class`, `confidence`, `reasoning`, `actions`. temperature=0, response_format json_object. Validate hallucination guard trước khi dùng output.

**Kết quả so sánh kNN vs LLM (5 cluster):**

| Cluster | kNN class | LLM class | Match |
|---------|-----------|-----------|-------|
| c-000-000 | connection_pool_exhaustion | connection_pool_exhaustion | same |
| c-000-001 | other | memory_leak | differ |
| c-000-002 | other | other | same |
| c-000-003 | memory_leak | memory_leak | same |
| c-000-004 | other | slow_query | differ |

LLM cải thiện 2 cluster nơi kNN fallback sang `other` vì class không trong enum. LLM confidence thấp hơn (0.80-0.85 vs kNN 1.0) vì tự đánh giá uncertainty thay vì copy từ graph score. Reasoning LLM tự nhiên hơn và tham chiếu nhiều signal cụ thể.

**Lý do chọn Augmented LLM thay vì full agentic loop:** trong incident response, latency là critical. Single-call với retrieval context đã chuẩn bị cho kết quả trong ~1-2 giây. Full agentic loop (multi-step tool use, self-reflection) mất 10-30 giây — không chấp nhận được khi có revenue impact.

**Nếu không chọn bonus:** retrieval-only (kNN) đã đủ cho GeekShop vì 30 incidents với clear structural overlap. Khi pattern lặp lại gần như hoàn toàn (INC-2025-11-08 xấp xỉ incident hiện tại), top-1 match đã cho đủ class và remediation. LLM chỉ thực sự cần thiết khi incident là novel, không có precedent gần trong history.
