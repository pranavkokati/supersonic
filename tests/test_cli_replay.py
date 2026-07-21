"""`sonic replay` — the CLI-facing entrypoint for Black Box Replay, exercised
through Typer's CliRunner the same way test_cli_verify_receipts.py covers
`sonic verify-receipts`."""

from __future__ import annotations

from typer.testing import CliRunner

from supersonic.cli import app
from supersonic.loop.checkpoint import CheckpointManager

runner = CliRunner()


def test_replay_rejects_a_nonexistent_workdir(tmp_path):
    result = runner.invoke(app, ["replay", str(tmp_path / "does-not-exist")])
    assert result.exit_code != 0


def test_replay_writes_html_to_default_path(tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    CheckpointManager(workdir).create(0, "setup complete")

    result = runner.invoke(app, ["replay", str(workdir)])
    assert result.exit_code == 0

    out_path = workdir / "replay.html"
    assert out_path.exists()
    assert out_path.read_text().startswith("<!DOCTYPE html>")
    assert "Wrote" in result.stdout


def test_replay_writes_html_to_custom_out_path(tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    CheckpointManager(workdir).create(0, "setup complete")
    out_path = tmp_path / "custom-replay.html"

    result = runner.invoke(app, ["replay", str(workdir), "--out", str(out_path)])
    assert result.exit_code == 0
    assert out_path.exists()
    assert not (workdir / "replay.html").exists()
