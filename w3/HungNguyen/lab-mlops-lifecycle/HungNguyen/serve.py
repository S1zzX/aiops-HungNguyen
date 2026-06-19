"""
serve.py — FastAPI model server

Endpoints:
    POST /predict               → {prediction, score, version}
    GET  /health/active-version → {version, alias, model_name}
    POST /reload                → reload model from registry @production
    GET  /metrics               → Prometheus metrics

Usage:
    python serve.py
    # or
    uvicorn serve:app --host 0.0.0.0 --port 8000
"""

import os
import time
from contextlib import asynccontextmanager
from typing import List

import mlflow
import mlflow.pyfunc
import numpy as np
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException
from mlflow import MlflowClient
from pydantic import BaseModel

# Prometheus
from prometheus_client import Counter, Histogram, Gauge, make_asgi_app

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
MODEL_NAME = "anomaly-detector"
MODEL_ALIAS = "production"
FEATURE_COLS = ["latency_p99", "error_rate", "rps"]
TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")

mlflow.set_tracking_uri(TRACKING_URI)

# ─────────────────────────────────────────────
# Prometheus metrics
# ─────────────────────────────────────────────
def _get_or_create(metric_cls, name, doc, **kwargs):
    try:
        return metric_cls(name, doc, **kwargs)
    except ValueError:
        from prometheus_client import REGISTRY
        return REGISTRY._names_to_collectors.get(name)

REQUEST_COUNT = _get_or_create(Counter, "serve_request_total", "Total /predict requests")
REQUEST_LATENCY = _get_or_create(
    Histogram,
    "serve_predict_latency_seconds",
    "Latency of /predict",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)
ACTIVE_VERSION = _get_or_create(Gauge, "serve_active_version", "Currently loaded model version")

# ─────────────────────────────────────────────
# Global model state
# ─────────────────────────────────────────────
state: dict = {
    "model": None,
    "version": None,
    "run_id": None,
}


def load_model_from_registry():
    """Load model from MLflow registry alias @production."""
    client = MlflowClient()
    model_uri = f"models:/{MODEL_NAME}@{MODEL_ALIAS}"

    model = mlflow.pyfunc.load_model(model_uri)

    # Get version number
    mv = client.get_model_version_by_alias(MODEL_NAME, MODEL_ALIAS)
    version = mv.version
    run_id = mv.run_id

    state["model"] = model
    state["version"] = version
    state["run_id"] = run_id
    ACTIVE_VERSION.set(float(version))

    print(f"[serve] Loaded {MODEL_NAME} version={version} (run_id={run_id})")
    return version


# ─────────────────────────────────────────────
# Lifespan (startup / shutdown)
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_model_from_registry()
    yield
    print("[serve] Shutting down.")


app = FastAPI(title="Anomaly Detector API", lifespan=lifespan)

# Mount Prometheus metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


# ─────────────────────────────────────────────
# Request / Response schemas
# ─────────────────────────────────────────────
class PredictRequest(BaseModel):
    features: List[List[float]]  # list of rows, each row = [latency_p99, error_rate, rps]


class PredictResponse(BaseModel):
    predictions: List[int]
    scores: List[float]
    version: str


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────
@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    if state["model"] is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    start = time.time()
    REQUEST_COUNT.inc()

    df = pd.DataFrame(req.features, columns=FEATURE_COLS)
    raw = state["model"].predict(df)

    # IsolationForest via pyfunc returns numpy array of -1 / 1
    preds = (np.array(raw) == -1).astype(int).tolist()

    # Scores: use the underlying sklearn model's decision_function if accessible
    try:
        sklearn_model = state["model"]._model_impl
        scores = (-sklearn_model.decision_function(df.values)).tolist()
    except Exception:
        scores = [float(p) for p in preds]

    elapsed = time.time() - start
    REQUEST_LATENCY.observe(elapsed)

    return PredictResponse(
        predictions=preds,
        scores=scores,
        version=str(state["version"]),
    )


@app.get("/health/active-version")
def active_version():
    if state["version"] is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {
        "model_name": MODEL_NAME,
        "alias": MODEL_ALIAS,
        "version": state["version"],
        "run_id": state["run_id"],
        "status": "ok",
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": state["version"]}


@app.post("/reload")
def reload_model():
    """Hot-reload model from registry — called by retrain.py after alias swap."""
    try:
        old_version = state["version"]
        new_version = load_model_from_registry()
        return {
            "status": "reloaded",
            "old_version": old_version,
            "new_version": new_version,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
