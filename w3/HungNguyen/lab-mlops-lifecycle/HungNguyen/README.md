# Lab MLOps Lifecycle — Anomaly Detection

**Sinh viên:** HungNguyen

---

## Tổng quan về lab

Lab này mô phỏng công việc thực tế của một MLOps Engineer tại một công ty fintech. Bối cảnh: công ty đã deploy một model phát hiện anomaly (bất thường) trên payment gateway từ 2 tháng trước — model theo dõi 3 chỉ số: độ trễ (`latency_p99`), tỉ lệ lỗi (`error_rate`) và lưu lượng (`rps`). Sau một chiến dịch marketing và tích hợp thêm 3rd-party, dữ liệu production đã thay đổi hoàn toàn so với lúc train — model cũ bắt đầu bỏ sót anomaly và sinh ra false positive nhiều hơn. Hiện tượng này gọi là **model decay** hay **drift**.

Nhiệm vụ trong lab là xây dựng một pipeline MLOps hoàn chỉnh gồm 4 thành phần:
1. Train model và đăng ký lên MLflow Registry
2. Serve model qua FastAPI
3. Phát hiện drift tự động bằng Evidently
4. Retrain model khi có drift, swap vào production với approval gate và auto-rollback

---

## Giải thích từng file code

### `pipeline.py` — Train và đăng ký model

File này thực hiện toàn bộ quá trình training model v1:

- Đọc dữ liệu từ `baseline.csv` (4320 rows, 30 ngày hoạt động bình thường)
- Train model **IsolationForest** — thuật toán unsupervised learning phát hiện điểm bất thường bằng cách cô lập chúng trong cây quyết định. Tham số: `contamination=0.05` (giả sử 5% data là anomaly), `n_estimators=100`, `random_state=42`
- Log toàn bộ params, metrics (`train_anomaly_rate`, `feature_count`) và artifact (file model) lên **MLflow Tracking Server**
- Đăng ký model vào **MLflow Registry** với tên `anomaly-detector`, gán alias `@production` cho version 1

Kết quả thực tế khi chạy: `train_anomaly_rate=0.0500`, model v1 được đăng ký thành công với alias `@production`.

---

### `serve.py` — API server phục vụ model

File này tạo ra một HTTP server bằng **FastAPI** để các hệ thống khác có thể gửi dữ liệu và nhận kết quả dự đoán:

- Khi khởi động: tự động load model từ `models:/anomaly-detector@production` trong MLflow Registry
- `POST /predict` — nhận JSON chứa danh sách các điểm dữ liệu `[latency_p99, error_rate, rps]`, trả về kết quả `{predictions, scores, version}`. Giá trị 1 = anomaly, 0 = bình thường
- `GET /health/active-version` — trả về version model đang được serve, dùng để verify sau blue-green swap
- `POST /reload` — hot-reload model từ registry mà không cần restart server. Được gọi tự động bởi `retrain.py` sau khi swap alias
- `GET /metrics` — expose Prometheus metrics (request count, latency p99/p50, active version)

Thiết kế quan trọng: server luôn load từ alias `@production` chứ không hardcode version number. Khi alias được swap từ v1 → v2, chỉ cần gọi `/reload` là xong, không cần thay đổi code hay restart.

---

### `drift_detector.py` — Phát hiện data drift và performance drift

File này kiểm tra xem dữ liệu production hiện tại có bị drift so với dữ liệu lúc train không, hỗ trợ 3 chế độ:

- `--check-mode data` — dùng **Evidently DataDriftPreset** chạy statistical tests (Jensen-Shannon divergence, Wasserstein distance) trên từng feature. Trả về drift score từ 0 đến 1 (tỉ lệ features bị drift). Kết quả thực tế: score = **1.0000** (cả 3 features đều drift nặng)
- `--check-mode performance` — load model từ registry, chạy predict trên labeled data, tính precision/recall/f1 so với ground truth. Kết quả thực tế: precision = **0.3121** (model v1 chỉ đúng 31% trên dữ liệu mới)
- `--check-mode combined` — chạy cả hai, flag drift nếu score > threshold (0.15) HOẶC precision < 0.70

Kết quả được lưu vào:
- HTML report tại `outputs/drift_reports/` (báo cáo chi tiết từ Evidently)
- MLflow experiment `drift-monitoring` (để track trend theo thời gian)
- Prometheus Pushgateway (để hiển thị trên Grafana dashboard)

---

### `retrain.py` — Orchestrator toàn bộ pipeline retrain

File này là trái tim của hệ thống, điều phối toàn bộ quy trình từ khi phát hiện drift đến khi swap model mới vào production:

**Bước 1 — Drift check:** Gọi `drift_detector.py` với `--check-mode combined`. Nếu không có drift → dừng lại, không làm gì.

**Bước 2 — Train v2 (Sliding Window):** Nếu có drift → gộp `baseline.csv` + `drifted.csv` thành 5328 rows và train model mới. Chiến lược "sliding window" này đảm bảo v2 không overfit chỉ vào distribution mới mà vẫn nhớ pattern cũ.

**Bước 3 — Register @staging:** Đăng ký v2 lên MLflow Registry với alias `@staging`, chưa đưa vào production.

**Bước 4 — Holdout validation:** Đánh giá cả v1 và v2 trên `holdout.csv` (500 rows old-pattern data). Nếu v2 precision < v1 precision → abort, không promote.

**Bước 5 — Approval gate:** In ra `Promote to production? [y/N]` và chờ con người xác nhận. Đây là bắt buộc trong fintech — không cho phép fully automatic vì sai model = thiệt hại tài chính.

**Bước 6 — Promote:** Swap alias `@staging → @production`, archive v1 về `@archived`, gọi `POST /reload` để serve.py load v2.

**Bước 7 — Post-deploy monitoring:** Theo dõi v2 trong 24 cycles trên `post_deploy_eval.csv`. Nếu precision < 0.65 → auto-rollback: restore v1 về `@production`, demote v2 về `@archived`, ghi event vào `outputs/audit_log.jsonl`.

Kết quả thực tế: v2 đạt precision = **1.0000** trong toàn bộ 24 cycles, không cần rollback.

---

## Dữ liệu sử dụng

| File | Mô tả | Số rows |
|---|---|---|
| `baseline.csv` | 30 ngày hoạt động bình thường. Latency ~120ms, error_rate ~0.8%, rps ~450 | 4320 |
| `drifted.csv` | 7 ngày sau campaign. Latency ~156ms (+30%), error_rate ~1.6% (×2), rps ~630 (+40%), 25% labels bị flip | 1008 |
| `holdout.csv` | 500 rows old-pattern để kiểm tra v2 không quên pattern cũ | 500 |
| `post_deploy_eval.csv` | 200 rows có ground truth để monitor v2 sau deploy | 200 |

---

## Stack hạ tầng

| Service | Cổng | Vai trò |
|---|---|---|
| MLflow | 5000 | Tracking experiments, lưu artifacts, Model Registry |
| PostgreSQL | 5432 | Backend store cho MLflow |
| serve.py (FastAPI) | 8000 | Serve model, blue-green swap |
| Prometheus | 9090 | Thu thập metrics |
| Pushgateway | 9091 | Nhận metrics từ batch jobs |
| Grafana | 3000 | Dashboard quan sát toàn bộ lifecycle |

---

## Hướng dẫn chạy từ đầu đến cuối

### Yêu cầu
- Python 3.11, Docker Desktop đang chạy
- Tất cả lệnh chạy từ thư mục `data-pack/`

### Bước 1 — Cài đặt môi trường

```powershell
py -3.11 -m venv venv
venv\Scripts\Activate.ps1

pip install "mlflow==2.13.2" "evidently==0.4.40" scikit-learn pandas numpy fastapi uvicorn prometheus_client requests

bash scripts/start_stack.sh

$env:MLFLOW_TRACKING_URI="http://localhost:5000"
```

### Bước 2 — Train model v1

```powershell
python HungNguyen\pipeline.py --data data\baseline.csv
```

### Bước 3 — Chạy API server (terminal riêng)

```powershell
$env:MLFLOW_TRACKING_URI="http://localhost:5000"
python HungNguyen\serve.py

# Verify ở terminal khác:
curl http://localhost:8000/health/active-version
```

### Bước 4 — Phát hiện drift

```powershell
python HungNguyen\drift_detector.py `
  --reference data\baseline.csv `
  --current data\drifted.csv `
  --check-mode combined `
  --model-uri models:/anomaly-detector@production `
  --labeled-current data\drifted.csv
```

### Bước 5 — Retrain pipeline đầy đủ

```powershell
python HungNguyen\retrain.py `
  --reference data\baseline.csv `
  --current data\drifted.csv `
  --holdout data\holdout.csv `
  --post-deploy-eval data\post_deploy_eval.csv
# Khi thấy prompt → gõ y để approve
```

### Bước 6 — Xem dashboard

Mở http://localhost:3000 → dashboard **AIOps MLOps Lifecycle**
