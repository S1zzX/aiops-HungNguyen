"""
retrain.py — MLOps Orchestrator

Flow:
    1. Run drift detection (data + performance)
    2. If drift detected → train v2 on sliding window (baseline + drift window)
    3. Register v2 with alias @staging
    4. Holdout validation: v2 precision must >= v1 precision
    5. Approval gate [y/N]
    6. Promote @staging → @production, call POST /reload on serve.py
    7. Post-deploy monitoring for 24 cycles on post_deploy_eval.csv
    8. Auto-rollback if precision < 0.65

Usage:
    python retrain.py \
        --reference data/baseline.csv \
        --current data/drifted.csv \
        --holdout data/holdout.csv \
        --post-deploy-eval data/post_deploy_eval.csv
"""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import mlflow
import mlflow.sklearn
import numpy as np
import pandas as pd
import requests
from mlflow import MlflowClient
from sklearn.ensemble import IsolationForest
from sklearn.metrics import precision_score, recall_score, f1_score

from drift_detector import detect_drift, DRIFT_THRESHOLD, FEATURE_COLS

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
MODEL_NAME = "anomaly-detector"
TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
SERVE_URL = os.environ.get("SERVE_URL", "http://localhost:8000")
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://localhost:9091")
AUDIT_LOG = Path("outputs/audit_log.jsonl")
CONTAMINATION = 0.05
N_ESTIMATORS = 100
RANDOM_STATE = 42
POST_DEPLOY_PRECISION_FLOOR = 0.65
POST_DEPLOY_CYCLES = 24

mlflow.set_tracking_uri(TRACKING_URI)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def write_audit(event: str, **kwargs):
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.utcnow().isoformat(), "event": event, **kwargs}
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[audit] {event}: {kwargs}")


def push_metric(name: str, value: float, job: str = "retrain"):
    try:
        from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
        registry = CollectorRegistry()
        g = Gauge(name, name, registry=registry)
        g.set(value)
        push_to_gateway(PUSHGATEWAY_URL, job=job, registry=registry)
    except Exception as e:
        print(f"[retrain] Warning: pushgateway error: {e}")


def get_version_for_alias(alias: str) -> str:
    client = MlflowClient()
    mv = client.get_model_version_by_alias(MODEL_NAME, alias)
    return mv.version


def evaluate_on_holdout(model, holdout_df: pd.DataFrame):
    X = holdout_df[FEATURE_COLS].values
    y_true = holdout_df["anomaly_label"].values
    raw = model.predict(X)
    y_pred = (raw == -1).astype(int)
    precision = precision_score(y_true, y_pred, zero_division=0)
    recall = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)
    return precision, recall, f1


def reload_serve():
    """Call POST /reload on serve.py to hot-swap model."""
    try:
        r = requests.post(f"{SERVE_URL}/reload", timeout=10)
        data = r.json()
        print(f"[retrain] serve.py reloaded: {data}")
        return data
    except Exception as e:
        print(f"[retrain] Warning: could not reload serve.py: {e}")
        return {}


# ─────────────────────────────────────────────
# Step 1: Drift check
# ─────────────────────────────────────────────
def step_drift_check(ref_df, cur_df, labeled_df, model_uri, threshold, check_mode):
    print("\n═══ Step 1: Drift Detection ═══════════════════")
    result = detect_drift(
        reference_df=ref_df,
        current_df=cur_df,
        threshold=threshold,
        check_mode=check_mode,
        model_uri=model_uri,
        labeled_current_df=labeled_df,
    )
    write_audit(
        "drift_check",
        drift_score=result.score,
        is_drift=result.is_drift,
        check_mode=check_mode,
        perf_precision=result.perf_precision,
    )
    return result


# ─────────────────────────────────────────────
# Step 2: Train v2 on sliding window
# ─────────────────────────────────────────────
def step_train_v2(ref_df: pd.DataFrame, cur_df: pd.DataFrame) -> tuple:
    """
    Sliding window strategy: combine baseline + drift window.
    This prevents v2 from overfitting to only the new distribution.
    """
    print("\n═══ Step 2: Train v2 (Sliding Window) ══════════")
    combined = pd.concat([ref_df, cur_df], ignore_index=True)
    print(f"[retrain] Training on {len(combined)} rows (baseline={len(ref_df)} + drift={len(cur_df)})")

    X = combined[FEATURE_COLS].values
    model_v2 = IsolationForest(
        contamination=CONTAMINATION,
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
    )
    model_v2.fit(X)
    train_anomaly_rate = ((model_v2.predict(X) == -1).mean())
    print(f"[retrain] v2 train_anomaly_rate={train_anomaly_rate:.4f}")
    return model_v2, train_anomaly_rate, combined


# ─────────────────────────────────────────────
# Step 3: Register v2 as @staging
# ─────────────────────────────────────────────
def step_register_staging(model_v2, train_anomaly_rate, combined_df) -> tuple:
    print("\n═══ Step 3: Register v2 → @staging ═════════════")
    client = MlflowClient()

    with mlflow.start_run(run_name="retrain-v2") as run:
        mlflow.log_param("contamination", CONTAMINATION)
        mlflow.log_param("n_estimators", N_ESTIMATORS)
        mlflow.log_param("random_state", RANDOM_STATE)
        mlflow.log_param("training_strategy", "sliding_window_baseline+drift")
        mlflow.log_param("n_rows", len(combined_df))
        mlflow.log_metric("train_anomaly_rate", train_anomaly_rate)

        mlflow.sklearn.log_model(model_v2, artifact_path="model")
        model_uri = f"runs:/{run.info.run_id}/model"

    # Register
    mv = mlflow.register_model(model_uri=model_uri, name=MODEL_NAME)
    v2_version = mv.version
    client.set_model_version_tag(MODEL_NAME, v2_version, "semantic_version", "v2")

    # Set @staging alias
    client.set_registered_model_alias(MODEL_NAME, "staging", v2_version)
    print(f"[retrain] Registered {MODEL_NAME} version={v2_version} → @staging")

    write_audit("v2_registered_staging", version=v2_version)
    push_metric("retrain_triggered_total", 1.0)

    return v2_version, model_v2


# ─────────────────────────────────────────────
# Step 4: Holdout validation (Stress 2)
# ─────────────────────────────────────────────
def step_holdout_validation(model_v1_uri: str, model_v2, holdout_df: pd.DataFrame) -> bool:
    print("\n═══ Step 4: Holdout Validation ══════════════════")
    import mlflow.pyfunc

    # v1 precision on holdout
    model_v1 = mlflow.pyfunc.load_model(model_v1_uri)
    X = holdout_df[FEATURE_COLS]
    y_true = holdout_df["anomaly_label"].values

    raw_v1 = model_v1.predict(X)
    y_pred_v1 = (np.array(raw_v1) == -1).astype(int)
    v1_precision = precision_score(y_true, y_pred_v1, zero_division=0)
    v1_recall = recall_score(y_true, y_pred_v1, zero_division=0)

    # v2 precision on holdout
    raw_v2 = model_v2.predict(X.values)
    y_pred_v2 = (raw_v2 == -1).astype(int)
    v2_precision = precision_score(y_true, y_pred_v2, zero_division=0)
    v2_recall = recall_score(y_true, y_pred_v2, zero_division=0)

    print(f"[retrain] Holdout validation — v1 precision: {v1_precision:.4f}  recall: {v1_recall:.4f}")
    print(f"[retrain] Holdout validation — v2 precision: {v2_precision:.4f}  recall: {v2_recall:.4f}")

    passed = v2_precision >= v1_precision
    print(f"[retrain] Holdout check {'PASSED ✓' if passed else 'FAILED ✗'} (v2 >= v1: {v2_precision:.4f} >= {v1_precision:.4f})")

    write_audit(
        "holdout_validation",
        v1_precision=v1_precision,
        v2_precision=v2_precision,
        passed=passed,
    )
    return passed, v1_precision, v2_precision


# ─────────────────────────────────────────────
# Step 5: Approval gate
# ─────────────────────────────────────────────
def step_approval_gate(v2_version: str) -> bool:
    print("\n═══ Step 5: Approval Gate ═══════════════════════")
    print(f"  Drift detected. Model v2 (version={v2_version}) registered as @staging.")
    answer = input("  Promote to production? [y/N]: ").strip().lower()
    approved = answer == "y"
    write_audit("approval_gate", decision="approved" if approved else "rejected", version=v2_version)
    return approved


# ─────────────────────────────────────────────
# Step 6: Promote staging → production
# ─────────────────────────────────────────────
def step_promote(v2_version: str):
    print("\n═══ Step 6: Promote @staging → @production ══════")
    client = MlflowClient()

    # Archive v1
    v1_version = get_version_for_alias("production")
    client.set_registered_model_alias(MODEL_NAME, "archived", v1_version)
    print(f"[retrain] v1 (version={v1_version}) → @archived")

    # Promote v2
    client.set_registered_model_alias(MODEL_NAME, "production", v2_version)
    client.delete_registered_model_alias(MODEL_NAME, "staging")
    print(f"[retrain] v2 (version={v2_version}) → @production")

    write_audit("promoted_to_production", promoted_version=v2_version, archived_version=v1_version)
    push_metric("production_version", float(v2_version))

    # Hot-reload serve.py
    reload_serve()
    return v1_version


# ─────────────────────────────────────────────
# Step 7: Post-deploy monitoring + auto-rollback (Stress 3)
# ─────────────────────────────────────────────
def step_post_deploy_monitor(
    post_deploy_df: pd.DataFrame,
    v1_version: str,
    v2_version: str,
    cycles: int = POST_DEPLOY_CYCLES,
    precision_floor: float = POST_DEPLOY_PRECISION_FLOOR,
):
    print("\n═══ Step 7: Post-Deploy Monitoring ══════════════")
    client = MlflowClient()
    import mlflow.pyfunc

    # Load v2
    model_v2 = mlflow.pyfunc.load_model(f"models:/{MODEL_NAME}@production")
    X = post_deploy_df[FEATURE_COLS]
    y_true = post_deploy_df["anomaly_label"].values

    rollback_occurred = False

    for cycle in range(1, cycles + 1):
        raw = model_v2.predict(X)
        y_pred = (np.array(raw) == -1).astype(int)
        precision = precision_score(y_true, y_pred, zero_division=0)
        recall = recall_score(y_true, y_pred, zero_division=0)

        print(f"[retrain] post_deploy_monitor Cycle {cycle:02d}/{cycles}  precision={precision:.4f}  recall={recall:.4f}")
        push_metric("post_deploy_precision", precision, job="post_deploy")

        if precision < precision_floor:
            print(f"\n[retrain] ⚠ Precision {precision:.4f} < floor {precision_floor} — triggering auto-rollback!")

            # Rollback: restore v1 → @production, demote v2 → @archived
            client.set_registered_model_alias(MODEL_NAME, "production", v1_version)
            client.set_registered_model_alias(MODEL_NAME, "archived", v2_version)
            try:
                client.delete_registered_model_alias(MODEL_NAME, "staging")
            except Exception:
                pass

            reload_serve()
            rollback_occurred = True

            write_audit(
                "auto_rollback_v2_to_v1",
                demoted_version=v2_version,
                restored_version=v1_version,
                trigger_precision=precision,
                cycle=cycle,
            )
            push_metric("auto_rollback_total", 1.0, job="retrain")

            print(f"[retrain] Rollback complete. v1 restored to @production. v2 → @archived")
            break

        time.sleep(0.1)  # simulate polling interval

    if not rollback_occurred:
        print(f"\n[retrain] ✓ v2 stable over {cycles} cycles. No rollback needed.")
        write_audit("post_deploy_stable", cycles=cycles, version=v2_version)

    return rollback_occurred


# ─────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MLOps Retrain Orchestrator")
    parser.add_argument("--reference", default="data/baseline.csv")
    parser.add_argument("--current", default="data/drifted.csv")
    parser.add_argument("--holdout", default=None, help="data/holdout.csv for Stress 2")
    parser.add_argument("--post-deploy-eval", default=None, help="data/post_deploy_eval.csv for Stress 3")
    parser.add_argument("--threshold", type=float, default=DRIFT_THRESHOLD)
    parser.add_argument("--check-mode", choices=["data", "performance", "combined"], default="combined")
    parser.add_argument("--model-uri", default=f"models:/{MODEL_NAME}@production")
    args = parser.parse_args()

    print(f"\n{'═'*55}")
    print(f"  MLOps Retrain Orchestrator")
    print(f"  reference  : {args.reference}")
    print(f"  current    : {args.current}")
    print(f"  threshold  : {args.threshold}")
    print(f"  check_mode : {args.check_mode}")
    print(f"{'═'*55}\n")

    # Load data
    ref_df = pd.read_csv(args.reference, parse_dates=["timestamp"])
    cur_df = pd.read_csv(args.current, parse_dates=["timestamp"])
    labeled_df = pd.read_csv(args.current, parse_dates=["timestamp"])  # current may have labels

    # Step 1: Drift detection
    drift_result = step_drift_check(
        ref_df, cur_df, labeled_df, args.model_uri, args.threshold, args.check_mode
    )

    if not drift_result.is_drift:
        print("\n[retrain] No drift detected. No retraining needed.")
        write_audit("no_drift_no_retrain", drift_score=drift_result.score)
        return

    print(f"\n[retrain] 🔴 Drift detected (score={drift_result.score:.4f}). Starting retrain pipeline...")

    # Step 2: Train v2
    model_v2, train_anomaly_rate, combined_df = step_train_v2(ref_df, cur_df)

    # Step 3: Register @staging
    v2_version, model_v2_sklearn = step_register_staging(model_v2, train_anomaly_rate, combined_df)

    # Step 4: Holdout validation (Stress 2)
    if args.holdout:
        holdout_df = pd.read_csv(args.holdout, parse_dates=["timestamp"])
        passed, v1_prec, v2_prec = step_holdout_validation(
            f"models:/{MODEL_NAME}@production", model_v2, holdout_df
        )
        if not passed:
            print(f"[retrain] ✗ v2 holdout validation FAILED. Aborting promotion.")
            write_audit("promotion_aborted", reason="holdout_validation_failed", v2_precision=v2_prec)
            return

    # Step 5: Approval gate
    approved = step_approval_gate(v2_version)
    if not approved:
        print("[retrain] Promotion rejected. v2 remains @staging.")
        return

    # Step 6: Promote → production
    v1_version = step_promote(v2_version)

    # Step 7: Post-deploy monitoring (Stress 3)
    if args.post_deploy_eval:
        post_deploy_df = pd.read_csv(args.post_deploy_eval, parse_dates=["timestamp"])
        step_post_deploy_monitor(post_deploy_df, v1_version, v2_version)

    print("\n[retrain] ✓ Retrain pipeline complete.")


if __name__ == "__main__":
    main()
