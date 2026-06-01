# Anomaly Detection Report for EC2 Request Latency

Dataset: ec2_request_latency_system_failure.csv
Period: 2014-03-07 to 2014-03-21
Interval: 5 minutes
Total points: 4032

---

## 1. Screenshots

### 1.1 Raw Time Series
![Raw Time Series](assets/plot_raw_series.png)

### 1.2 Histogram and Distribution
![Histogram and Distribution](assets/plot_distribution.png)

### 1.3 Anomaly Detection Results
![Anomaly Detection Results](assets/plot_comparison.png)

### 1.4 ACF Plot
![ACF Plot](assets/plot_acf.png)

### 1.5 STL Decomposition
![STL Decomposition](assets/plot_stl_decomposition.png)

---

## 2. Comparison Table

Default parameter results before tuning:

| Metric | Detector 1 Rolling Z score | Detector 2 Isolation Forest |
| :--- | :--- | :--- |
| Precision | 0.2222 | 0.2308 |
| Recall | 0.8000 | 0.9000 |
| F1 | 0.3478 | 0.3673 |
| False Alarms | 28 | 30 |

Best configuration results after tuning:

| Metric | Z score Best Config | Isolation Forest Best Config |
| :--- | :--- | :--- |
| Precision | 0.6154 | 0.4500 |
| Recall | 0.8000 | 0.9000 |
| F1 | 0.6957 | 0.6000 |
| False Alarms | 5 | 11 |
| Best Params | Window 288, Threshold 4.0 | Contamination 0.005, Estimators 50 |

---

## 3. Tuning Log

### 3.1 Detector 1 Rolling Z score

Grid search parameters: window sizes 144, 288, 576 and threshold levels 2.0, 2.5, 3.0, 3.5, 4.0

Selected tuning runs:

Run 1: Window 288, Threshold 3.0, Precision 0.2222, Recall 0.8000, F1 0.3478, False Alarms 28
Run 2: Window 144, Threshold 2.5, Precision 0.0976, Recall 0.8000, F1 0.1739, False Alarms 74
Run 3: Window 576, Threshold 3.5, Precision 0.4444, Recall 0.8000, F1 0.5714, False Alarms 10

Observations:
Reducing the threshold increases recall but decreases precision because of more false alarms.
Increasing the window size provides a more stable baseline but delays response to sudden changes.
Increasing both window size and threshold reduces the F1 score due to missed anomalies.

### 3.2 Detector 2 Isolation Forest

Grid search parameters: contamination rates 0.005, 0.01, 0.02, 0.03, 0.05 and estimator counts 50, 100, 200

Selected tuning runs:

Run 1: Contamination 0.01, Estimators 100, Precision 0.2308, Recall 0.9000, F1 0.3673, False Alarms 30
Run 2: Contamination 0.02, Estimators 100, Precision 0.1154, Recall 0.9000, F1 0.2045, False Alarms 69
Run 3: Contamination 0.05, Estimators 200, Precision 0.0462, Recall 0.9000, F1 0.0878, False Alarms 186

Observations:
Higher contamination values increase recall as more points are classified as anomalies, which decreases precision.
The number of estimators has a minor impact on the F1 score because the dataset is mostly stable.
A contamination of 0.005 delivers the highest F1 score for this dataset.

---

## 4. Model Artifacts

Trained Isolation Forest model file:
artifacts/isolation_forest.joblib

Loading example:
```python
import joblib
clf = joblib.load("artifacts/isolation_forest.joblib")
preds = clf.predict(X)
```

Best configuration parameters:
Contamination: 0.005
Estimators: 50
Random State: 42
Training Size: 3889 rows, 11 features

---

## 5. Reflection

### 5.1 Data Characteristics

The EC2 request latency is a continuous time series measured at 5 minute intervals over 14 days.

Main observations:
1. The distribution is close to Gaussian with low skewness. The histogram shows a symmetric bell shape.
2. The series is stationary during most periods. The mean and standard deviation remain stable around 44 milliseconds and 2 milliseconds respectively.
3. There is no clear daily seasonal pattern.
4. There is a single anomaly window at the end of the dataset, from 2014-03-21 02:55 to 2014-03-21 03:41.

### 5.2 Method Selection Justification

The Rolling Z score method is chosen because the data is stationary and close to Gaussian. This method is effective when the distribution is not skewed and uses a statistical threshold. A window size of one day calculates the baseline from recent history instead of the entire series.

Isolation Forest is selected because it is a machine learning method that does not assume any specific data distribution. It detects anomalies by isolating points using multiple engineered features. The feature table includes 11 dimensions representing level, velocity, acceleration, volatility, and range.

### 5.3 Detector Performance Comparison

Based on the F1 score, the Rolling Z score detector performed better than the Isolation Forest detector.
The system failure anomaly is a large and sudden latency spike. The statistical Z score method is highly sensitive to such sudden deviations, which explains its better performance.

### 5.4 Trade-off Analysis

| Metric | Rolling Z score | Isolation Forest |
| :--- | :--- | :--- |
| Training Time | None | Fast |
| Interpretability | High | Low |
| Distribution Dependent | Yes | No |
| Features Used | 1 | 11 |
| Clear Anomaly Performance | High | High |
| Complex Anomaly Performance | Low | High |
| Noise Sensitivity | High | Medium |

### 5.5 Production Recommendation

For monitoring EC2 request latency in a production environment, the Rolling Z score is recommended.
1. It does not require retraining on new data and runs efficiently on streaming data.
2. It is easy to explain to on-call engineers because alert triggers are based on standard deviation deviations.
3. The latency anomaly is a sudden spike, which is easily captured by Z score.
4. The computational cost is low, making it suitable for real-time calculation.

The Isolation Forest model can be used as a secondary validator to confirm Z score alerts before alerting engineers.
