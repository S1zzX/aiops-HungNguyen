"""
W1-D3: Mock Streaming Pipeline
Data: NAB realKnownCause/machine_temperature_system_failure.csv
Architecture: CSV → Producer (mock Kafka) → queue.Queue → Consumer (mock Flink) → features.parquet
"""

import queue
import threading
import time
import math
import pandas as pd
from collections import deque
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
CSV_PATH    = "Dataset/machine_temperature_system_failure.csv"
OUTPUT_PATH = "features.parquet"
WINDOW_SIZE = 12        # 12 × 5min = 1-hour rolling window
EMIT_DELAY  = 0.0       # set > 0 để simulate real-time (VD: 0.001)
SENTINEL    = None      # signal consumer to stop

# ─────────────────────────────────────────────────────────────
# PRODUCER — đọc CSV và push từng row vào queue (mock Kafka)
# ─────────────────────────────────────────────────────────────

def producer(q: queue.Queue, csv_path: str):
    """
    Đọc CSV từng dòng, emit vào queue như Kafka producer.
    Mỗi row = 1 "message" với timestamp + value.
    """
    df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    total = len(df)
    print(f"[Producer] Loaded {total} rows from {csv_path}")
    print(f"[Producer] Time range: {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")

    for i, row in df.iterrows():
        event = {
            "timestamp": row["timestamp"].isoformat(),
            "value": row["value"],
            "seq": i,
        }
        q.put(event)
        if EMIT_DELAY > 0:
            time.sleep(EMIT_DELAY)

        if (i + 1) % 5000 == 0:
            print(f"[Producer] Emitted {i + 1}/{total} events...")

    q.put(SENTINEL)  # signal done
    print(f"[Producer] Done. Emitted {total} events -> queue.")


# ─────────────────────────────────────────────────────────────
# CONSUMER — đọc từ queue, extract features (mock Flink job)
# ─────────────────────────────────────────────────────────────

class FeatureExtractor:
    """Stateful window processor — giống Flink KeyedProcessFunction."""

    def __init__(self, window_size: int):
        self.window = deque(maxlen=window_size)
        self.prev_mean = None

    def process(self, event: dict) -> dict | None:
        val = event["value"]
        self.window.append(val)

        if len(self.window) < self.window_size:
            return None  # chưa đủ window

        values = list(self.window)
        n = len(values)

        # Rolling mean
        mean = sum(values) / n

        # Rolling std
        variance = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(variance)

        # Rate of change (so với window mean trước)
        roc = 0.0
        if self.prev_mean is not None and self.prev_mean != 0:
            roc = (mean - self.prev_mean) / self.prev_mean
        self.prev_mean = mean

        # Z-score của current value
        zscore = (val - mean) / (std + 1e-9)

        # Min / Max trong window
        win_min = min(values)
        win_max = max(values)

        # Hour of day (cyclical)
        ts = datetime.fromisoformat(event["timestamp"])
        hour = ts.hour + ts.minute / 60
        hour_sin = math.sin(2 * math.pi * hour / 24)
        hour_cos = math.cos(2 * math.pi * hour / 24)

        return {
            "timestamp":    event["timestamp"],
            "seq":          event["seq"],
            "value":        val,
            "rolling_mean": round(mean, 4),
            "rolling_std":  round(std, 4),
            "rolling_min":  round(win_min, 4),
            "rolling_max":  round(win_max, 4),
            "rate_of_change": round(roc, 6),
            "zscore":       round(zscore, 4),
            "hour_sin":     round(hour_sin, 4),
            "hour_cos":     round(hour_cos, 4),
        }

    @property
    def window_size(self):
        return self.window.maxlen


def consumer(q: queue.Queue, output_path: str):
    """
    Đọc từ queue, extract features, lưu ra parquet.
    Giống Flink consumer group đọc từ Kafka topic.
    """
    extractor = FeatureExtractor(window_size=WINDOW_SIZE)
    features_list = []
    processed = 0

    print(f"[Consumer] Started. Window size = {WINDOW_SIZE} points ({WINDOW_SIZE * 5} min)")

    while True:
        event = q.get()

        if event is SENTINEL:
            print(f"[Consumer] Received sentinel — stopping.")
            break

        features = extractor.process(event)
        if features:
            features_list.append(features)

        processed += 1
        if processed % 5000 == 0:
            print(f"[Consumer] Processed {processed} events, {len(features_list)} feature vectors so far...")

    # Save to parquet
    df = pd.DataFrame(features_list)
    df.to_parquet(output_path, index=False)
    print(f"[Consumer] Saved {len(df)} rows x {len(df.columns)} cols -> {output_path}")
    return df


# -------------------------------------------------------------
# MAIN - chay producer + consumer voi threading (bonus)
# -------------------------------------------------------------

def run_pipeline():
    print("=" * 60)
    print(f"AIOps Streaming Pipeline -- Machine Temperature")
    print(f"Data: {CSV_PATH} | Window: {WINDOW_SIZE}x5min = {WINDOW_SIZE*5}min")
    print("=" * 60)

    q = queue.Queue(maxsize=1000)  # buffer toi da 1000 messages

    # Chay producer + consumer tren 2 thread (simulate producer/consumer pattern)
    t_producer = threading.Thread(target=producer, args=(q, CSV_PATH), name="Producer")
    
    # Consumer chay tren main thread de collect ket qua de hon
    # (hoac co the dung thread + shared list neu muon full async)
    result_holder = {}

    def consumer_thread():
        result_holder["df"] = consumer(q, OUTPUT_PATH)

    t_consumer = threading.Thread(target=consumer_thread, name="Consumer")

    t0 = time.time()
    t_producer.start()
    t_consumer.start()

    t_producer.join()
    t_consumer.join()
    elapsed = time.time() - t0

    df = result_holder.get("df", pd.DataFrame())

    # -- Summary ----------------------------------------------
    print("\n" + "=" * 60)
    print("PIPELINE SUMMARY")
    print("=" * 60)
    print(f"  Elapsed time     : {elapsed:.2f}s")
    print(f"  Feature vectors  : {len(df)}")
    print(f"  Columns          : {list(df.columns)}")

    if len(df) > 0:
        print(f"\n  Value stats:")
        print(f"    mean = {df['value'].mean():.2f}")
        print(f"    std  = {df['value'].std():.2f}")
        print(f"    min  = {df['value'].min():.2f}")
        print(f"    max  = {df['value'].max():.2f}")

        # High z-score = potential anomaly
        anomalies = df[df["zscore"].abs() > 3]
        print(f"\n  High z-score events (|z| > 3): {len(anomalies)}")
        if len(anomalies) > 0:
            print(anomalies[["timestamp", "value", "rolling_mean", "zscore"]].head(5).to_string(index=False))

        print(f"\n  Sample (first 3 rows):")
        print(df[["timestamp", "value", "rolling_mean", "rolling_std", "zscore"]].head(3).to_string(index=False))

    print(f"\nDone. Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    run_pipeline()
