#!/usr/bin/env python3
"""Break-even cost model for an AIOps platform (W3-D3 §8, §9.7).

Usage:
    python cost_model.py

is_worth_it() signature and verdict thresholds match the W3-D3 spec exactly:
    roi > 1.5            -> "worth_it"
    1.0 < roi <= 1.5      -> "marginal"
    roi <= 1.0            -> "not_worth_it"
"""


def is_worth_it(
    num_services: int,
    incidents_per_month: int,
    avg_incident_duration_hours: float,
    downtime_cost_per_hour: float,
    expected_mttr_reduction_pct: float = 0.4,
    aiops_monthly_cost: float = 15_000,
) -> dict:
    """
    Returns:
      {
        "monthly_value": float,
        "monthly_cost": float,
        "roi": float,
        "payback_months": float,  # or float('inf')
        "verdict": "worth_it" | "marginal" | "not_worth_it"
      }
    Verdict rule:
      roi > 1.5 -> worth_it
      1.0 < roi <= 1.5 -> marginal
      roi <= 1.0 -> not_worth_it
    """
    monthly_downtime_hours = incidents_per_month * avg_incident_duration_hours
    monthly_value = (
        monthly_downtime_hours
        * expected_mttr_reduction_pct
        * downtime_cost_per_hour
    )
    roi = monthly_value / aiops_monthly_cost if aiops_monthly_cost else float("inf")
    payback_months = (
        aiops_monthly_cost / monthly_value if monthly_value > 0 else float("inf")
    )

    if roi > 1.5:
        verdict = "worth_it"
    elif roi > 1.0:
        verdict = "marginal"
    else:
        verdict = "not_worth_it"

    return {
        "monthly_value": monthly_value,
        "monthly_cost": aiops_monthly_cost,
        "roi": roi,
        "payback_months": payback_months,
        "verdict": verdict,
    }


if __name__ == "__main__":
    # Scenario 1 — small shop, few incidents: from §8.4 example table
    print("Scenario 1: 20 services, 2 incidents/mo x 1h, $10k/h downtime")
    print(is_worth_it(
        num_services=20, incidents_per_month=2,
        avg_incident_duration_hours=1, downtime_cost_per_hour=10_000,
        aiops_monthly_cost=15_000,
    ))
    print()

    # Scenario 2 — mid-size platform, right-sized for AIOps: from §8.4 example table
    print("Scenario 2: 100 services, 5 incidents/mo x 2h, $20k/h downtime")
    print(is_worth_it(
        num_services=100, incidents_per_month=5,
        avg_incident_duration_hours=2, downtime_cost_per_hour=20_000,
        aiops_monthly_cost=25_000,
    ))
    print()

    # Scenario 3 — my own scenario: a mid-tier e-commerce checkout stack,
    # modeled on the reproduction in this exercise (api-gateway + checkout
    # path). I picked $15k/hour as the downtime-cost input because it sits
    # inside the "E-commerce mid-tier" band from §8.2 ($5k-$50k/hour) — this
    # company is bigger than a small storefront (it runs ~60 services and a
    # 24/7 on-call rotation) but nowhere near Amazon-scale, so I anchored
    # near the middle-upper end of that band rather than the extremes.
    # incidents_per_month=4 and avg_incident_duration_hours=1.5 reflect a
    # platform that has decent but not great reliability (roughly 1
    # incident/week, each resolved within a couple hours) — consistent with
    # having SLOs and a postmortem culture (§8.5: AIOps is only worth
    # evaluating once those exist).
    print("Scenario 3 (mine): mid-tier e-commerce checkout, 60 services, "
          "4 incidents/mo x 1.5h, $15k/h downtime, $18k/mo AIOps cost")
    print(is_worth_it(
        num_services=60, incidents_per_month=4,
        avg_incident_duration_hours=1.5, downtime_cost_per_hour=15_000,
        aiops_monthly_cost=18_000,
    ))
