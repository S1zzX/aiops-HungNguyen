# Detection Approach — DESIGN.md

## Approach tôi dùng

**Sliding Window + Static/Relative Threshold** kết hợp với **Log Pattern Matching**.

Hai lớp detection chạy song song trên mỗi tick:
1. **Metrics detection** — dùng sliding window để tính baseline, so sánh với ngưỡng tĩnh và tỉ lệ tương đối
2. **Log detection** — phân tích log entries để bắt signal sớm hơn metrics

---

## Tại sao chọn approach này

Streaming data có đặc điểm không có "toàn bộ dataset" trước — mỗi tick chỉ có một điểm dữ liệu mới. Sliding window phù hợp vì:

- **Stateless về lịch sử dài**: chỉ cần giữ 20 điểm gần nhất trong RAM, không cần database
- **Tự thích nghi với diurnal pattern**: baseline tính từ window hiện tại nên tự động điều chỉnh theo chu kỳ ngày/đêm của traffic
- **Độ trễ thấp**: mỗi tick xử lý O(1), không block endpoint

Log detection bổ sung vì log thường xuất hiện **trước** khi metrics vượt ngưỡng — giúp giảm TTD (Time To Detect).

---

## Cách hoạt động

### Lớp 1: Metrics Detection

Mỗi tick, pipeline cập nhật sliding window (20 điểm) cho 8 metrics. Sau khi warm-up đủ 20 điểm, baseline được tính:

```
baseline = mean(window[0:17])   # bỏ 3 điểm cuối tránh fault contaminate
```

Ba loại fault được detect với hai mức severity:

**memory_leak**
```
WARNING:  mem_pct > 65%  AND  gc > 30ms
CRITICAL: mem_pct > 80%  AND  gc > 80ms

mem_pct = memory_usage_bytes / memory_limit_bytes × 100
```

**traffic_spike**
```
WARNING:  rps > baseline_rps × 2.0  AND  queue > 20
CRITICAL: rps > baseline_rps × 3.0  AND  queue > 50
```

**dependency_timeout**
```
WARNING:  upstream_timeout_rate > 5%   AND  http_5xx_rate > 2%
CRITICAL: upstream_timeout_rate > 15%  AND  http_5xx_rate > 5%
```

### Lớp 2: Log Pattern Matching

Mỗi tick, pipeline duyệt qua 0–3 log entries và match keyword:

| Log level | Keyword trong message | → Fault type | Severity |
|-----------|----------------------|-------------|----------|
| ERROR | `OutOfMemoryWarning` | memory_leak | critical |
| WARN | `GC pause exceeded` | memory_leak | warning |
| ERROR | `server overloaded` | traffic_spike | critical |
| WARN | `Queue depth high` | traffic_spike | warning |
| ERROR | `Circuit breaker OPEN` | dependency_timeout | critical |
| WARN | `Upstream timeout` | dependency_timeout | warning |

### Cooldown System

Để tránh spam alert, mỗi loại alert có cooldown riêng:
```
WARNING:  120 giây
CRITICAL:  60 giây
```

Alert chỉ được fire lại sau khi hết cooldown — thay vì chặn vĩnh viễn như `fired_alerts = set()`.

---

## Parameters tôi chọn

| Parameter | Giá trị | Lý do |
|-----------|---------|-------|
| `WINDOW_SIZE` | 20 | ~10 phút production data (30s/tick × 20 = 600s). Đủ để tính baseline ổn định nhưng không quá cũ |
| Bỏ 3 điểm cuối khi tính baseline | 3 | Tránh fault đang xảy ra làm nhiễu baseline, đồng thời không bỏ quá nhiều |
| mem WARNING threshold | 65% | Bắt sớm khi memory bắt đầu tăng bất thường, còn cách limit 35% |
| mem CRITICAL threshold | 80% | Mức nguy hiểm rõ ràng, container có thể bị OOM kill |
| RPS spike WARNING | 2x baseline | Traffic tăng gấp đôi là dấu hiệu đáng chú ý |
| RPS spike CRITICAL | 3x baseline | Traffic tăng 3x gây overload rõ ràng |
| Cooldown WARNING | 120s | Đủ lâu để tránh spam nhưng vẫn nhắc lại nếu fault kéo dài |
| Cooldown CRITICAL | 60s | Critical cần nhắc nhanh hơn warning |

---

## Kết quả thực tế

Pipeline phát hiện `memory_leak` với timeline:

```
fault_start_real_seconds = rng.uniform(30 * 60, 150 * 60)  # deterministic từ birthday

10:09:30 → WARNING  [LOG] GC pressure detected: GC pause exceeded threshold pause_ms=58
           (log detection bắt trước metrics ~20 giây)

10:09:50 → CRITICAL metrics: Memory at 80.9% of limit (1.62GB), GC pause=160ms
           (metrics detection xác nhận)
```

Log detection giúp giảm TTD xuống ~20 giây so với chỉ dùng metrics.

---

## Cải thiện nếu có thêm thời gian

**Z-score thay vì threshold tĩnh:**
```
z = (x - mean) / std
Nếu z > 3 → anomaly
```
Không cần đặt ngưỡng thủ công, tự thích nghi theo từng metric.

**EWMA (Exponentially Weighted Moving Average)** cho baseline mượt hơn, ít bị ảnh hưởng bởi spike ngắn:
```
baseline = α × current + (1 - α) × previous_baseline
```

**Multivariate detection**: hiện tại mỗi metric detect độc lập. Kết hợp nhiều metric cùng lúc (ví dụ memory + GC + latency tăng đồng thời) sẽ giảm false positive hơn.

**Persistent state**: hiện tại `last_fired` và `windows` mất khi restart server. Có thể lưu vào file để pipeline recover được sau khi crash.
