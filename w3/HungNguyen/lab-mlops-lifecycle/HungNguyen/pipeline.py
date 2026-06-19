"""
pipeline.py — Train IsolationForest on baseline.csv, log to MLflow, register with alias 'production'

Usage:
    python pipeline.py --data data/baseline.csv
"""

import argparse
import os
import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import precision_score, recall_score, f1_score
import mlflow
import mlflow.sklearn
from mlflow import MlflowClient

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
MODEL_NAME = "anomaly-detector"
CONTAMINATION = 0.05
N_ESTIMATORS = 100
RANDOM_STATE = 42
FEATURE_COLS = ["latency_p99", "error_rate", "rps"]


def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    print(f"[pipeline] Loaded {len(df)} rows from {path}")
    return df


def train(df: pd.DataFrame):
    X = df[FEATURE_COLS].values

    model = IsolationForest(
        contamination=CONTAMINATION,
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
    )
    model.fit(X)

    # IsolationForest: -1 = anomaly, 1 = normal → convert to 0/1
    raw_preds = model.predict(X)
    preds = (raw_preds == -1).astype(int)
    train_anomaly_rate = preds.mean()

    return model, train_anomaly_rate, X


def evaluate(model, df: pd.DataFrame):
    """If 'anomaly_label' column exists, compute precision/recall/f1."""
    if "anomaly_label" not in df.columns:
        return None, None, None

    X = df[FEATURE_COLS].values
    raw_preds = model.predict(X)
    preds = (raw_preds == -1).astype(int)
    labels = df["anomaly_label"].values

    precision = precision_score(labels, preds, zero_division=0)
    recall = recall_score(labels, preds, zero_division=0)
    f1 = f1_score(labels, preds, zero_division=0)
    return precision, recall, f1


def register_model(run_id: str, model_uri: str, version_tag: str = "v1") -> int:
    client = MlflowClient()

    # Register (creates model if not exists)
    mv = mlflow.register_model(model_uri=model_uri, name=MODEL_NAME)
    version = mv.version
    print(f"[pipeline] Registered {MODEL_NAME} version {version}")

    # Tag with semantic version
    client.set_model_version_tag(MODEL_NAME, version, "semantic_version", version_tag)

    # Set alias → production
    client.set_registered_model_alias(MODEL_NAME, "production", version)
    print(f"[pipeline] Alias 'production' → version {version}")

    return int(version)


def main(data_path: str, version_tag: str = "v1"):
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("anomaly-detection")

    df = load_data(data_path)
    model, train_anomaly_rate, X = train(df)
    precision, recall, f1 = evaluate(model, df)

    with mlflow.start_run(run_name=f"train-{version_tag}") as run:
        # Log params
        mlflow.log_param("contamination", CONTAMINATION)
        mlflow.log_param("n_estimators", N_ESTIMATORS)
        mlflow.log_param("random_state", RANDOM_STATE)
        mlflow.log_param("data_path", data_path)
        mlflow.log_param("version_tag", version_tag)
        mlflow.log_param("n_rows", len(df))

        # Log metrics
        mlflow.log_metric("train_anomaly_rate", train_anomaly_rate)
        mlflow.log_metric("feature_count", len(FEATURE_COLS))
        if precision is not None:
            mlflow.log_metric("precision", precision)
            mlflow.log_metric("recall", recall)
            mlflow.log_metric("f1", f1)
            print(f"[pipeline] Precision={precision:.4f}  Recall={recall:.4f}  F1={f1:.4f}")

        print(f"[pipeline] train_anomaly_rate={train_anomaly_rate:.4f}")

        # Log model artifact
        mlflow.sklearn.log_model(
            model,
            artifact_path="model",
            registered_model_name=None,  # we register manually below
        )

        model_uri = f"runs:/{run.info.run_id}/model"
        version = register_model(run.info.run_id, model_uri, version_tag)

        mlflow.set_tag("registered_version", version)
        mlflow.set_tag("alias", "production")

    print(f"[pipeline] Done. Model v{version} is now @production")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/baseline.csv")
    parser.add_argument("--version-tag", default="v1")
    args = parser.parse_args()
    main(args.data, args.version_tag)
