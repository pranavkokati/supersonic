"""Checkpoint -> Verify -> Rollback build loop.

checkpoint.py   git-native snapshotting of verified-good states
rollback.py     hard-reset back to the last verified checkpoint
planner.py      provider-agnostic turn planning (plan, brand, next-turn routing)
bandit.py       Thompson-sampling bandit gating Agent Racing
race.py         worktree-isolated concurrent agent racing
orchestrator.py ties it all together into run_factory()

Note: run_factory is intentionally NOT re-exported here. Importing it eagerly
at package-init time creates a circular import (orchestrator -> race ->
agents.worktree -> loop.checkpoint -> back through this __init__). Import it
directly: `from supersonic.loop.orchestrator import run_factory`.
"""
