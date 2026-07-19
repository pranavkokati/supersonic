"""DLE — Telemetry Gate: auto-detection, graceful Playwright unavailability,
and the relative (not flat-10ms) perf-regression rule. No network calls: the
Playwright provisioning subprocess calls and the browser itself are mocked.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import supersonic.verify.telemetry_gate as telemetry_gate
from supersonic.verify.telemetry_gate import (
    REGRESSION_ABSOLUTE_FLOOR_MS,
    REGRESSION_RELATIVE_THRESHOLD,
    compute_regression,
    detect_dev_server,
    ensure_playwright,
    run_telemetry_gate,
)


def setup_function(_):
    # ensure_playwright() caches its result process-wide; reset between tests.
    telemetry_gate._playwright_ready = None


def _write_pkg(tmp_path, scripts):
    import json
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))


def test_detect_dev_server_none_without_package_json(tmp_path):
    assert detect_dev_server(tmp_path) is None


def test_detect_dev_server_none_when_no_dev_server_hint(tmp_path):
    _write_pkg(tmp_path, {"build": "tsc", "lint": "eslint ."})
    assert detect_dev_server(tmp_path) is None


def test_detect_dev_server_detects_next_dev(tmp_path):
    _write_pkg(tmp_path, {"dev": "next dev", "build": "next build"})
    assert detect_dev_server(tmp_path) == "next dev"


def test_detect_dev_server_detects_vite_in_start_script(tmp_path):
    _write_pkg(tmp_path, {"start": "vite --port 5173"})
    assert detect_dev_server(tmp_path) == "vite --port 5173"


def test_detect_dev_server_ignores_malformed_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{not valid json")
    assert detect_dev_server(tmp_path) is None


def test_run_telemetry_gate_skips_when_disabled(tmp_path):
    _write_pkg(tmp_path, {"dev": "next dev"})
    verdict = run_telemetry_gate(tmp_path, enabled=False)
    assert verdict.ran is False
    assert verdict.passed is True  # fail-open
    assert "disabled" in verdict.skipped_reason


def test_run_telemetry_gate_skips_when_no_dev_server(tmp_path):
    verdict = run_telemetry_gate(tmp_path, enabled=True)
    assert verdict.ran is False
    assert verdict.passed is True
    assert "no dev/start script" in verdict.skipped_reason


def test_run_telemetry_gate_skips_gracefully_when_playwright_unavailable(tmp_path):
    _write_pkg(tmp_path, {"dev": "next dev"})

    with patch.object(telemetry_gate, "ensure_playwright", return_value=False):
        verdict = run_telemetry_gate(tmp_path, enabled=True)

    assert verdict.ran is False
    assert verdict.passed is True
    assert "playwright unavailable" in verdict.skipped_reason


def test_ensure_playwright_returns_false_on_pip_install_failure(monkeypatch):
    # Force the "import playwright" path to fail so we exercise the pip-install branch.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no network")

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert ensure_playwright(timeout=1) is False


def test_ensure_playwright_returns_false_on_timeout(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert ensure_playwright(timeout=1) is False


def test_run_telemetry_gate_never_raises_when_probe_throws(tmp_path):
    _write_pkg(tmp_path, {"dev": "next dev"})

    with patch.object(telemetry_gate, "ensure_playwright", return_value=True), \
         patch.object(telemetry_gate, "_measure_load_times", side_effect=RuntimeError("boom")):
        verdict = run_telemetry_gate(tmp_path, enabled=True)

    assert verdict.ran is False
    assert verdict.passed is True
    assert "probe failed" in verdict.skipped_reason


def test_run_telemetry_gate_reports_console_errors_and_layout(tmp_path):
    _write_pkg(tmp_path, {"dev": "next dev"})

    with patch.object(telemetry_gate, "ensure_playwright", return_value=True), \
         patch.object(telemetry_gate, "_measure_load_times", return_value=([120.0, 118.0, 121.0], ["TypeError: x is undefined"], True)):
        verdict = run_telemetry_gate(tmp_path, enabled=True, baseline_samples=[])

    assert verdict.ran is True
    assert verdict.passed is False
    assert verdict.console_errors == ["TypeError: x is undefined"]


# --- the relative regression rule (replacing the original spec's flat 10ms budget) ---

def test_compute_regression_no_baseline_never_flags():
    is_reg, base, cur = compute_regression([], [500.0, 510.0])
    assert is_reg is False


def test_compute_regression_small_relative_jump_on_slow_baseline_not_flagged():
    # 2000ms -> 2100ms is +5%, well under the 15% relative threshold.
    baseline = [2000.0, 2010.0, 1995.0]
    current = [2100.0, 2090.0, 2105.0]
    is_reg, base_med, cur_med = compute_regression(baseline, current)
    assert is_reg is False


def test_compute_regression_large_relative_jump_on_tiny_baseline_not_flagged():
    # 5ms -> 6ms is +20% (over threshold) but only 1ms absolute — under the 50ms noise floor.
    baseline = [5.0, 5.0, 5.0]
    current = [6.0, 6.0, 6.0]
    is_reg, base_med, cur_med = compute_regression(baseline, current)
    assert is_reg is False


def test_compute_regression_flags_when_both_relative_and_absolute_exceeded():
    baseline = [300.0, 305.0, 295.0]
    current = [400.0, 410.0, 395.0]  # +33%, +100ms — clears both bars
    is_reg, base_med, cur_med = compute_regression(baseline, current)
    assert is_reg is True
    assert cur_med > base_med


def test_regression_constants_are_not_the_original_flat_10ms_spec():
    # Documents the corrected design: 15% relative + 50ms absolute floor,
    # not a flat 10ms constant (which real-machine timing noise would blow past).
    assert REGRESSION_RELATIVE_THRESHOLD == 0.15
    assert REGRESSION_ABSOLUTE_FLOOR_MS == 50.0
