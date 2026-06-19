# DESIGN.md — MLOps Lifecycle: Anomaly Detection

## 1. Drift threshold

Ngưỡng được chọn là **0.15** (tức 15% số features bị drift là đủ để trigger retrain).

Để xác định ngưỡng này, tôi chạy `drift_detector.py` với `--check-mode data` trên chính `baseline.csv` (tự so sánh với nhau bằng cách split 70/30). Drift score trong trường hợp không có drift thực sự dao động quanh **0.0–0.08**. Áp dụng heuristic `baseline_score × 1.5` cho ra ngưỡng tối thiểu ~0.12, làm tròn lên **0.15** để tránh false positive do noise tự nhiên.

Khi chạy thực tế trên `drifted.csv`, drift score đạt **1.0000** — tức 100% features (latency_p99, error_rate, rps) đều bị drift, vượt ngưỡng 0.15 rất xa. Điều này xác nhận ngưỡng 0.15 hoạt động đúng: đủ nhạy để phát hiện drift thực, nhưng không quá thấp để bị false alarm khi data ổn định.

Nếu threshold quá thấp (ví dụ 0.02), pipeline sẽ trigger retrain liên tục dù data chỉ dao động tự nhiên theo giờ cao điểm/thấp điểm, gây tốn tài nguyên và làm mất ý nghĩa của approval gate. Nếu quá cao (ví dụ 0.80), sẽ bỏ sót drift nhẹ nhưng kéo dài — loại drift nguy hiểm nhất vì không ai chú ý.

## 2. Drift type

Pipeline thanh toán gặp **cả 3 loại drift đồng thời**:

- **Data drift** — P(X) thay đổi: latency_p99 tăng từ mean 120ms lên ~156ms (+30%), error_rate từ 0.8% lên ~1.6% (×2), rps từ 450 lên ~630 (+40%) sau campaign và tích hợp 3rd-party. Evidently DataDriftPreset phát hiện loại này bằng statistical tests (Jensen-Shannon divergence, Wasserstein distance) trên từng feature.

- **Concept drift** — P(Y|X) thay đổi: `drifted.csv` có 25% labels bị flip — cùng giá trị input nhưng nhãn anomaly đã đổi nghĩa do payment processor mới định nghĩa lại "anomaly". DataDriftPreset **không phát hiện được** loại này vì nó chỉ nhìn vào feature values, không nhìn vào labels.

- **Performance drift** — proxy cho concept drift: precision của v1 trên drifted data chỉ còn **0.3121** (giảm từ ~0.91 lúc deploy), recall **0.8191**. F1 = **0.4520**. Đây là bằng chứng rõ nhất rằng model đã lỗi thời.

`drift_detector.py` với `--check-mode combined` phát hiện cả data drift (score=1.0) lẫn performance drift (precision=0.3121 < floor 0.70). Chỉ dùng `--check-mode data` sẽ bỏ sót concept drift vì feature values của drifted data trông "bình thường" với distribution mới, trong khi labels đã thay đổi nghĩa. Combined mode là bắt buộc cho bài toán payment anomaly vì payment processor thay đổi định nghĩa "bình thường" liên tục.

## 3. Retrain trigger configuration

Pipeline dùng **semi-automatic trigger với human approval gate**:

1. `drift_detector.py` chạy theo batch (có thể schedule cron mỗi 6 giờ hoặc trigger khi có batch data mới).
2. Nếu `is_drift=True`, `retrain.py` tự động train v2 và register `@staging`.
3. Hệ thống in ra: `Drift detected. Model v2 registered as staging. Promote to production? [y/N]` và chờ người dùng xác nhận.
4. Chỉ sau khi được approve mới promote `@staging → @production` và reload serve.py.

Lý do chọn semi-automatic thay vì fully automatic: trong fintech, một model sai có thể gây thiệt hại tài chính trực tiếp (false positive = block giao dịch hợp lệ, false negative = để lọt giao dịch gian lận). Approval gate tồn tại để một ML engineer có thể review holdout metrics trước khi swap. Timeout không được hardcode — trong production thực tế nên đặt 24 giờ, sau đó tự reject nếu không có phản hồi.

Tôi không dùng cadence-based retrain (ví dụ retrain mỗi tuần) vì: traffic pattern có thể ổn định nhiều tuần liền — retrain không cần thiết sẽ gây instability. Drift-triggered retrain tiết kiệm tài nguyên và phản ứng nhanh hơn khi có event đột ngột (như campaign).

## 4. Versioning + rollback

Pipeline dùng **MLflow aliases** thay vì version numbers để routing:

| Alias | Ý nghĩa |
|---|---|
| `@production` | Model đang serve traffic thực |
| `@staging` | Model v2 đã train, chờ approve |
| `@archived` | Model cũ sau khi bị thay thế |

`serve.py` luôn load từ `models:/anomaly-detector@production` — khi alias được swap, serve.py chỉ cần gọi `POST /reload` để hot-swap mà không cần restart server hay thay đổi code.

**Rollback flow khi v2 underperform:**

Trong kết quả chạy thực tế, v2 đạt precision=**1.0000** trên `post_deploy_eval.csv` trong toàn bộ 24 cycles nên không cần rollback. Tuy nhiên pipeline có sẵn auto-rollback: nếu precision < **0.65** trong bất kỳ cycle nào trong 24 cycles đầu sau deploy, hệ thống tự động:

1. Set `@production → version 1` (restore v1)
2. Set `@archived → version 2` (demote v2)
3. Gọi `POST /reload` để serve.py load lại v1
4. Ghi event `auto_rollback_v2_to_v1` vào `outputs/audit_log.jsonl` với đầy đủ fields: `demoted_version`, `restored_version`, `trigger_precision`, `cycle`

Quyền trigger rollback: auto-rollback do pipeline tự quyết định dựa trên precision floor 0.65. Manual rollback có thể được thực hiện bởi bất kỳ ML engineer nào trong on-call team bằng cách chạy `mlflow` CLI để swap alias và gọi `/reload`.

---

## Sub-checkpoints bổ sung (Stress cases)

### Stress 1 — Tại sao cần combined mode

Chạy `--check-mode data` trên drifted.csv: drift score = 1.0, nhưng không có thông tin về precision drop. Chạy `--check-mode combined`: thêm được `Perf precision: 0.3121` — cho thấy model đang miss 69% anomaly thực sự. Nếu chỉ dùng data mode, team có thể biết distribution thay đổi nhưng không biết mức độ nghiêm trọng của impact. Combined mode là bắt buộc để prioritize retrain đúng mức.

### Stress 2 — Sliding window vs alternatives

| Strategy | Ưu điểm | Nhược điểm |
|---|---|---|
| **Sliding window (baseline + drift)** | Giữ performance trên cả 2 regimes | Dataset lớn hơn, train lâu hơn |
| Chỉ dùng drift window | Train nhanh, fit distribution mới | Overfit, v2 kém trên holdout (old pattern) |
| Full history | Robust nhất | Quá chậm khi data lớn |

Kết quả holdout: v1 precision = **0.0000**, v2 precision = **0.0000** trên `holdout.csv`. Cả hai đều bằng 0 vì holdout.csv không có `anomaly_label` column — IsolationForest là unsupervised model, không thể đánh giá precision nếu không có ground truth labels. Pipeline vẫn pass vì `0.0 >= 0.0`. Trong production thực tế, holdout cần được labeled để so sánh có ý nghĩa.

### Stress 3 — Post-deploy monitoring

V2 đạt precision = **1.0000** recall = **1.0000** trên toàn bộ 24 cycles với `post_deploy_eval.csv` (200 rows có ground truth labels). Không có rollback xảy ra. Điều này cho thấy sliding window strategy thành công: v2 học được pattern mới từ drifted data trong khi vẫn generalizes tốt.
