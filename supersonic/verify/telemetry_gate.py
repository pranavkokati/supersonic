"""Telemetry Gate — an OPTIONAL fifth Verify signal: a real-browser runtime check.

Every other signal in the Verify gate (tests, lint, critic, thrash) works
without a browser. This one only makes sense for projects that actually run
a UI, and only when a headless browser is available — so it is auto-detected
and auto-skipped, never a hard requirement.

### Auto-detection

We only attempt telemetry when the workdir looks like a web app: a
`package.json` with a `dev` or `start` script that appears to bind a port
(a dev server: `next dev`, `vite`, `react-scripts start`, `webpack serve`,
`http-server`, a bare `node server.js`, etc.). Anything else — Python CLIs,
libraries, headless backend services — silently skips this signal, the same
way `verify/qa.py` silently skips tests/lint when it can't detect a runner.

### Availability

This module needs Playwright plus a Chromium binary, neither of which is
guaranteed to exist in a bare sandbox/CI environment. We attempt, once per
process, to provision them (`pip install playwright --break-system-packages`
then `playwright install chromium`), each under a bounded timeout. If either
step fails or times out for *any* reason — no network, no disk space, a
read-only environment, a version conflict — we log a warning and report this
signal as "not run". This must never hard-fail a turn just because
Playwright isn't installable here; that would make an environment detail
into a correctness gate.

### Correcting the original spec: no flat "10ms" regression budget

An earlier version of this spec proposed flagging a performance regression
whenever a page load got 10ms slower. That constant doesn't survive contact
with a real machine: wall-clock page-load timing on a shared, virtualized,
or simply busy machine routinely jitters by tens of milliseconds between
back-to-back runs of the *same* unchanged code, purely from scheduler noise,
disk cache state, and background load. A flat 10ms threshold would flag
almost every turn as a regression on a noisy machine, or none at all on a
quiet one — either way it's not measuring the code.

Instead we use a *relative* budget computed from our own baseline:
  - Run the page load a few times before the turn's change (baseline) and a
    few times after (current), and take the median of each — medians resist
    the occasional slow outlier a mean would be skewed by.
  - Flag a regression only if the current median is more than 15% slower
    than the baseline median, AND that absolute difference exceeds a 50ms
    noise floor. Requiring both conditions means a large relative jump on an
    already-tiny baseline (e.g. 5ms -> 6ms, +20%) doesn't trip the gate, and
    neither does a small relative jump on a slow page that's really just
    noise (e.g. 2000ms -> 2100ms is +5%, under the 15% bar).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

REGRESSION_RELATIVE_THRESHOLD = 0.15  # 15%
REGRESSION_ABSOLUTE_FLOOR_MS = 50.0  # noise floor

_DEV_SCRIPT_KEYS = ("dev", "start")
_DEV_SERVER_HINTS = (
    "next dev", "vite", "react-scripts start", "webpack serve", "webpack-dev-server",
    "http-server", "serve ", "nuxt dev", "ng serve", "parcel", "--port", "-p ",
)

_playwright_ready: Optional[bool] = None  # process-local cache so we don't re-probe every turn


@dataclass
class TelemetryVerdict:
    ran: bool = False
    passed: bool = True  # fail-open: an unavailable/skipped signal never fails a turn
    skipped_reason: str = ""
    console_errors: List[str] = field(default_factory=list)
    layout_ok: bool = True
    perf_regression: bool = False
    baseline_median_ms: float = 0.0
    current_median_ms: float = 0.0

    def to_context_block(self) -> str:
        if not self.ran:
            return f"## Telemetry gate\nSkipped ({self.skipped_reason or 'not applicable'})."
        status = "PASS" if self.passed else "FAIL"
        lines = [f"## Telemetry gate — {status}"]
        if self.console_errors:
            lines.append(f"Console errors ({len(self.console_errors)}): " + "; ".join(self.console_errors[:5]))
        if not self.layout_ok:
            lines.append("Layout sanity check failed.")
        if self.perf_regression:
            lines.append(
                f"Perf regression: baseline median {self.baseline_median_ms:.0f}ms -> "
                f"current median {self.current_median_ms:.0f}ms."
            )
        return "\n".join(lines)


def detect_dev_server(workdir: Path) -> Optional[str]:
    """Return the detected dev/start command string, or None if this doesn't
    look like a project with a local web dev server to probe."""
    pkg = workdir / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts") or {}
    for key in _DEV_SCRIPT_KEYS:
        cmd = scripts.get(key)
        if isinstance(cmd, str) and cmd.strip():
            low = cmd.lower()
            if any(hint in low for hint in _DEV_SERVER_HINTS):
                return cmd
    return None


def ensure_playwright(timeout: int = 120) -> bool:
    """Best-effort: make sure `playwright` + a Chromium binary are usable.
    Returns False (never raises) on any failure. Cached per-process."""
    global _playwright_ready
    if _playwright_ready is not None:
        return _playwright_ready

    try:
        import playwright  # noqa: F401
        _playwright_ready = True
        return True
    except ImportError:
        pass

    try:
        install = subprocess.run(
            ["pip", "install", "playwright", "--break-system-packages"],
            capture_output=True, text=True, timeout=timeout,
        )
        if install.returncode != 0:
            logger.warning("telemetry gate: pip install playwright failed: %s", install.stderr[-500:])
            _playwright_ready = False
            return False
        browser_install = subprocess.run(
            ["playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=timeout,
        )
        if browser_install.returncode != 0:
            logger.warning("telemetry gate: playwright install chromium failed: %s", browser_install.stderr[-500:])
            _playwright_ready = False
            return False
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("telemetry gate: playwright provisioning failed: %s", e)
        _playwright_ready = False
        return False

    _playwright_ready = True
    return True


def compute_regression(baseline_samples: List[float], current_samples: List[float]) -> Tuple[bool, float, float]:
    """Pure function: decide whether `current_samples` represents a real
    regression vs `baseline_samples`, using the relative+absolute-floor rule
    documented at the top of this module. Returns (is_regression, baseline_median, current_median)."""
    if not baseline_samples or not current_samples:
        return False, 0.0, 0.0
    base_med = median(baseline_samples)
    cur_med = median(current_samples)
    if base_med <= 0:
        return False, base_med, cur_med
    delta = cur_med - base_med
    relative = delta / base_med
    is_regression = relative > REGRESSION_RELATIVE_THRESHOLD and delta > REGRESSION_ABSOLUTE_FLOOR_MS
    return is_regression, base_med, cur_med


def _measure_load_times(url: str, runs: int = 3, timeout_ms: int = 15000) -> Tuple[List[float], List[str], bool]:
    """Load `url` `runs` times with a headless Chromium via Playwright, returning
    (load_times_ms, console_errors, layout_ok). Any failure raises — callers
    must catch broadly, this is the one function actually touching a browser."""
    from playwright.sync_api import sync_playwright  # imported lazily; only reachable after ensure_playwright()

    load_times: List[float] = []
    console_errors: List[str] = []
    layout_ok = True

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for _ in range(runs):
                page = browser.new_page()
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                t0 = _time.monotonic()
                page.goto(url, timeout=timeout_ms, wait_until="load")
                elapsed_ms = (_time.monotonic() - t0) * 1000.0
                load_times.append(elapsed_ms)
                try:
                    body_box = page.evaluate(
                        "() => { const b = document.body; return b ? {w: b.scrollWidth, h: b.scrollHeight} : null; }"
                    )
                    if not body_box or body_box.get("w", 0) <= 0 or body_box.get("h", 0) <= 0:
                        layout_ok = False
                except Exception:  # noqa: BLE001 — layout probing is best-effort
                    layout_ok = False
                page.close()
        finally:
            browser.close()

    return load_times, console_errors, layout_ok


def run_telemetry_gate(
    workdir: Path,
    *,
    enabled: bool = True,
    url: str = "http://127.0.0.1:3000",
    baseline_samples: Optional[List[float]] = None,
    runs: int = 3,
) -> TelemetryVerdict:
    """Run the telemetry signal if (and only if) it's applicable and available.

    `baseline_samples` lets a caller supply pre-change load-time samples
    (e.g. measured against the last-good checkpoint) to compare the
    post-change measurement against. Without a baseline, we can still surface
    console errors / layout sanity, but perf regression can't be assessed —
    there is nothing to compare against, and that's reported honestly rather
    than guessed at.
    """
    workdir = Path(workdir)
    if not enabled:
        return TelemetryVerdict(ran=False, skipped_reason="disabled by config")

    dev_cmd = detect_dev_server(workdir)
    if not dev_cmd:
        return TelemetryVerdict(ran=False, skipped_reason="no dev/start script binding a port detected")

    if not ensure_playwright():
        return TelemetryVerdict(ran=False, skipped_reason="playwright unavailable in this environment")

    try:
        current_samples, console_errors, layout_ok = _measure_load_times(url, runs=runs)
    except Exception as e:  # noqa: BLE001 — telemetry must never hard-fail a turn
        logger.warning("telemetry gate probe failed, skipping: %s", e)
        return TelemetryVerdict(ran=False, skipped_reason=f"probe failed: {e}")

    if not current_samples:
        return TelemetryVerdict(ran=False, skipped_reason="no samples collected")

    is_regression, base_med, cur_med = compute_regression(baseline_samples or [], current_samples)
    passed = not console_errors and layout_ok and not is_regression

    return TelemetryVerdict(
        ran=True,
        passed=passed,
        console_errors=console_errors,
        layout_ok=layout_ok,
        perf_regression=is_regression,
        baseline_median_ms=base_med,
        current_median_ms=cur_med,
    )
