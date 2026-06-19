"""
drift_detector.py — Drift detection using Evidently DataDriftPreset

Modes:
    --check-mode data         → data drift only (feature distribution)
    --check-mode performance  → performance/concept drift only
    --check-mode combined     → both (required for Stress 1)

Usage:
    python drift_detector.py \
        --reference data/baseline.csv \
        --current data/drifted.csv \
        --check-mode combined \
        --model-uri models:/anomaly-detector@production \
        --labeled-current data/drifted.csv
"""

import argparse
import json
import os
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import mlflow
import numpy as np
import pandas as pd
from sklearn.metrics import precision_score, recall_score, f1_score

warnings.filterwarnings("ignore")

# Evidently
from evidently.report import Report
from evidently.metric_preset import DataDriftPreset
from evidently.metrics import DatasetDriftMetric

# Prometheus pushgateway
try:
    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
MODEL_NAME = "anomaly-detector"
FEATURE_COLS = ["latency_p99", "error_rate", "rps"]
TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://localhost:9091")
OUTPUT_DIR = Path("outputs/drift_reports")
DRIFT_THRESHOLD = 0.15  # defended in DESIGN.md


@dataclass
class DriftResult:
    score: float
    is_drift: bool
    perf_precision: Optional[float] = None
    perf_recall: Optional[float] = None
    perf_f1: Optional[float] = None
    report_path: Optional[str] = None
    check_mode: str = "data"


# ─────────────────────────────────────────────
# Core functions
# ─────────────────────────────────────────────
def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    return df[FEATURE_COLS + [c for c in ["anomaly_label"] if c in pd.read_csv(path, nrows=1).columns]]


def detect_data_drift(
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    threshold: float = DRIFT_THRESHOLD,
) -> tuple[float, str]:
    """Run Evidently DataDriftPreset, return (drift_score, html_report_path)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ref = reference_df[FEATURE_COLS].copy()
    cur = current_df[FEATURE_COLS].copy()

    report = Report(metrics=[DataDriftPreset()])
    report.run(reference_data=ref, current_data=cur)

    # Extract drift score (share of drifted features)
    result_dict = report.as_dict()
    metrics = result_dict["metrics"]

    # DatasetDriftMetric is first metric in DataDriftPreset
    dataset_drift = metrics[0]["result"]
    drift_score = dataset_drift.get("share_of_drifted_columns", 0.0)

    # Save HTML report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = str(OUTPUT_DIR / f"drift_report_{timestamp}.html")
    report.save_html(report_path)

    return drift_score, report_path


def detect_performance_drift(
    model_uri: str,
    labeled_df: pd.DataFrame,
) -> tuple[float, float, float]:
    """Compare model predictions vs ground-truth labels → precision/recall/f1."""
    import mlflow.pyfunc
    mlflow.set_tracking_uri(TRACKING_URI)

    model = mlflow.pyfunc.load_model(model_uri)
    X = labeled_df[FEATURE_COLS]
    y_true = labeled_df["anomaly_label"].values

    raw_preds = model.predict(X)
    y_pred = (np.array(raw_preds) == -1).astype(int)

    precision = precision_score(y_true, y_pred, zero_division=0)
    recall = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)
    return precision, recall, f1


def push_metrics(drift_score: float, is_drift: bool, precision: Optional[float] = None):
    """Push drift metrics to Prometheus Pushgateway."""
    if not PROMETHEUS_AVAILABLE:
        return
    try:
        registry = CollectorRegistry()
        g_score = Gauge("drift_score", "Current drift score", registry=registry)
        g_flag = Gauge("drift_detected", "1 if drift detected else 0", registry=registry)
        g_score.set(drift_score)
        g_flag.set(1.0 if is_drift else 0.0)
        if precision is not None:
            g_prec = Gauge("model_precision", "Model precision on current data", registry=registry)
            g_prec.set(precision)
        push_to_gateway(PUSHGATEWAY_URL, job="drift_detector", registry=registry)
        print(f"[drift_detector] Pushed metrics to Pushgateway")
    except Exception as e:
        print(f"[drift_detector] Warning: could not push to Pushgateway: {e}")


def log_to_mlflow(drift_score: float, is_drift: bool, precision: Optional[float] = None):
    """Log drift score to MLflow as a metric."""
    try:
        mlflow.set_tracking_uri(TRACKING_URI)
        mlflow.set_experiment("drift-monitoring")
        with mlflow.start_run(run_name="drift-check"):
            mlflow.log_metric("drift_score", drift_score)
            mlflow.log_metric("drift_detected", int(is_drift))
            if precision is not None:
                mlflow.log_metric("perf_precision", precision)
        print(f"[drift_detector] Logged drift_score={drift_score:.4f} to MLflow")
    except Exception as e:
        print(f"[drift_detector] Warning: could not log to MLflow: {e}")


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────
def detect_drift(
    reference_df: pd.DataFrame,
    current_df: pd.DataFrame,
    threshold: float = DRIFT_THRESHOLD,
    check_mode: str = "data",
    model_uri: Optional[str] = None,
    labeled_current_df: Optional[pd.DataFrame] = None,
) -> DriftResult:
    """
    Main entry point.

    check_mode:
        'data'        → Evidently DataDriftPreset only
        'performance' → model precision/recall vs ground truth only
        'combined'    → both data drift + performance drift
    """
    drift_score = 0.0
    report_path = None
    precision = recall = f1 = None

    # ── Data drift ──
    if check_mode in ("data", "combined"):
        drift_score, report_path = detect_data_drift(reference_df, current_df, threshold)
        print(f"[drift_detector] Drift score (data): {drift_score:.4f}  threshold={threshold}")
        print(f"[drift_detector] Report saved: {report_path}")

    # ── Performance drift ──
    if check_mode in ("performance", "combined"):
        if model_uri is None:
            raise ValueError("--model-uri required for performance/combined check mode")
        if labeled_current_df is None or "anomaly_label" not in labeled_current_df.columns:
            raise ValueError("--labeled-current with 'anomaly_label' column required for performance check")

        precision, recall, f1 = detect_performance_drift(model_uri, labeled_current_df)
        print(f"[drift_detector] Perf precision: {precision:.4f}  recall: {recall:.4f}  f1: {f1:.4f}")

    is_drift = drift_score > threshold
    if check_mode in ("performance", "combined") and precision is not None:
        # Also flag drift if precision drops significantly
        PRECISION_FLOOR = 0.70
        if precision < PRECISION_FLOOR:
            print(f"[drift_detector] Performance drift flagged: precision {precision:.4f} < {PRECISION_FLOOR}")
            is_drift = True

    print(f"[drift_detector] is_drift={is_drift}  (score={drift_score:.4f} {'>' if is_drift else '<='} threshold={threshold})")

    push_metrics(drift_score, is_drift, precision)
    log_to_mlflow(drift_score, is_drift, precision)

    return DriftResult(
        score=drift_score,
        is_drift=is_drift,
        perf_precision=precision,
        perf_recall=recall,
        perf_f1=f1,
        report_path=report_path,
        check_mode=check_mode,
    )


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Drift detector using Evidently")
    parser.add_argument("--reference", default="data/baseline.csv")
    parser.add_argument("--current", default="data/drifted.csv")
    parser.add_argument("--threshold", type=float, default=DRIFT_THRESHOLD)
    parser.add_argument(
        "--check-mode",
        choices=["data", "performance", "combined"],
        default="data",
        help="Type of drift to check",
    )
    parser.add_argument("--model-uri", default=None, help="MLflow model URI for performance check")
    parser.add_argument("--labeled-current", default=None, help="CSV with anomaly_label column")
    args = parser.parse_args()

    ref_df = pd.read_csv(args.reference, parse_dates=["timestamp"])
    cur_df = pd.read_csv(args.current, parse_dates=["timestamp"])

    labeled_df = None
    if args.labeled_current:
        labeled_df = pd.read_csv(args.labeled_current, parse_dates=["timestamp"])

    result = detect_drift(
        reference_df=ref_df,
        current_df=cur_df,
        threshold=args.threshold,
        check_mode=args.check_mode,
        model_uri=args.model_uri,
        labeled_current_df=labeled_df,
    )

    print("\n── Summary ──────────────────────────────────")
    print(f"  Check mode : {result.check_mode}")
    print(f"  Drift score: {result.score:.4f}")
    print(f"  Is drift   : {result.is_drift}")
    if result.perf_precision is not None:
        print(f"  Perf precision: {result.perf_precision:.4f}")
        print(f"  Perf recall   : {result.perf_recall:.4f}")
        print(f"  Perf f1       : {result.perf_f1:.4f}")
    if result.report_path:
        print(f"  Report     : {result.report_path}")
    print("─────────────────────────────────────────────")


if __name__ == "__main__":
    main()
