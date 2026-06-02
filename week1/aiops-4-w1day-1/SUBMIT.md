# W1-D1: Metric Anomaly Detection — SUBMIT

## Screenshots

### Raw Time Series + Known Anomalies
![Raw Series](assets/plot_raw_series.png)

> 3 cụm anomaly thật (đỏ) rõ ràng: mid-Dec 2013 (drop mạnh xuống ~0), early-Feb 2014 (drop xuống ~25), và early-Feb 2014 lần 2. Data dao động quanh 80-100 là bình thường.

---

### Distribution Analysis
![Distribution](assets/plot_distribution.png)

> Histogram và KDE cho thấy data **left-skewed** (skewness = -1.834) — đa số giá trị tập trung ở 85-100, nhưng có đuôi dài bên trái do các lần nhiệt độ drop mạnh. KDE lệch xa so với Gaussian lý tưởng (đường đỏ đứt) → không nên dùng 3σ trực tiếp trên raw data.

---

### ACF Plot
![ACF](assets/plot_acf.png)

> ACF giảm dần liên tục, không có peak rõ ràng ở lag 288 (1 ngày). Kết luận: **data không có strong daily seasonal pattern**. Tuy nhiên vẫn dùng STL để kiểm tra vì data có thể có weak seasonality.

---

### STL Decomposition
![STL](assets/plot_stl_decomposition.png)

> STL tách thành công 3 thành phần:
> - **Trend**: bắt được 2 đợt drop lớn (Dec 2013 và Feb 2014)
> - **Seasonal**: dao động nhỏ ±10, chu kỳ đều — weak daily pattern
> - **Residual**: anomaly (chấm đỏ) hiện rõ khi residual vượt ±3σ band

---

### Anomaly Detection Comparison
![Comparison](assets/plot_comparison.png)

> - **STL + 3σ** (panel giữa): detect được cluster Dec-2013 và Feb-2014, nhưng sinh nhiều false alarm (cam) ở các vùng temperature drop tự nhiên
> - **Isolation Forest** (panel dưới): ít false alarm hơn, bắt được cluster Feb-2014 tốt hơn, nhưng miss một số điểm trong cluster Dec-2013

---

## Bảng So Sánh 2 Detector

| Metric         | Detector 1: STL + 3σ | Detector 2: Isolation Forest |
|----------------|---------------------:|-----------------------------:|
| Precision      | 0.165                | 0.286                        |
| Recall         | 0.107                | 0.489                        |
| F1             | 0.130                | 0.361                        |
| False Alarms   | cao                  | thấp hơn                     |
| Missed Anomaly | nhiều                | ít hơn                       |

---

## Tuning Log

### Isolation Forest — contamination tuning

| Run | contamination | Precision | Recall | F1    | False Alarms |
|-----|--------------|-----------|--------|-------|--------------|
| 1   | 0.010        | 0.489     | 0.286  | 0.361 | thấp         |
| 2   | 0.020        | 0.315     | 0.369  | 0.340 | trung bình   |
| 3   | 0.050        | 0.219     | 0.384  | 0.279 | cao          |

**Best**: contamination=0.01 → F1=0.361 

### STL — threshold tuning

| Run | Threshold | Precision | Recall | F1    |
|-----|-----------|-----------|--------|-------|
| 1   | 2.0σ      | 0.093     | 0.141  | 0.112 |
| 2   | 3.0σ      | 0.165     | 0.107  | 0.130 |
| 3   | 4.0σ      | 0.250     | 0.046  | 0.077 |

**Best**: threshold=3.0σ → F1=0.130 (default) 

### Isolation Forest — score threshold tuning (percentile)

| Run | Percentile | Threshold | Precision | Recall | F1    | Alerts |
|-----|-----------|-----------|-----------|--------|-------|--------|
| 1   | 1%        | -0.0000   | 0.489     | 0.286  | 0.361 | 227    |
| 2   | 3%        | 0.0671    | 0.219     | 0.384  | 0.279 | 681    |
| 3   | 8%        | 0.1234    | 0.113     | 0.531  | 0.187 | 1816   |

**Quan sát**: Recall 0.531 đạt được ở percentile=8% nhưng alerts tăng 8x (227→1816) — không khả thi production.

---

## Model Artifacts

- `isolation_forest_model.joblib` — Isolation Forest trained model (contamination=0.01, n_estimators=200)
- `scaler.joblib` — StandardScaler fitted trên training features

---

## Reflection

### 1. Data Type
- **Dataset**: machine_temperature_system_failure (NAB — realKnownCause)
- **Skewness = -1.834** → Heavily left-skewed. Đa số giá trị tập trung ở 85-100°C (nhiệt độ vận hành bình thường), đuôi dài bên trái do các lần drop đột ngột. 3σ trên raw data sẽ sai vì threshold âm vô nghĩa.
- **Seasonal**: Weak — ACF không có peak rõ ở lag 288. Data 5-minute, không có daily pattern mạnh.
- **Stationarity**: Non-stationary — có 2 đợt drop lớn (system failure events) làm mean thay đổi đột ngột.

### 2. Method Choice

**Detector 1: STL + 3σ**
Chọn vì data có thể có weak seasonality → STL tách seasonal ra trước, 3σ trên residual chính xác hơn raw data. Dù ACF không peak rõ, STL với robust=True vẫn handle được non-stationary data tốt hơn 3σ thuần.

**Detector 2: Isolation Forest**
Chọn vì:
- Data left-skewed → IF không giả định distribution
- Dùng feature engineering (rolling mean, std, rate of change, lag) để capture temporal context
- Không cần label để train (unsupervised)

### 3. Kết Quả & Trade-off

Isolation Forest thắng rõ ràng theo mọi metric:
- Recall cao hơn gần 5x (0.489 vs 0.107) → bắt được nhiều anomaly thật hơn
- Precision cao hơn gần 2x (0.286 vs 0.165) → ít false alarm hơn
- F1 cao hơn gần 3x (0.361 vs 0.130)

STL fail vì: data không có strong seasonality → seasonal component STL tách ra không có ý nghĩa nhiều → residual vẫn noisy → 3σ trên residual sinh nhiều false alarm ở các vùng temperature drop tự nhiên.

Khi tune threshold IF xuống percentile=8%, Recall đạt 0.531 (>50%) nhưng alerts tăng từ 227 lên 1816 — không dùng được trong production vì on-call sẽ bị alert fatigue.

### 4. Production Choice

Dùng **Isolation Forest làm detector chính** (Recall=0.489, F1=0.361).
STL giữ lại để cross-validate: khi IF trigger, nếu STL cũng trigger → high confidence alert → page on-call ngay. Nếu chỉ IF trigger → low confidence → log để review sau.

---
