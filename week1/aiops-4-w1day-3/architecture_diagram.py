"""
W1-D3: Architecture Diagram — Anomaly Detection on Payment Service
Generate: architecture.png using Python diagrams library
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.onprem.monitoring import Grafana, Prometheus
from diagrams.onprem.queue import Kafka
from diagrams.onprem.compute import Server
from diagrams.onprem.logging import FluentBit, Loki
from diagrams.onprem.analytics import Flink
from diagrams.onprem.monitoring import Prometheus
from diagrams.onprem.tracing import Jaeger
from diagrams.onprem.client import Client
from diagrams.aws.storage import S3
from diagrams.programming.language import Python
from diagrams.generic.storage import Storage

graph_attr = {
    "fontsize": "16",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "ortho",
}

with Diagram(
    "AIOps Data Layer — Anomaly Detection on Payment Service",
    filename="architecture",
    outformat="png",
    graph_attr=graph_attr,
    direction="LR",
    show=False,
):

    # ── Layer 1: Services ──────────────────────────────────────
    with Cluster("Payment Services"):
        svc1 = Server("payment-service-1")
        svc2 = Server("payment-service-2")
        svc3 = Server("payment-service-3")

    # ── Layer 2: Collection ────────────────────────────────────
    with Cluster("Collection Layer\n(OpenTelemetry)"):
        otel = FluentBit("OTel Collector\n(sidecar)")

    with Cluster("Transport Layer"):
        kafka = Kafka("Kafka\n3 brokers / KRaft\n7-day retention")

    with Cluster("Processing Layer"):
        flink = Flink("Flink\nFeature Engineering\n(rolling mean, z-score)")

    with Cluster("Storage Layer"):
        with Cluster("Hot (0-7d)"):
            vm = Prometheus("VictoriaMetrics\n(metric TSDB)")
            loki = Loki("Loki\n(log store)")
            jaeger = Jaeger("Jaeger\n(trace store)")

        with Cluster("Cold (30d+)"):
            s3 = S3("S3 + Parquet\n(archive)")

    # ── Layer 6: Query / ML ────────────────────────────────────
    with Cluster("Query & ML Layer"):
        grafana = Grafana("Grafana\n(dashboard)")
        ml = Python("Anomaly Detection\n(Isolation Forest)")
        alert = Client("Alertmanager\n(PagerDuty)")

    # ── Edges ──────────────────────────────────────────────────
    # Services → OTel
    [svc1, svc2, svc3] >> Edge(label="metric+log+trace") >> otel

    # OTel → Kafka
    otel >> Edge(label="emit events") >> kafka

    # Kafka → Processing + Storage
    kafka >> Edge(label="metric stream") >> flink
    kafka >> Edge(label="logs") >> loki
    kafka >> Edge(label="traces") >> jaeger

    flink >> Edge(label="features") >> vm
    flink >> Edge(label="features") >> ml

    vm >> Edge(label="downsample\n30d+", style="dashed") >> s3
    loki >> Edge(label="archive\n30d+", style="dashed") >> s3

    vm >> grafana
    loki >> grafana
    jaeger >> grafana

    # ML → Alert
    ml >> Edge(label="anomaly score > 0.7") >> alert
