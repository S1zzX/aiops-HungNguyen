# SUBMIT.md — Reflection

## 1. Drift threshold: lựa chọn và validation

Em chọn threshold **0.15** dựa trên heuristic `baseline_score × 1.5`. Cụ thể: chạy Evidently DataDriftPreset so sánh baseline với chính nó (split 70/30), drift score thu được dao động 0.00–0.08 — đây là "noise floor" của hệ thống. Nhân 1.5 cho ra ~0.12, làm tròn lên **0.15** để có buffer an toàn. Threshold này đã được validate trực tiếp trên `drifted.csv`: drift score = **1.0000**, vượt ngưỡng gấp ~6.7 lần — cho thấy ngưỡng không quá cao để bỏ sót drift thực. Ngoài ra, performance drift check bổ sung (precision = **0.3121 < 0.70**) xác nhận thêm rằng drift đã ảnh hưởng nghiêm trọng đến chất lượng model, không chỉ là distribution shift vô hại.

## 2. Khi v2 sau retrain tệ hơn v1

Pipeline xử lý trường hợp này qua **2 lớp bảo vệ**:

**Lớp 1 — Holdout validation trước khi promote:** Trong `retrain.py` Step 4, v2 được đánh giá trên `holdout.csv` (500 rows old-pattern data). Nếu v2 precision < v1 precision trên holdout, pipeline in `Holdout validation FAILED` và abort — v2 không bao giờ được promote lên production.

**Lớp 2 — Auto-rollback sau khi promote:** Sau khi v2 lên `@production`, `retrain.py` chạy 24 cycles post-deploy monitoring trên `post_deploy_eval.csv`. Nếu precision < **0.65** trong bất kỳ cycle nào, pipeline tự động restore v1 về `@production`, demote v2 về `@archived`, gọi `POST /reload`, và ghi event `auto_rollback_v2_to_v1` vào `outputs/audit_log.jsonl`. Trong kết quả chạy thực tế, v2 đạt precision = **1.0000** toàn bộ 24 cycles nên không cần rollback — nhưng cơ chế đã sẵn sàng.

## 3. Data drift vs concept drift — Evidently phát hiện loại nào

**Data drift** là khi phân phối input thay đổi: P(X) thay đổi nhưng P(Y|X) giữ nguyên. Ví dụ: latency_p99 tăng từ mean 120ms lên 156ms vì thêm 3rd-party integration — cùng một giá trị latency vẫn có nghĩa "anomaly" như cũ, chỉ là baseline đã dịch chuyển.

**Concept drift** là khi mối quan hệ input-output thay đổi: P(Y|X) thay đổi. Ví dụ: `drifted.csv` có 25% labels bị flip — cùng pattern latency/error_rate nhưng payment processor mới định nghĩa lại thế nào là "bình thường". Model cũ không thể biết điều này chỉ từ feature values.

Evidently `DataDriftPreset` **chỉ phát hiện data drift** — nó chạy statistical tests (Jensen-Shannon divergence, Wasserstein distance) trên feature distributions, hoàn toàn không biết đến labels. Để phát hiện concept drift, pipeline dùng thêm `--check-mode performance`: load model, chạy predict trên labeled data, tính precision/recall. Trong lab này, kết quả là precision = **0.3121** — con số này mới là bằng chứng của concept drift, không phải drift score 1.0.

## 4. Blue-green swap quan trọng hơn thay file trực tiếp

Thay file model trực tiếp (overwrite `model.pkl`) có 3 vấn đề nghiêm trọng:

**Thứ nhất, không có rollback path.** File cũ đã bị ghi đè — nếu v2 tệ, không có cách nào restore v1 ngay lập tức. Với MLflow aliases, rollback chỉ cần 1 lệnh: `set_registered_model_alias("production", v1_version)` + `POST /reload`.

**Thứ hai, downtime trong quá trình swap.** Thay file trực tiếp thường yêu cầu restart server — trong thời gian đó `/predict` trả về 503. Blue-green với `POST /reload` là hot-swap: serve.py load model mới trong memory, sau đó mới giải phóng model cũ, không có request nào bị drop.

**Thứ ba, không có version verification.** Sau khi thay file, không có cách nào biết chắc version nào đang chạy. Với `/health/active-version`, team on-call có thể verify ngay: `{"version": "2", "alias": "production"}` — xác nhận blue-green swap thành công trước khi cutover 100% traffic.

## 5. Nếu phải automate approval gate

Nếu không có human trong loop, em sẽ dùng **holdout precision** làm metric chính với threshold **≥ 0.85**:

- Điều kiện auto-approve: `v2_holdout_precision >= 0.85 AND v2_holdout_precision >= v1_holdout_precision`
- Điều kiện auto-reject: một trong hai điều kiện trên không thỏa mãn

Lý do chọn 0.85: đây là mức precision mà team đã chấp nhận khi deploy v1 lần đầu (precision ~0.91 trên validation set, trừ đi buffer 6% cho distribution shift). Ngoài ra, kết hợp thêm điều kiện `v2 >= v1` để đảm bảo retrain không bao giờ làm model tệ hơn hiện tại, dù v2 đạt 0.86 nhưng v1 đang ở 0.90 thì vẫn reject. Trong trường hợp auto-reject, pipeline giữ nguyên v1 `@production` và ghi alert vào audit log để team review vào đầu ngày hôm sau.
