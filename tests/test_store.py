"""Store, config, and the planner's brand/slug fallback logic."""

from __future__ import annotations

from supersonic.config import UserSecrets
from supersonic.loop.planner import ProductBrand
from supersonic.store import create_project, init_db, list_projects


def test_store_project(tmp_path, monkeypatch):
    monkeypatch.setattr("supersonic.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("supersonic.store.CONFIG_DIR", tmp_path)
    init_db()
    p = create_project("Test", idea="CLI tool", agent="claude")
    assert p.id
    assert p.agent == "claude"
    assert list_projects()


def test_product_brand_slug_from_idea():
    b = ProductBrand.from_idea("CLI that connects to the Supersonic dashboard!!")
    assert b.repo_slug
    assert " " not in b.repo_slug
    assert b.repo_slug == b.repo_slug.lower()


def test_product_brand_slug_length_capped():
    long_idea = "a" * 200
    b = ProductBrand.from_idea(long_idea)
    assert len(b.repo_slug) <= 40


def test_user_secrets_defaults_require_no_composio():
    secrets = UserSecrets()
    assert secrets.default_agent == "claude"
    assert secrets.ship_mode == "pr"
    assert not hasattr(secrets, "composio_api_key")
    assert not hasattr(secrets, "race_enabled")


def test_configured_providers_always_lists_ollama_last():
    secrets = UserSecrets(anthropic_api_key="sk-a")
    names = secrets.configured_providers()
    assert names[-1] == "ollama"
    assert "anthropic" in names


def test_dle_toggles_have_sane_defaults():
    secrets = UserSecrets()
    # Cheap, always-safe DLE stages default on; patch-diff mode (a bigger
    # behavior change not every agent CLI backend handles well) defaults off.
    assert secrets.dle_dependency_mapper is True
    assert secrets.dle_syntax_shield is True
    assert secrets.dle_telemetry_gate is True
    assert secrets.dle_patch_diff_mode is False


def test_dle_toggles_are_overridable():
    secrets = UserSecrets(dle_patch_diff_mode=True, dle_telemetry_gate=False)
    assert secrets.dle_patch_diff_mode is True
    assert secrets.dle_telemetry_gate is False


def test_verify_min_signals_pass_allows_up_to_five_for_the_optional_telemetry_signal():
    secrets = UserSecrets(verify_min_signals_pass=5)
    assert secrets.verify_min_signals_pass == 5
