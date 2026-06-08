# SUBMIT.md — W2-D1 Alert Correlation

## Phần 1: Design Choices

### Bạn chọn `gap_sec` bao nhiêu, vì sao?

Chọn `gap_sec = 120` (2 phút).

Nhìn vào dataset thực tế, toàn bộ 20 alert trải dài từ `09:42:01` đến `09:48:30` — span ~6 phút 30 giây, với khoảng cách giữa 2 alert liền nhau tối đa chỉ ~60 giây. Chọn `gap_sec=120` đảm bảo toàn bộ burst nằm trong 1 session, không bị cắt giữa chừng.

**Trade-off:** Nếu `gap_sec` quá ngắn (30s) thì incident kéo dài 6 phút bị tách thành nhiều session nhỏ, mỗi session mất context. Nếu quá dài (600s) thì 2 incident riêng biệt trong vòng 10 phút có thể bị gộp nhầm thành 1 cluster, khiến RCA sai root cause.

---

### Bạn chọn `max_hop` bao nhiêu, vì sao?

Chọn `max_hop = 2`.

Chuỗi cascade chính trong dataset là `payment-svc → checkout-svc → edge-lb`, mỗi bước 1 hop. `max_hop=2` gom được đúng chuỗi này. `cart-svc` và `notification-svc` cũng trong 1-2 hop từ checkout — hợp lý vì chúng bị ảnh hưởng gián tiếp từ payment failure.

**Trade-off:** `max_hop=1` sẽ tách cascade thành mảnh nhỏ, mất toàn bộ context incident. `max_hop=3+` kéo `recommender-svc` (4 hop từ payment-svc) vào cluster chính dù alert của nó là batch retrain độc lập.

---

### 1 alert ID bị "miss" — tại sao?

Trong dataset này không có alert orphan thực sự vì tất cả 20 alert xảy ra trong cùng 1 session và mọi service đều connected qua graph. Tuy nhiên về mặt semantic, `a-0013` (`recommender-svc | cpu_utilization | warn`) đáng lẽ phải bị tách riêng vì label ghi rõ `"note": "unrelated — concurrent batch retrain"` — đây là ML job định kỳ, không phải hệ quả của payment failure. Nó bị gom nhầm vào cluster chính do Union-Find chain qua `edge-lb → catalog-svc → recommender-svc` (mỗi bước ≤ 2 hop).

---

### Nếu có 10.000 alert thay vì 20, code sẽ chậm ở đâu?

Điểm chậm nhất là `topology_group()` — vòng double-loop O(n²) qua các service có alert, mỗi cặp gọi `nx.shortest_path_length()` chạy BFS. Với hàng trăm service distinct, số lần gọi BFS có thể lên hàng chục nghìn lần mỗi session. **Cách khắc phục:** pre-compute toàn bộ ma trận khoảng cách 1 lần lúc khởi động bằng `nx.all_pairs_shortest_path_length()`, cache vào dict, sau đó lookup O(1).

---

## Phần 2: EOD Checkpoint

### Câu 1 — Vì sao fingerprint không include `timestamp` hay `value`?

Fingerprint dùng để nhận dạng "đây cùng 1 loại alert" — tức 2 alert là duplicate của nhau. `timestamp` và `value` thay đổi mỗi lần alert fire (cùng 1 vấn đề nhưng fire lúc 09:42 và 09:44 sẽ có timestamp khác nhau, value khác nhau). Nếu include chúng vào fingerprint thì **không có 2 alert nào giống nhau** — dedup hoàn toàn vô dụng, mỗi alert trở thành 1 cluster riêng biệt.

Ví dụ cụ thể: `a-0003`, `a-0008`, `a-0015` đều là `payment-svc | latency_p99_ms | crit` với value=1840 nhưng timestamp khác nhau. Nếu include timestamp → 3 fingerprint khác nhau → 3 cluster riêng → miss hoàn toàn việc đây là cùng 1 vấn đề fire lặp lại 3 lần.

---

### Câu 2 — Sự khác biệt giữa "duplicate" và "correlated" alert?

**Duplicate** = cùng 1 alert fire nhiều lần: cùng service, cùng metric, cùng severity, chỉ khác timestamp/value. Ví dụ từ dataset: `a-0003`, `a-0008`, `a-0015` — đều là `payment-svc | latency_p99_ms | crit`, fire lại 3 lần trong 4 phút vì vấn đề chưa được fix.

**Correlated** = các alert khác nhau (khác service hoặc khác metric) nhưng có cùng root cause. Ví dụ: `a-0003` (`payment-svc | latency_p99_ms`) và `a-0006` (`checkout-svc | downstream_payment_error_rate`) — 2 fingerprint hoàn toàn khác nhau, nhưng cùng nguyên nhân gốc là DB connection pool của payment-svc bị cạn. Dedup không gom được chúng — phải dùng time-window + topology mới phát hiện ra.

---

### Câu 3 — `gap_sec=30` vs `gap_sec=600` ảnh hưởng output thế nào?

`gap_sec=30`: Incident 6 phút bị tách thành ~8 session nhỏ → output 8+ cluster thay vì 1 → RCA phải xử lý 8 thứ thay vì 1, mất context toàn bộ cascade chain.

`gap_sec=600`: Toàn bộ 20 alert vẫn trong 1 session (đúng), nhưng nếu có 2 incident riêng biệt trong cùng 10 phút thì bị gộp nhầm thành 1 cluster → false correlation, RCA tìm root cause sai.

---

### Câu 4 — Correlator có gom `recommender-svc` vào cluster chính không? Vì sao?

**Có** — correlator gom `recommender-svc` (alert `a-0013`) vào cluster chính `c-000-000` cùng với payment-svc, checkout-svc, edge-lb.

Lý do: Union-Find dùng `max_hop=2` trên undirected graph. `recommender-svc` cách `edge-lb` đúng 2 hop (`edge-lb → catalog-svc → recommender-svc`), và `edge-lb` đã được union với cluster chính. Khi Union-Find chạy, `recommender-svc` được union với `edge-lb` (dist=2 ≤ max_hop=2) → tự động kéo vào cùng component.

Đây là **false positive** của topology grouping. Alert `a-0013` có label `"note": "unrelated — concurrent batch retrain"` — nó là ML job định kỳ, không phải hệ quả của payment pool exhaustion. Topology grouping không phân biệt được "connected trên graph" với "thực sự bị ảnh hưởng bởi cùng incident". RCA layer (D2) sẽ cần loại nó ra bằng causal scoring.

---

### Câu 5 — Limitation lớn nhất của topology grouping?

Topology grouping chỉ biết "2 service có đường đi trên graph" nhưng **không biết chiều ảnh hưởng và thời điểm**. Nó gom `recommender-svc` vào cluster payment failure chỉ vì chúng connected qua `catalog-svc`, dù thực tế recommender không bị ảnh hưởng gì bởi payment.

**Cách khắc phục:** Thêm **directed propagation** — chỉ union 2 service nếu có path upstream (từ caller về callee), vì cascade thường đi từ downstream lên upstream (payment hỏng → checkout bị ảnh hưởng, không phải ngược lại). Kết hợp với anomaly timestamp: nếu service A alert trước service B quá lâu (> 5 phút) dù connected, không union — vì không phải cùng incident.

---

*Pipeline: Dedup (fingerprint) → Session Window (gap_sec=120) → Topology-Aware Union-Find (max_hop=2)*
*Result: 20 alerts → 1 cluster, reduction_ratio = 0.95*
