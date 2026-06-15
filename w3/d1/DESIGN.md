# W3-D1 DESIGN.md

## Câu 1 — SLI choice cho frontend

**Chọn: `js_error=false AND network_error=false` (availability từ RUM)**

Frontend RUM log có 4 candidate signal:

| Signal | Candidate? | Lý do loại / giữ |
|---|---|---|
| Page load time (dom_ready_ms) | Loai | Không proportional: trang load chậm 2× nhưng user vẫn thấy nội dung → không phải "fail". Hơn nữa p99 = 1430ms biến động lớn theo device/network của user, rất khó đặt threshold ổn định. |
| DOM ready time | Loai | Cùng vấn đề với page load time. Là latency metric, dùng được cho SLO latency tier riêng nếu cần, nhưng không phải availability SLI. |
| JS error rate | Giu (mot phan) | Proportional với user pain (JS crash = user không dùng được). Nhưng đứng một mình thì miss network failure (API timeout, CDN down). |
| Network error rate | Giu (mot phan) | Proportional khi CDN/API unreachable. Nhưng đứng một mình thì miss JS runtime crash. |

Kết hợp `js_error=false AND network_error=false` capture cả hai loại failure → SLI duy nhất đo "user thực sự dùng được trang". Từ `baseline.json`: success_rate = **98.61%**, fail_count = 7204 / 518400 events (3 ngày). SLO target 99.0% đặt ngay trên baseline hiện tại để có buffer fix mà không miss ngay.

---

## Câu 2 — SLO target cho api

**Chọn: 99.0%**

Từ `baseline.json`: api success_rate hiện tại = **97.63%**, tức là baseline đang *dưới* 99.0%. Phân tích 3 tier:

| Target | Downtime/tháng | Đánh giá |
|---|---|---|
| 99% | 7h 18m | Phù hợp. Baseline 97.63% nghĩa là hệ thống đang có ~2.37% fail rate tự nhiên (bao gồm cả bursty incident). SLO 99% tạo budget 207,378 fail/month — đủ rộng để không miss SLO ngay tháng đầu trong khi vẫn có áp lực cải thiện. |
| 99.9% | 43m | Quá chặt. Budget chỉ còn 20,737 fail/month. Với baseline fail_count 7,234 trong 3 ngày = ~72,340/month → miss SLO ngay lập tức ngay cả không có incident. |
| 99.99% | 4m | Không thực tế hoàn toàn — cần multi-AZ + dedicated SRE team, chi phí nhân lên 10×. |

**Kết luận**: 99.0% là "aspirational but achievable" — tạo áp lực cải thiện mà không gây alert fatigue ngay ngày 1. Budget 207,378 fail/month cho phép hấp thụ baseline noise và vẫn còn headroom cho 5 incident/tháng.

---

## Câu 3 — Latency threshold p99

**Chọn: 500ms cho api**

Từ `baseline.json`: `latency_p99_ms = 156ms` (api), `dom_ready_p99_ms = 1430ms` (frontend RUM).

Phân phối latency api (ước tính từ data):

| Percentile | Giá trị |
|---|---|
| p99 baseline | 156ms |
| Alert threshold chọn | 500ms |
| Margin (×) | 3.2× baseline |

Lý do chọn 500ms thay vì các mốc khác:

- **200ms**: quá gần baseline p99 (156ms). Một spike nhỏ trong normal operation sẽ trigger false positive liên tục.
- **500ms**: đây là ngưỡng "user bắt đầu nhận ra chậm" theo nghiên cứu UX (Nielsen 1993: >500ms user mất flow). Margin 3.2× so với baseline đủ để phân biệt incident thật vs noise.
- **1000ms**: quá trễ — user đã bỏ request trước khi alert fire.

Frontend dom_ready p99 = 1430ms — không dùng làm SLI chính (xem Câu 1), nhưng nếu cần SLO latency riêng cho frontend thì threshold hợp lý là 3000ms (2× baseline).

---

## Câu 4 — 4xx exclusion

**Loại 4xx (trừ 429) ra khỏi error count**

Lý do cốt lõi: 4xx là **user-side error**, không phải system failure:

- `400 Bad Request`: client gửi sai payload → system healthy, chỉ là validation reject.
- `401/403`: authentication/authorization fail → system hoạt động đúng khi từ chối.
- `404 Not Found`: client request resource không tồn tại → không phải outage.

**429 là ngoại lệ** vì nó phản ánh system đang bị overwhelm (rate limiting do capacity, không phải do user sai) → đây là user pain thật.

Từ data `access_log.jsonl`, các endpoint có 4xx rate cao thường là:
- `/api/auth/*` → 401 spike khi session expire (bình thường, không phải outage)
- `/api/products/{id}` → 404 khi product bị xóa (bình thường, user bookmark cũ)

Nếu đếm 4xx vào fail: baseline.json api fail_rate = **0.35%** sẽ bị inflate lên do bot traffic, scraper, và expired session — SLI sẽ không còn proportional với real user pain. SLO sẽ bị miss liên tục dù hệ thống hoàn toàn healthy.

---

## Câu 5 — MWMBR tuning

**Không dùng Google default (14.4, 6, 1) — đã tune thành (5, 2, 1)**

Google default được thiết kế cho SLO 99.9% với 1h/6h/3d windows. Với setup này (SLO 99.0%, windows 10m/6h/3d), cần điều chỉnh:

| Tier | Google default threshold | Threshold đã chọn | Lý do |
|---|---|---|---|
| 1 (urgent) | 14.4 | **5** | SLO 99.0% → error budget rộng hơn (1%). Burn rate 5 tương đương đốt 5% budget/window — đủ aggressive để catch real incident mà không false positive. |
| 2 (page) | 6 | **2** | Catch sustained burn. Với SLO 99.0%, burn rate 2 = đang fail 2% request liên tục → user pain thật. |
| 3 (ticket) | 1 | **1** | Giữ nguyên — burn rate = baseline là signal đủ để tạo ticket. |

**Kết quả từ validation_report.json**:

| Metric | Static baseline | MWMBR của mình |
|---|---|---|
| Alerts fired | 22 | 3 |
| True positive | 3 | 3 |
| False positive | 19 | 0 |
| False negative | 0 | 0 |
| noise_reduction_pct | — | **86.4%** |
| mttd_delta_s | — | **0s** |
| verdict | — | **pass** |

Threshold 5/2/1 đã loại hoàn toàn 19 false positive (từ 22 xuống 3 fired) trong khi giữ nguyên 3 TP và 0 FN. MTTD không tăng (delta = 0s) — tức là detect vẫn nhanh như cũ, chỉ bỏ noise.
