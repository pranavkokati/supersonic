"""Checkpoint -> Verify -> Rollback build loop.

checkpoint.py   git-native snapshotting of verified-good states
rollback.py     hard-reset back to the last verified checkpoint
planner.py      provider-agnostic turn planning (plan, brand, next-turn routing)
orchestrator.py ties it all together into run_factory()

Note: run_factory is intentionally NOT re-exported here to keep package-init
import order simple and predictable. Import it directly:
`from supersonic.loop.orchestrator import run_factory`.
"""
