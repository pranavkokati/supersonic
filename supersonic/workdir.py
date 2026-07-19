"""Workdir introspection — feeds the planner and critic a compact project snapshot."""

from __future__ import annotations

from pathlib import Path

SKIP = {".git", "__pycache__", ".venv", "node_modules", ".continuity"}
TEXT_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json", ".html", ".css", ".toml", ".yaml", ".yml", ".sh"}


def workdir_summary(workdir: Path, max_files: int = 25) -> str:
    if not workdir.exists():
        return "(empty workdir)"
    lines: list[str] = []
    files = sorted(
        [f for f in workdir.rglob("*") if f.is_file() and not any(p in SKIP for p in f.parts)],
        key=lambda p: str(p),
    )
    for f in files[:max_files]:
        rel = f.relative_to(workdir)
        if f.suffix.lower() in TEXT_EXT and f.stat().st_size < 80_000:
            try:
                body = f.read_text(errors="replace")[:1200]
                lines.append(f"### {rel}\n```\n{body}\n```")
            except OSError:
                lines.append(f"### {rel}\n(unreadable)")
        else:
            lines.append(f"### {rel}\n({f.stat().st_size} bytes)")
    if len(files) > max_files:
        lines.append(f"\n… and {len(files) - max_files} more files")
    return "\n\n".join(lines) if lines else "(no files yet)"
