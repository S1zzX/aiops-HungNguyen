# SUBMIT.md — W2-D1 Alert Correlation

## Phần 1: Design Choices

### Bạn chọn `gap_sec` bao nhiêu, vì sao?

Chọn `gap_sec = 120` (2 phút).

Nhìn vào dataset thực tế, toàn bộ 20 alert trải dài từ `09:42:01` đến `09:48:30` — span ~6 phút 30 giây, với khoảng cách giữa 2 alert liền nhau tối đa chỉ ~60 giây. Chọn `gap_sec=120` đảm bảo toàn bộ burst nằm trong 1 session duy nhất, không bị cắt giữa chừng.

**Trade-off:** Nếu `gap_sec` quá ngắn (30s) thì incident kéo dài 6 phút bị tách thành nhiều session nhỏ, mỗi session mất context cascade chain. Nếu quá dài (600s) thì 2 incident riêng biệt trong cùng 10 phút có thể bị gộp nhầm thành 1 cluster, khiến RCA sai root cause.

---

### Bạn chọn `max_hop` bao nhiêu, vì sao?

Chọn `max_hop = 2`.

Code dùng **directed upstream traversal** (BFS trên reversed graph) thay vì undirected Union-Find. Từ root `payment-svc`, BFS ngược tìm ai là upstream caller: `checkout-svc` (1 hop), `edge-lb` (2 hop). `max_hop=2` gom đúng chuỗi cascade chính `payment-svc → checkout-svc → edge-lb` mà không kéo những service không liên quan vào.

**Trade-off:** `max_hop=1` chỉ gom payment + checkout, bỏ sót `edge-lb` dù nó bị ảnh hưởng trực tiếp. `max_hop=3+` có thể kéo thêm service ngoài cascade chain thật, làm cluster chính bị nhiễu.

---

### 1 alert ID bị "miss" (không match cluster cascade chính) — tại sao?

Với directed upstream traversal, `a-0013` (`recommender-svc | cpu_utilization | warn`) **không** bị kéo vào cluster chính `c-000-000`. Lý do: BFS ngược từ `payment-svc` chỉ đi theo chiều "ai gọi payment-svc?" — tức `checkout-svc` và `edge-lb`. `recommender-svc` không phải upstream caller của `payment-svc` nên nằm ngoài reach.

Kết quả: `a-0013` ra cluster riêng `c-000-003` (size=1) — đây là hành vi đúng vì alert này là batch retrain ML job định kỳ, không phải hệ quả của payment pool exhaustion. Directed traversal tránh được false positive mà undirected Union-Find mắc phải.

---

### Nếu có 10.000 alert thay vì 20, code sẽ chậm ở đâu?

Điểm chậm nhất là `topology_group()` — hàm gọi `nx.single_source_shortest_path_length()` (BFS) mỗi lần xử lý 1 session. Với session có hàng trăm service distinct và graph lớn, BFS chạy lại từ đầu mỗi session. **Cách khắc phục:** pre-compute toàn bộ reachability từ mọi node 1 lần lúc khởi động bằng `nx.all_pairs_shortest_path_length()`, cache vào dict, sau đó lookup O(1) trong pipeline chính.

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

`gap_sec=30`: Incident 6 phút bị tách thành ~8 session nhỏ → output 8+ cluster thay vì 5 → RCA mất context toàn bộ cascade chain, mỗi mảnh chỉ thấy 1 phần câu chuyện.

`gap_sec=600`: Toàn bộ 20 alert vẫn trong 1 session (đúng), nhưng nếu có 2 incident riêng biệt trong cùng 10 phút thì bị gộp nhầm vào cùng session → false correlation, RCA tìm root cause sai.

---

### Câu 4 — Correlator có gom `recommender-svc` vào cluster chính không? Vì sao?

**Không** — với implementation hiện tại dùng directed upstream BFS, `recommender-svc` (alert `a-0013`) **không** nằm trong cluster chính `c-000-000`. Nó ra cluster riêng `c-000-003`.

Lý do: `topology_group()` tìm root là `payment-svc` (service có crit alert sớm nhất), sau đó BFS trên reversed graph để tìm upstream callers. `recommender-svc` không gọi vào `payment-svc` theo bất kỳ path nào — nó chỉ được gọi bởi `catalog-svc` (downstream). BFS ngược từ `payment-svc` không reach được `recommender-svc` trong `max_hop=2`.

Đây là điểm mạnh của directed traversal so với undirected Union-Find: tránh false positive từ service chỉ "gần nhau trên graph" nhưng không thực sự trong cùng cascade chain.

---

### Câu 5 — Limitation lớn nhất của topology grouping?

Topology grouping dựa trên cấu trúc graph tĩnh — nó không biết **runtime behavior**. Ví dụ: `cart-svc` (cluster `c-000-001`) và `notification-svc` (cluster `c-000-002`) đều là downstream của `checkout-svc` và alert trong cùng session, nhưng correlator tách chúng ra riêng vì BFS ngược từ `payment-svc` không reach chúng (chúng không phải upstream caller của payment-svc, chúng là downstream). Về mặt kỹ thuật đây là đúng với directed logic — nhưng nếu trong thực tế `cart-svc` bị ảnh hưởng gián tiếp qua `checkout-svc`, ta lại mất correlation đó.

**Cách khắc phục:** Thêm **bidirectional cascade scoring** — ngoài upstream BFS, thêm 1 pass downstream BFS từ root với threshold thấp hơn (max_hop=1), gom những service bị root gọi trực tiếp. Kết hợp với anomaly timestamp alignment: chỉ union nếu alert của service B bắt đầu sau alert của root trong vòng N giây (tức có temporal causality).

---

*Pipeline: Dedup (fingerprint) → Session Window (gap_sec=120) → Directed Upstream Topology (max_hop=2, BFS on reversed graph)*
*Result: 20 alerts → **5 clusters**, reduction_ratio = **0.75** (≥ 0.5 ✅)*
*Clusters: c-000-000 (15 alerts, payment/checkout/edge-lb cascade), c-000-001 (cart-svc), c-000-002 (notification-svc), c-000-003 (recommender-svc), c-000-004 (search-svc)*
