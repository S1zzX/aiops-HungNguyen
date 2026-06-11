# W2-D2 RCA Findings

## Cluster c-000-000 — Phân tích Root Cause

Pipeline phân tích 1 cluster gồm 20 alert trên 7 service: `payment-svc`, `checkout-svc`, `edge-lb`, `cart-svc`, `notification-svc`, `recommender-svc`, và `search-svc`.

**Root cause xác định: `payment-svc`** với class `connection_pool_exhaustion` và confidence **0.9955**.

### Tại sao payment-svc là culprit?

Hai tín hiệu cùng trỏ vào `payment-svc`:

1. **Vị trí trên graph**: Trong alerting subgraph, `payment-svc` có `out_degree=0` — nó không gọi bất kỳ service nào khác đang alert. Đây là terminal node. Các service phía trên (`checkout-svc`, `edge-lb`) là caller bị cascade khi `payment-svc` degraded. PageRank trên subgraph cũng cho `payment-svc` score cao nhất vì nó tích lũy flow từ tất cả upstream caller.

2. **Tín hiệu thời gian**: `payment-svc` fire alert đầu tiên lúc `09:42:01Z` — sớm hơn bất kỳ service nào trong cluster. `checkout-svc` theo sau lúc `09:42:45Z`, `edge-lb` còn muộn hơn. Cascade alert rõ ràng bắt nguồn từ `payment-svc` rồi lan ngược lên call graph.

Combined score (60% graph + 40% temporal) = **0.9955**, cách biệt rõ so với #2 là `cart-svc` ở mức 0.8696.

### Incident retrieval

Incident gần nhất: **INC-2025-11-08** — sự cố `connection_pool_exhaustion` giống hệt, do `payment-svc v3.2` leak DB connection cho đến khi pool 50/50 bị đầy, cascade xuống `checkout-svc` và `notification-svc`. Đây gần như là replay chính xác của scenario hiện tại.

Incident thứ hai: **INC-2025-09-05** — cùng root cause class trên `payment-svc`, version khác (v2.6).

Remediation từ lịch sử: rollback về v3.1, scale pool 50→100, thêm alert monitor pool khi vượt 80%.

### Confidence — có nên auto-remediate không?

Confidence là 0.9955. Tuy nhiên tôi **không** set threshold auto-rollback ở mức này. Threshold hợp lý là **0.85** vì:
- Rollback là action có tác động production, cần ít nhất một SRE xác nhận
- Threshold 0.85 đủ để bắt được incident này một cách chắc chắn
- Không nên đặt quá cao (0.95+) vì sẽ bỏ sót các case graph scoring có chút nhiễu do thiếu edge

### Một case không chắc chắn

`cart-svc` xếp hạng #2 (score 0.8696). Score cao vì nó cũng gần terminal trong alerting subgraph (chỉ gọi `cart-redis` và `catalog-svc`, cả hai đều không có trong alerting subgraph) và timestamp alert tương đối sớm. Nếu thực tế `cart-redis` mới là culprit (ví dụ eviction event như INC-2025-07-19), pipeline vẫn có thể rank `cart-svc` cao vì store node bị loại khỏi graph scoring. Đây là blind spot đã biết: lỗi ở tầng store bị quy cho service app phía trên.

---

## Bonus Paths — Kết quả và So sánh

Đã implement cả 3 bonus paths trong `assignment_with_bonus.ipynb`.

### Bonus 1 — Decision Tree Classifier

Train `DecisionTreeClassifier(max_depth=4)` trên 30 incidents với feature vector gồm one-hot của tất cả service/store nodes (14 features), `severity_encoded` (low=0 → critical=3), `n_services`, và `time_burst_pattern` (1 nếu n_services ≥ 3).

**Kết quả:**
- Train accuracy: ~41% (overfit)
- Leave-One-Out CV accuracy: ~10–15% — gần random
- Prediction cho cluster c-000-000: DT dự đoán `ddos` — **sai** (đúng là `connection_pool_exhaustion`)

**Tại sao DT thất bại:** Với 30 incidents và ~22 class duy nhất, mỗi class trung bình chỉ có 1.4 sample. DT không thể học decision boundary có ý nghĩa. sklearn cũng raise `UserWarning` xác nhận điều này ("number of unique classes is greater than 50% of samples").

**So sánh với kNN top-1:** kNN thắng hoàn toàn ở scale này vì nó chỉ cần structural similarity (service overlap + severity) mà không cần học. DT sẽ cạnh tranh được khi history đạt ~300+ incidents với mỗi class có ít nhất 10 examples.

---

### Bonus 2 — TF-IDF Embedding + Cosine Similarity

Thay keyword overlap heuristic bằng `TfidfVectorizer(ngram_range=(1,2), max_features=300, sublinear_tf=True)` fit trên corpus `summary + remediation + root_cause_class + services_involved`. Query doc build từ fingerprints + alert metrics của cluster.

**Kết quả:**

| Rank | TF-IDF | Keyword heuristic |
|------|--------|-------------------|
| #1 | INC-2025-11-08 (cosine=0.4925) ✅ | INC-2025-11-08 ✅ |
| #2 | INC-2025-09-05 (cosine=0.4061) | INC-2026-03-20 |
| #3 | INC-2026-03-20 (cosine=0.3395) | INC-2025-09-05 |

Cả hai đều đồng ý top-1 là INC-2025-11-08 (`connection_pool_exhaustion`). TF-IDF xếp INC-2025-09-05 cao hơn INC-2026-03-20 — hợp lý hơn vì INC-09-05 cùng class và cùng service, trong khi INC-03-20 là `ddos` trên `edge-lb`.

**Ưu điểm TF-IDF so với keyword:** không cần hand-tune weights (0.4/0.4/0.2), bigrams như "connection pool" được coi là 1 term, tự điều chỉnh qua IDF khi history lớn hơn. Nhược điểm: cold-start — rare class term score thấp cho đến khi có đủ examples.

---

### Bonus 3 — LLM Enrichment (Groq free tier)

Dùng pattern **Augmented LLM** từ "Building Effective Agents": single LLM call với context đã chuẩn bị sẵn (graph top-3 + fingerprints + top-3 similar incidents) → LLM trả về JSON có `root_cause`, `class`, `confidence`, `reasoning`, `actions`. Model: `llama-3.3-70b-versatile` trên Groq, `temperature=0`, `response_format: json_object`.

**Kết quả thực tế từ `rca_output_bonus.json`:**

| Field | kNN top-1 | Groq LLM |
|-------|-----------|----------|
| root_cause | payment-svc | payment-svc |
| class | connection_pool_exhaustion | connection_pool_exhaustion |
| confidence | 0.9955 | 0.85 |
| method | graph+retrieval | graph+llm |

Cả hai đồng ý root cause và class — **Class agreement: ✅ agree**.

LLM confidence thấp hơn (0.85 vs 0.9955) vì LLM tự đánh giá uncertainty thay vì lấy trực tiếp từ graph score. Actions của LLM tổng quát hơn ("rollback to a previous version") so với kNN copy verbatim từ incident history ("Rollback to v3.1. Scale pool 50→100"). Reasoning của LLM tự nhiên hơn và tham chiếu đến nhiều signal cụ thể.

**Lý do chọn Augmented LLM thay vì full agentic loop:** trong incident response, latency là critical. Single-call với retrieval context đã chuẩn bị sẵn cho kết quả trong ~1-2 giây. Full agentic loop (multi-step tool use, self-reflection) có thể mất 10-30 giây — không chấp nhận được khi có revenue impact.

**Khi nào LLM thực sự vượt kNN:** incident mới không có precedent trong history, hoặc khi cần tổng hợp pattern từ nhiều incidents thay vì copy top-1. Với GeekShop 30 incidents, kNN đủ mạnh — LLM là lớp enhancement cho edge cases và chất lượng reasoning.
