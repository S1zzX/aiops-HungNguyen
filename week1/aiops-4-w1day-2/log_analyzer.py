#!/usr/bin/env python3
"""
log_analyzer.py — Mini Log Analyzer
Usage: python log_analyzer.py <logfile>

Output:
  - Total lines & unique templates
  - Top-5 templates (count + %)
  - Spike templates in last 1 hour
  - New templates (first appeared in last 1 hour)
"""
import sys, re, os
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timedelta

try:
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig
except ImportError:
    print("ERROR: drain3 not installed. Run: pip install drain3")
    sys.exit(1)


BANNER = """
╔══════════════════════════════════════════════════════════════╗
║            Mini Log Analyzer  (W1-D2 AIOps)               ║
╚══════════════════════════════════════════════════════════════╝
"""


def preprocess(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^\d{6}\s+\d{6}\s+", "", line)
    line = re.sub(r"\[\d{4}-\d{2}-\d{2}T[\d:.Z]+\]\s*", "", line)
    line = re.sub(r"\b(INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL|FATAL)\b\s*", "", line)
    line = re.sub(r"[\w.$]+:\s*", "", line, count=1)
    return line.strip()


def parse_timestamp(line: str):
    m = re.search(r"(\d{6})\s+(\d{6})", line)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%y%m%d%H%M%S")
        except:
            pass
    m2 = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
    if m2:
        try:
            return datetime.strptime(m2.group(1), "%Y-%m-%dT%H:%M:%S")
        except:
            pass
    return None


def analyze(logfile: str, sim_th: float = 0.5):
    path = Path(logfile)
    if not path.exists():
        print(f"ERROR: File not found: {logfile}")
        sys.exit(1)

    # -- Setup Drain3 --
    cfg = TemplateMinerConfig()
    cfg.drain_sim_th = sim_th
    cfg.drain_depth  = 4
    cfg.parametrize_numeric_tokens = True
    miner = TemplateMiner(config=cfg)

    # -- Parse --
    records = []   # (timestamp, cluster_id, template, is_new)
    n_total = 0
    n_skip  = 0

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            n_total += 1
            content = preprocess(raw)
            if not content:
                n_skip += 1
                continue
            ts = parse_timestamp(raw)
            result = miner.add_log_message(content)
            is_new = (result["change_type"] == "cluster_created")
            records.append((ts, result["cluster_id"], result["template_mined"], is_new))

    # -- Stats --
    n_parsed    = len(records)
    n_templates = len(miner.drain.clusters)

    # Template counts
    tmpl_counts = Counter(r[1] for r in records)
    tmpl_text   = {c.cluster_id: c.get_template() for c in miner.drain.clusters}

    # -- Time-based analysis --
    valid_ts = [(ts, cid, tmpl, is_new) for ts, cid, tmpl, is_new in records if ts is not None]
    spike_templates = []
    new_in_last_hour = []

    if valid_ts:
        max_ts = max(r[0] for r in valid_ts)
        cutoff = max_ts - timedelta(hours=1)

        recent = [(ts, cid, tmpl, is_new) for ts, cid, tmpl, is_new in valid_ts if ts >= cutoff]
        older  = [(ts, cid, tmpl, is_new) for ts, cid, tmpl, is_new in valid_ts if ts < cutoff]

        recent_counts = Counter(r[1] for r in recent)
        older_counts  = Counter(r[1] for r in older)
        older_total   = max(len(older), 1)
        recent_total  = max(len(recent), 1)

        # Spike: recent rate / baseline rate > 3
        for cid, cnt in recent_counts.items():
            recent_rate   = cnt / recent_total
            baseline_rate = older_counts.get(cid, 0) / older_total
            if baseline_rate > 0:
                ratio = recent_rate / baseline_rate
                if ratio > 3:
                    spike_templates.append((cid, cnt, ratio, tmpl_text.get(cid, "?")))

        # New templates in last hour
        new_in_last_hour = list(set(
            (cid, tmpl) for ts, cid, tmpl, is_new in recent if is_new
        ))

    # -- Print Results --
    print(BANNER)
    print(f"  File    : {path.name}")
    print(f"  Size    : {path.stat().st_size / 1024:.1f} KB")
    print()

    print("┌─────────────────────────────────────────┐")
    print("│  SUMMARY                                │")
    print("├─────────────────────────────────────────┤")
    print(f"│  Total lines      : {n_total:<20,}│")
    print(f"│  Parsed lines     : {n_parsed:<20,}│")
    print(f"│  Unique templates : {n_templates:<20,}│")
    print("└─────────────────────────────────────────┘")

    print()
    print("  TOP-5 TEMPLATES")
    print("  " + "-" * 64)
    for rank, (cid, cnt) in enumerate(tmpl_counts.most_common(5), 1):
        pct = cnt / n_parsed * 100 if n_parsed > 0 else 0
        tmpl_str = tmpl_text.get(cid, "?")[:55]
        bar = "█" * int(pct / 2)
        print(f"  #{rank} T-{cid:03d}  {cnt:6,}  ({pct:5.1f}%)  {bar}")
        print(f"       {tmpl_str}")

    print()
    print("  SPIKE TEMPLATES (last 1h vs baseline, ratio > 3x)")
    print("  " + "-" * 64)
    if spike_templates:
        for cid, cnt, ratio, tmpl in sorted(spike_templates, key=lambda x: -x[2]):
            print(f"  🔥 T-{cid:03d}  count={cnt}  ratio={ratio:.1f}x")
            print(f"       {tmpl[:60]}")
    else:
        print("  ✅ No spike detected")

    print()
    print("  NEW TEMPLATES (first appeared in last 1h)")
    print("  " + "-" * 64)
    if new_in_last_hour:
        for cid, tmpl in new_in_last_hour:
            print(f"  ⚡ T-{cid:03d}: {tmpl[:60]}")
    else:
        print("  ✅ No new templates")

    print()
    return {
        "total_lines": n_total,
        "n_templates": n_templates,
        "top5": tmpl_counts.most_common(5),
        "spike_templates": spike_templates,
        "new_templates": new_in_last_hour,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python log_analyzer.py <logfile>")
        sys.exit(1)
    analyze(sys.argv[1])
