#!/usr/bin/env python3
"""
closed_loop.py — Closed-Loop Auto-Remediation Orchestrator
Author  : HungNguyen
Pattern : Detect → Decide → Act → Verify → Rollback

Covers all 6 acceptance scenarios:
  1. Action success
  2. Action fail → auto-rollback
  3. Circuit breaker (3 consecutive failures → HALT)
  4. Multi-step transactional rollback
  5. Concurrent alert race (per-service mutex)
  6. LLM hallucination defense (runbook registry validation)
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# ── Ensure engine.* is importable regardless of working directory ────────────
sys.path.insert(0, str(Path(__file__).parent))

from engine.metrics import (
    action_counter,
    blast_radius_gauge,
    circuit_breaker_gauge,
    mutex_gauge,
    start_metrics_server,
    verify_status_gauge,
)
from engine.safety import BlastRadiusGuard, CircuitBreaker, ServiceMutex
from engine.verify import verify_service


# ─────────────────────────────────────────────────────────────────────────────
# Audit-capable logger
# Writes structured JSON events to stdout AND an optional audit file
# (set AUDIT_LOG_PATH=/path/to/audit_log.jsonl to enable file logging)
# ─────────────────────────────────────────────────────────────────────────────
class AuditLogger:
    def __init__(self) -> None:
        self._audit_path: str | None = os.environ.get("AUDIT_LOG_PATH")
        self._lock = threading.Lock()

    def _emit(self, level: str, event_type: str, **kwargs) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event_type": event_type,
            **kwargs,
        }
        line = json.dumps(record)
        print(line, flush=True)
        if self._audit_path:
            with self._lock:
                with open(self._audit_path, "a") as fh:
                    fh.write(line + "\n")

    def info(self, event_type: str, **kwargs) -> None:
        self._emit("INFO", event_type, **kwargs)

    def warning(self, event_type: str, **kwargs) -> None:
        self._emit("WARNING", event_type, **kwargs)

    def error(self, event_type: str, **kwargs) -> None:
        self._emit("ERROR", event_type, **kwargs)


log = AuditLogger()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_config(config_path: str) -> dict:
    with open(config_path) as fh:
        return yaml.safe_load(fh)


def load_baseline(config: dict, config_dir: Path) -> dict:
    baseline_path = config_dir / config["baseline_path"]
    with open(baseline_path) as fh:
        return json.load(fh)


def poll_alertmanager(url: str) -> list[dict]:
    """Fetch all currently active alerts from Alertmanager /api/v2/alerts."""
    try:
        resp = requests.get(f"{url}/api/v2/alerts", timeout=5)
        resp.raise_for_status()
        return [a for a in resp.json() if a.get("status", {}).get("state") == "active"]
    except Exception as exc:
        log.error("ALERTMANAGER_POLL_ERROR", error=str(exc))
        return []


def run_script(
    script: str,
    service: str,
    dry_run: bool = False,
    timeout: int = 30,
    extra_args: list | None = None,
) -> tuple[bool, str]:
    """
    Execute a bash runbook script.
    Returns (success: bool, combined_output: str).
    """
    cmd = ["bash", script, "--service", service]
    if dry_run:
        cmd.append("--dry-run")
    if extra_args:
        cmd.extend(extra_args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    except Exception as exc:
        return False, str(exc)


def validate_runbook(runbook: str, registry: list[str]) -> bool:
    """
    Hallucination / bad-decision defense (Scenario 6).
    Only scripts explicitly listed in runbook_registry may be executed.
    """
    return runbook in registry


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
class ClosedLoopOrchestrator:
    def __init__(
        self,
        config: dict,
        config_dir: Path,
        baseline: dict,
        dry_run_mode: bool = False,
    ) -> None:
        self.config = config
        self.config_dir = config_dir
        self.baseline = baseline
        self.dry_run_mode = dry_run_mode

        # ── Safety components ─────────────────────────────────────────
        br = config["blast_radius"]
        self.guard = BlastRadiusGuard(
            max_per_minute=br["max_actions_per_minute"],
            max_restarts_per_hour=br["max_restarts_per_service_per_hour"],
        )
        self.mutex = ServiceMutex()  # per-service lock (Scenario 5)

        # Per-service circuit breakers (Scenario 3)
        self._cb_threshold = config["circuit_breaker"]["consecutive_failure_threshold"]
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._cb_lock = threading.Lock()

        # In-progress dedup: prevent re-processing the same (alert, service) pair
        self._in_progress: set[tuple[str, str]] = set()
        self._ip_lock = threading.Lock()

        # ── Config maps ───────────────────────────────────────────────
        self.runbook_map: dict[str, str] = config.get("runbook_map", {})
        self.rollback_map: dict[str, str] = config.get("rollback_map", {})
        # runbook_registry defaults to all values in runbook_map if not set
        self.runbook_registry: list[str] = config.get(
            "runbook_registry", list(self.runbook_map.values())
        )
        # multi_step_map: alertname → list of [script_rel, *extra_args]
        self.multi_step_map: dict[str, list] = config.get("multi_step_map", {})
        # multi_step_rollback_map: alertname → list of [script_rel, *extra_args] in rollback order
        self.multi_step_rollback_map: dict[str, list] = config.get(
            "multi_step_rollback_map", {}
        )

        # ── Timing ───────────────────────────────────────────────────
        self.prometheus_url: str = config["prometheus_url"]
        self.runbook_timeout: int = config.get("runbook_timeout_seconds", 30)
        vt = baseline["verify_thresholds"]
        self.verify_timeout: int = vt["verify_timeout_seconds"]
        self.verify_poll_interval: int = vt["verify_poll_interval_seconds"]
        self.verify_min_samples: int = vt["verify_min_samples"]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_cb(self, service: str) -> CircuitBreaker:
        with self._cb_lock:
            if service not in self._circuit_breakers:
                self._circuit_breakers[service] = CircuitBreaker(self._cb_threshold)
            return self._circuit_breakers[service]

    def _resolve(self, rel: str) -> str:
        """Resolve a relative script path against the config directory."""
        return str(self.config_dir / rel)

    # ── Public entry-point (one thread per alert) ─────────────────────────────

    def process_alert(self, alert: dict) -> None:
        """
        Full Detect → Decide → Act → Verify → Rollback pipeline.
        Called in its own daemon thread for concurrent handling (Scenario 5).
        """
        labels = alert.get("labels", {})
        alertname = labels.get("alertname", "unknown")
        # Alertmanager labels service as 'service' or falls back to 'job'
        service = labels.get("service", labels.get("job", "unknown"))
        severity = labels.get("severity", "unknown")

        # ── 1. DETECT ─────────────────────────────────────────────────────
        log.info(
            "ALERT_DETECTED",
            alertname=alertname,
            service=service,
            severity=severity,
        )

        # ── Dedup guard ────────────────────────────────────────────────────
        key = (alertname, service)
        with self._ip_lock:
            if key in self._in_progress:
                log.info("ALERT_DEDUP_SKIP", alertname=alertname, service=service)
                return
            self._in_progress.add(key)

        try:
            self._acquire_and_handle(alertname, service, severity)
        finally:
            with self._ip_lock:
                self._in_progress.discard(key)

    # ── Step 2: per-service mutex (Scenario 5) ────────────────────────────────

    def _acquire_and_handle(
        self, alertname: str, service: str, severity: str
    ) -> None:
        if not self.mutex.try_acquire(service):
            # Same service already handling a runbook — log and skip
            log.warning("SERVICE_LOCK_BUSY", alertname=alertname, service=service)
            mutex_gauge.labels(service=service).set(1)
            return

        mutex_gauge.labels(service=service).set(1)
        try:
            self._pipeline(alertname, service, severity)
        finally:
            self.mutex.release(service)
            mutex_gauge.labels(service=service).set(0)

    # ── Steps 3-9: safety pipeline ────────────────────────────────────────────

    def _pipeline(self, alertname: str, service: str, severity: str) -> None:
        cb = self._get_cb(service)

        # ── 3. Circuit-breaker gate ────────────────────────────────────────
        if cb.is_open():
            log.error(
                "CIRCUIT_BREAKER_HALT",
                alertname=alertname,
                service=service,
                message="Circuit is OPEN — automation halted. Manual reset required.",
            )
            circuit_breaker_gauge.labels(service=service).set(1)
            return

        # ── 4. DECIDE: alert → runbook + registry validation ───────────────
        # Check multi_step_map first — if matched, skip runbook_map lookup
        if alertname in self.multi_step_map:
            self._multi_step(alertname, service, self._get_cb(service))
            return
        runbook_rel = self.runbook_map.get(alertname)
        if not runbook_rel:
            log.warning("NO_RUNBOOK_MAPPED", alertname=alertname, service=service)
            return

        # Hallucination / bad-LLM-decision defense (Scenario 6)
        if not validate_runbook(runbook_rel, self.runbook_registry):
            log.error(
                "DECISION_VALIDATION_FAILED",
                alertname=alertname,
                service=service,
                bad_runbook=runbook_rel,
                raw_decision=runbook_rel,
                action="escalate_no_auto_action",
            )
            # NOTE: validation failure does NOT increment the circuit breaker
            return

        log.info(
            "DECIDE_RUNBOOK",
            alertname=alertname,
            service=service,
            runbook=runbook_rel,
        )

        # ── 5. Blast-radius check ──────────────────────────────────────────
        allowed, reason = self.guard.check(service)
        blast_radius_gauge.labels(service=service).set(
            max(0, self.config["blast_radius"]["max_actions_per_minute"] - 1)
        )

        if not allowed:
            log.warning(
                "BLAST_RADIUS_EXCEEDED",
                alertname=alertname,
                service=service,
                reason=reason,
            )
            return
        log.info("BLAST_RADIUS_OK", alertname=alertname, service=service)

        # ── 6. Multi-step transactional deploy? ────────────────────────────
        if alertname in self.multi_step_map:
            self._multi_step(alertname, service, cb)
            return

        # ── 7. DRY-RUN ────────────────────────────────────────────────────
        if self.dry_run_mode:
            log.info(
                "DRY_RUN_PASS",
                alertname=alertname,
                service=service,
                runbook=runbook_rel,
                note="orchestrator --dry-run mode: execution skipped",
            )
            return

        script = self._resolve(runbook_rel)
        ok, output = run_script(script, service, dry_run=True, timeout=self.runbook_timeout)
        if not ok:
            log.error(
                "DRY_RUN_FAIL",
                alertname=alertname,
                service=service,
                runbook=runbook_rel,
                output=output[:400],
            )
            return
        log.info("DRY_RUN_PASS", alertname=alertname, service=service, runbook=runbook_rel)

        # ── 8. ACT ────────────────────────────────────────────────────────
        self.guard.record(service)
        ok, output = run_script(script, service, dry_run=False, timeout=self.runbook_timeout)
        log.info(
            "ACTION_EXECUTED",
            alertname=alertname,
            service=service,
            runbook=runbook_rel,
            success=ok,
            output=output[:400],
        )

        if not ok:
            log.error(
                "ACTION_FAILED",
                alertname=alertname,
                service=service,
                runbook=runbook_rel,
                output=output[:400],
            )
            self._rollback(alertname, service, runbook_rel, cb)
            action_counter.labels(service=service, runbook=runbook_rel, outcome="fail").inc()
            return

        # ── 9. VERIFY ─────────────────────────────────────────────────────
        verify_status_gauge.labels(service=service, runbook=runbook_rel).set(2)  # in_progress

        passed = verify_service(
            self.prometheus_url,
            service,
            self.baseline,
            self.verify_timeout,
            self.verify_poll_interval,
            self.verify_min_samples,
        )

        if passed:
            cb.record_success()
            circuit_breaker_gauge.labels(service=service).set(0)
            verify_status_gauge.labels(service=service, runbook=runbook_rel).set(1)
            action_counter.labels(
                service=service, runbook=runbook_rel, outcome="success"
            ).inc()
            log.info(
                "ACTION_SUCCESS",
                alertname=alertname,
                service=service,
                runbook=runbook_rel,
            )
        else:
            verify_status_gauge.labels(service=service, runbook=runbook_rel).set(0)
            self._rollback(alertname, service, runbook_rel, cb)
            action_counter.labels(
                service=service, runbook=runbook_rel, outcome="rollback"
            ).inc()

    # ── Auto-rollback ─────────────────────────────────────────────────────────

    def _rollback(
        self,
        alertname: str,
        service: str,
        runbook_rel: str,
        cb: CircuitBreaker,
    ) -> None:
        """
        Execute the rollback runbook and increment the circuit breaker counter.
        If failure_count reaches threshold the circuit opens and automation halts.
        """
        rollback_rel = self.rollback_map.get(alertname, runbook_rel)
        log.warning(
            "ROLLBACK_TRIGGERED",
            alertname=alertname,
            service=service,
            rollback_runbook=rollback_rel,
        )

        rollback_script = self._resolve(rollback_rel)
        ok, output = run_script(
            rollback_script, service, dry_run=False, timeout=self.runbook_timeout
        )
        log.info(
            "ROLLBACK_EXECUTED",
            alertname=alertname,
            service=service,
            rollback_runbook=rollback_rel,
            success=ok,
            output=output[:400],
        )

        cb.record_failure()  # may open circuit breaker (logs CIRCUIT_BREAKER_HALT internally)
        if cb.is_open():
            circuit_breaker_gauge.labels(service=service).set(1)

    # ── Multi-step transactional deploy (Scenario 4) ──────────────────────────

    def _multi_step(
        self, alertname: str, service: str, cb: CircuitBreaker
    ) -> None:
        """
        Execute a sequence of steps transactionally.
        If any step fails, roll back completed steps in REVERSE order.
        Each entry in multi_step_map is [script_rel, *extra_args].
        """
        steps: list = self.multi_step_map[alertname]
        rollback_entries: list = self.multi_step_rollback_map.get(alertname, [])

        # Track rollback entries for completed steps (in order of completion)
        completed_rollbacks: list = []

        log.info(
            "TRANSACTIONAL_START",
            alertname=alertname,
            service=service,
            total_steps=len(steps),
        )

        for i, step_entry in enumerate(steps):
            step_rel = step_entry[0]
            step_extra = list(step_entry[1:]) if len(step_entry) > 1 else []
            step_script = self._resolve(step_rel)

            ok, output = run_script(
                step_script,
                service,
                dry_run=False,
                timeout=self.runbook_timeout,
                extra_args=step_extra,
            )

            if ok:
                # Record the corresponding rollback entry for this step
                if i < len(rollback_entries):
                    completed_rollbacks.append(rollback_entries[i])
                log.info(
                    "TRANSACTIONAL_STEP_COMPLETE",
                    alertname=alertname,
                    service=service,
                    step=step_rel,
                    step_index=i,
                )
            else:
                # Step failed — roll back in reverse order
                log.error(
                    "TRANSACTIONAL_STEP_FAIL",
                    alertname=alertname,
                    service=service,
                    step=step_rel,
                    step_index=i,
                    output=output[:400],
                    completed_before_failure=[steps[j][0] for j in range(i)],
                )

                for rb_entry in reversed(completed_rollbacks):
                    rb_rel = rb_entry[0]
                    rb_extra = list(rb_entry[1:]) if len(rb_entry) > 1 else []
                    rb_script = self._resolve(rb_rel)
                    rb_ok, rb_out = run_script(
                        rb_script,
                        service,
                        dry_run=False,
                        timeout=self.runbook_timeout,
                        extra_args=rb_extra,
                    )
                    log.info(
                        "TRANSACTIONAL_ROLLBACK_STEP",
                        alertname=alertname,
                        service=service,
                        rollback_step=rb_rel,
                        success=rb_ok,
                        exit_code=0 if rb_ok else 1,
                        output=rb_out[:200],
                    )

                log.info(
                    "TRANSACTIONAL_ROLLBACK_COMPLETE",
                    alertname=alertname,
                    service=service,
                    rolled_back=[e[0] for e in reversed(completed_rollbacks)],
                )
                cb.record_failure()
                if cb.is_open():
                    circuit_breaker_gauge.labels(service=service).set(1)
                return

        # All steps succeeded — verify
        passed = verify_service(
            self.prometheus_url,
            service,
            self.baseline,
            self.verify_timeout,
            self.verify_poll_interval,
            self.verify_min_samples,
        )
        if passed:
            cb.record_success()
            log.info(
                "ACTION_SUCCESS",
                alertname=alertname,
                service=service,
                note="multi-step transactional deploy verified OK",
            )
        else:
            # Verify failed after all steps — fall back to single-step rollback
            fallback_rel = self.rollback_map.get(alertname, steps[0][0])
            self._rollback(alertname, service, fallback_rel, cb)

    # ── Main polling loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        poll_interval = self.config.get("poll_interval_seconds", 15)
        alertmanager_url = self.config["alertmanager_url"]

        log.info(
            "ORCHESTRATOR_START",
            poll_interval_seconds=poll_interval,
            alertmanager=alertmanager_url,
            dry_run_mode=self.dry_run_mode,
            circuit_breaker_threshold=self._cb_threshold,
        )

        while True:
            alerts = poll_alertmanager(alertmanager_url)
            for alert in alerts:
                # Each alert processed in its own daemon thread (Scenario 5 concurrency)
                t = threading.Thread(
                    target=self.process_alert, args=(alert,), daemon=True
                )
                t.start()
            time.sleep(poll_interval)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Closed-Loop Auto-Remediation Orchestrator — HungNguyen"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log-only mode: no runbooks executed, no side effects",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config_dir = config_path.parent.resolve()
    config = load_config(str(config_path))
    baseline = load_baseline(config, config_dir)

    # Start Prometheus metrics endpoint on :9100
    try:
        start_metrics_server(port=9100)
        log.info("METRICS_SERVER_STARTED", port=9100)
    except OSError as exc:
        log.warning("METRICS_SERVER_SKIP", port=9100, reason=str(exc))

    orchestrator = ClosedLoopOrchestrator(
        config=config,
        config_dir=config_dir,
        baseline=baseline,
        dry_run_mode=args.dry_run,
    )
    orchestrator.run()


if __name__ == "__main__":
    main()
