"""
W1-D3: Cost Model — Build vs Buy cho 3 scale tiers
Payment Service AIOps Platform
"""


def calculate_costs(tier_name: str, n_services: int, log_gb_per_day: float, metric_events_per_sec: float) -> dict:
    """
    Tính monthly cost cho 1 tier.
    
    Pricing assumptions (AWS us-east-1, 2024):
    - S3 Standard: $0.023/GB/month
    - S3 Glacier: $0.004/GB/month  
    - EC2 m5.2xlarge (8 vCPU, 32GB): $0.384/hr = ~$278/month
    - EC2 r5.xlarge (4 vCPU, 32GB RAM): $0.252/hr = ~$182/month
    - EBS gp3: $0.08/GB/month
    - Network egress: $0.09/GB
    """

    costs = {}

    # ─── STORAGE ───────────────────────────────────────────────

    # Metric storage (VictoriaMetrics)
    # 1 data point ≈ 0.8 bytes sau compression (VM rất efficient)
    metric_bytes_per_month = metric_events_per_sec * 0.8 * 86400 * 30
    metric_gb_month = metric_bytes_per_month / 1e9
    # VM cần ~1 GB RAM per 10M active time series
    # EBS cho storage
    metric_ebs_gb = metric_gb_month * 2  # 2x replication
    costs["metric_storage"] = metric_ebs_gb * 0.08

    # Log storage (Loki hot 7 ngày + S3 warm/cold)
    log_gb_month = log_gb_per_day * 30
    log_hot_gb = log_gb_per_day * 7   # 7 ngày hot
    log_warm_gb = log_gb_per_day * 23  # 23 ngày warm (S3)
    log_cold_gb = log_gb_per_day * 30 * 11  # 11 tháng cold (S3 Glacier, giả sử giữ 1 năm)

    # Loki hot: cần EBS, compress ~3x
    costs["log_hot_storage"] = (log_hot_gb / 3) * 0.08
    # S3 warm
    costs["log_warm_storage"] = (log_warm_gb / 3) * 0.023
    # S3 Glacier cold
    costs["log_cold_storage"] = (log_cold_gb / 5) * 0.004

    # Trace storage (Jaeger, 1% sampling)
    # 1 trace ~10KB, 100 req/sec per service
    requests_per_sec = n_services * 100  # rough estimate
    sampled_traces_per_sec = requests_per_sec * 0.01
    trace_gb_month = sampled_traces_per_sec * 10_000 * 86400 * 30 / 1e9
    costs["trace_storage"] = trace_gb_month * 0.08  # EBS, giữ 7 ngày

    # ─── COMPUTE ───────────────────────────────────────────────

    # Kafka (3 brokers, size phụ thuộc throughput)
    if metric_events_per_sec <= 100_000:
        kafka_nodes = 3
        kafka_instance = "m5.xlarge"  # $0.192/hr
        kafka_cost_per_node = 0.192 * 24 * 30
    elif metric_events_per_sec <= 1_000_000:
        kafka_nodes = 3
        kafka_instance = "m5.2xlarge"  # $0.384/hr
        kafka_cost_per_node = 0.384 * 24 * 30
    else:
        kafka_nodes = 6
        kafka_instance = "m5.4xlarge"  # $0.768/hr
        kafka_cost_per_node = 0.768 * 24 * 30
    costs["kafka_compute"] = kafka_nodes * kafka_cost_per_node

    # Kafka EBS storage (7 ngày retention)
    kafka_throughput_gb_per_day = (metric_events_per_sec * 100) / 1e9 * 86400  # 100 bytes/event
    kafka_ebs_gb = kafka_throughput_gb_per_day * 7 * 3  # 3x replication
    costs["kafka_storage"] = kafka_ebs_gb * 0.08

    # Flink (stream processing)
    if metric_events_per_sec <= 100_000:
        flink_nodes = 2
        flink_cost_per_node = 0.192 * 24 * 30  # m5.xlarge
    elif metric_events_per_sec <= 1_000_000:
        flink_nodes = 4
        flink_cost_per_node = 0.384 * 24 * 30  # m5.2xlarge
    else:
        flink_nodes = 8
        flink_cost_per_node = 0.768 * 24 * 30  # m5.4xlarge
    costs["flink_compute"] = flink_nodes * flink_cost_per_node

    # VictoriaMetrics
    if metric_events_per_sec <= 100_000:
        vm_nodes = 1
        vm_cost_per_node = 0.252 * 24 * 30  # r5.xlarge (RAM-heavy)
    elif metric_events_per_sec <= 1_000_000:
        vm_nodes = 2
        vm_cost_per_node = 0.504 * 24 * 30  # r5.2xlarge
    else:
        vm_nodes = 4
        vm_cost_per_node = 1.008 * 24 * 30  # r5.4xlarge
    costs["victoriametrics_compute"] = vm_nodes * vm_cost_per_node

    # Loki
    if log_gb_per_day <= 50:
        loki_nodes = 1
        loki_cost_per_node = 0.192 * 24 * 30
    elif log_gb_per_day <= 500:
        loki_nodes = 2
        loki_cost_per_node = 0.384 * 24 * 30
    else:
        loki_nodes = 4
        loki_cost_per_node = 0.768 * 24 * 30
    costs["loki_compute"] = loki_nodes * loki_cost_per_node

    # Jaeger
    jaeger_cost = 0.192 * 24 * 30 * max(1, n_services // 50)
    costs["jaeger_compute"] = jaeger_cost

    # Grafana + Alertmanager (nhỏ)
    costs["grafana_alertmanager"] = 0.096 * 24 * 30 * 2  # 2x m5.large

    # ML inference (CPU, batch mỗi 1 phút)
    costs["ml_compute"] = 0.192 * 24 * 30  # 1x m5.xlarge đủ cho inference

    # ─── NETWORK ───────────────────────────────────────────────
    # Egress cross-AZ: ~10% total data volume
    total_data_gb_month = log_gb_month + metric_gb_month / 1e3 + trace_gb_month
    costs["network_egress"] = total_data_gb_month * 0.1 * 0.09

    # ─── OPERATIONS ─────────────────────────────────────────────
    # SRE time cost (không tính vào infra nhưng note lại)
    # Small: 0.5 SRE, Medium: 1.5 SRE, Large: 3 SRE
    # Không add vào infra cost để fair comparison

    # ─── DATADOG SaaS comparison ────────────────────────────────
    # Datadog pricing (2024):
    # Host: $27/host/month (Infrastructure)
    # Log: $0.10/GB ingested + $0.0023/GB/month indexed
    # APM: $40/host/month (trace)
    # Custom metric: $0.05/metric/month
    dd_infrastructure = n_services * 27  # 1 host per service (roughly)
    dd_log = log_gb_per_day * 30 * 0.10  # ingestion
    dd_log_index = log_gb_per_day * 7 * 0.0023 * 1024  # indexed (MB basis) — 7 ngày
    dd_apm = n_services * 40
    dd_metrics = min(metric_events_per_sec / 1000, 100000) * 0.05  # custom metrics estimate
    datadog_total = dd_infrastructure + dd_log + dd_log_index + dd_apm + dd_metrics

    build_total = sum(costs.values())

    return {
        "tier": tier_name,
        "specs": {
            "services": n_services,
            "log_gb_per_day": log_gb_per_day,
            "metric_events_per_sec": f"{metric_events_per_sec:,.0f}",
        },
        "build_breakdown": costs,
        "build_total": build_total,
        "datadog_total": datadog_total,
        "savings_buy_vs_build": datadog_total - build_total,
    }


def print_cost_report(result: dict):
    tier = result["tier"]
    specs = result["specs"]
    breakdown = result["build_breakdown"]
    build_total = result["build_total"]
    dd_total = result["datadog_total"]

    print(f"\n{'='*65}")
    print(f"  TIER: {tier.upper()}")
    print(f"  {specs['services']} services | {specs['log_gb_per_day']} GB log/day | {specs['metric_events_per_sec']} metric events/sec")
    print(f"{'='*65}")

    print(f"\n  {'Component':<35} {'Monthly Cost':>12}")
    print(f"  {'-'*47}")

    categories = {
        "Storage": ["metric_storage", "log_hot_storage", "log_warm_storage", "log_cold_storage", "trace_storage", "kafka_storage"],
        "Compute": ["kafka_compute", "flink_compute", "victoriametrics_compute", "loki_compute", "jaeger_compute", "grafana_alertmanager", "ml_compute"],
        "Network": ["network_egress"],
    }

    for category, keys in categories.items():
        cat_total = sum(breakdown.get(k, 0) for k in keys)
        print(f"\n  {category}")
        for k in keys:
            if k in breakdown and breakdown[k] > 0:
                label = k.replace("_", " ").title()
                print(f"    {label:<33} ${breakdown[k]:>10,.0f}")
        print(f"    {'Subtotal':<33} ${cat_total:>10,.0f}")

    print(f"\n  {'-'*47}")
    print(f"  {'BUILD TOTAL (self-host)':<35} ${build_total:>10,.0f}/month")
    print(f"  {'DATADOG SaaS':<35} ${dd_total:>10,.0f}/month")
    print(f"  {'-'*47}")

    if dd_total > build_total:
        savings = dd_total - build_total
        print(f"  => Build saves ${savings:,.0f}/month vs Datadog ({savings/dd_total*100:.0f}% cheaper)")
        print(f"  => But requires ~{max(0.5, specs['services']//100 * 1.5):.1f} dedicated SRE to operate stack")
    else:
        savings = build_total - dd_total
        print(f"  => Datadog saves ${savings:,.0f}/month vs self-host at this scale")
        print(f"  => Plus 0 SRE overhead for infrastructure ops")


def main():
    print("=" * 62)
    print("   AIOps Platform -- Cost Model (Build vs Buy)")
    print("   Use case: Anomaly Detection on Payment Service")
    print("=" * 62)

    tiers = [
        ("Small",  10,   50,      100_000),
        ("Medium", 100,  500,   1_000_000),
        ("Large",  1000, 5000, 10_000_000),
    ]

    results = []
    for name, services, log_gb, metric_eps in tiers:
        result = calculate_costs(name, services, log_gb, metric_eps)
        results.append(result)
        print_cost_report(result)

    # Summary comparison table
    print(f"\n\n{'='*65}")
    print("  SUMMARY COMPARISON TABLE")
    print(f"{'='*65}")
    print(f"  {'Tier':<10} {'Build/month':>12} {'Datadog/month':>14} {'Verdict':<25}")
    print(f"  {'-'*60}")
    for r in results:
        build = r["build_total"]
        dd = r["datadog_total"]
        if dd < build * 1.5 and r["specs"]["services"] <= 50:
            verdict = "=> BUY (Datadog)"
        elif dd > build * 2:
            verdict = "=> BUILD (self-host)"
        else:
            verdict = "=> BUY or hybrid"
        print(f"  {r['tier']:<10} ${build:>10,.0f}   ${dd:>12,.0f}   {verdict}")

    print(f"\n  Note: Build cost = infrastructure only.")
    print(f"  Datadog cost = subscription. Neither includes eng salary.")
    print(f"\n  Rule of thumb:")
    print(f"  < 50 services  => Buy (Datadog), time-to-value 1-2 weeks")
    print(f"  50-500 services => Hybrid (Datadog for quick win, migrate hot paths)")
    print(f"  > 500 services  => Build (scale economics justify infra investment)")


if __name__ == "__main__":
    main()
