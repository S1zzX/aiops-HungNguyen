# Evidence-Driven Remediation Engine

## Setup

```bash
pip install pyyaml
```

No other dependencies required (pure Python stdlib + pyyaml).

## How to run

```bash
# Run on a single incident:
python engine.py decide --incident eval/E01.json \
                        --history incidents_history.json \
                        --actions actions.yaml

# Run all 8 eval incidents:
for i in 01 02 03 04 05 06 07 08; do
  python engine.py decide --incident eval/E$i.json \
                          --history incidents_history.json \
                          --actions actions.yaml
done

# Grade:
python grade.py --audit audit.jsonl --expected eval/expected.json
```

Expected output: `Correct: 8/8, Forbidden: 0/8, Missing: 0/8`

## Architecture

```
engine.py       ← CLI entry point, orchestrates the 3 layers
features.py     ← Layer 1: log template extraction (Drain-lite) + trace features + affected services
retrieval.py    ← Layer 2: weighted Jaccard similarity + outcome-weighted kNN voting
decision.py     ← Layer 3: EV-based action selection + blast-radius gate + OOD escalation
```

## Key design decisions

- **Similarity:** Weighted Jaccard (log tokens 45% + trace destination service 35% + affected services 20%)
- **Voting:** sim² × outcome_weight amplifies dominant neighbors over weak clusters
- **OOD threshold:** 0.22 — incidents with max neighbor similarity below this escalate to page_oncall
- **EV formula:** `ev_p × 1.0 - (1-ev_p) × blast_penalty - 0.1 × cost_penalty`
  where `ev_p = 0.6 × vote_frac + 0.4 × p_success`
- **page_oncall is never chosen by naive zero-cost math** — it is only selected when is_ood=True, no auto-candidates exist, or blast gate triggers


