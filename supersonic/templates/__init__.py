"""Project templates — seed a starter scaffold in the workdir before the agent's first turn.

Templates are data (a set of files to write), not code branches — adding one
means adding an entry to `_SCAFFOLDS`, not another `if` block.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_TEMPLATE_ID = "greenfield"


@dataclass
class ProjectTemplate:
    id: str
    label: str
    description: str
    stack: str

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "description": self.description, "stack": self.stack}


TEMPLATES: Dict[str, ProjectTemplate] = {
    "greenfield": ProjectTemplate("greenfield", "Greenfield", "Empty workdir — agent scaffolds from scratch", "any"),
    "cli": ProjectTemplate("cli", "Python CLI", "Typer CLI with tests and README", "python"),
    "python-api": ProjectTemplate("python-api", "Python API", "FastAPI service with pytest", "python"),
    "nextjs": ProjectTemplate("nextjs", "Next.js SaaS", "App router starter with npm test", "node"),
}

# Each entry: (relative_path, file_content). Paths ending in nothing special —
# parent directories are created automatically before writing.
_SCAFFOLDS: Dict[str, List[Tuple[str, str]]] = {
    "cli": [
        (
            "pyproject.toml",
            '[project]\nname = "app"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
            'dependencies = ["typer>=0.12.0", "rich>=13.0.0"]\n\n'
            '[project.scripts]\napp = "app.main:app"\n\n'
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        ),
        ("app/__init__.py", ""),
        (
            "app/main.py",
            '"""CLI entrypoint."""\nimport typer\n\napp = typer.Typer()\n\n'
            '@app.command()\ndef hello():\n    typer.echo("hello")\n\n'
            'if __name__ == "__main__":\n    app()\n',
        ),
        (
            "tests/test_main.py",
            "from typer.testing import CliRunner\nfrom app.main import app\n\n"
            'runner = CliRunner()\n\ndef test_hello():\n    r = runner.invoke(app, ["hello"])\n'
            "    assert r.exit_code == 0\n",
        ),
    ],
    "python-api": [
        (
            "pyproject.toml",
            '[project]\nname = "api"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
            'dependencies = ["fastapi>=0.115.0", "uvicorn[standard]>=0.30.0"]\n\n'
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        ),
        ("api/__init__.py", ""),
        (
            "api/main.py",
            'from fastapi import FastAPI\n\napp = FastAPI()\n\n'
            '@app.get("/health")\ndef health():\n    return {"ok": True}\n',
        ),
        (
            "tests/test_api.py",
            "from fastapi.testclient import TestClient\nfrom api.main import app\n\n"
            'def test_health():\n    assert TestClient(app).get("/health").json()["ok"]\n',
        ),
    ],
    "nextjs": [
        (
            "app/page.tsx",
            "export default function Home() {\n  return <main><h1>App</h1></main>;\n}\n",
        ),
        (
            "tests/smoke.test.js",
            "test('smoke', () => { expect(1 + 1).toBe(2); });\n",
        ),
    ],
}

_HINTS: Dict[str, str] = {
    "greenfield": "## Template\nGreenfield — scaffold full project structure.",
    "cli": "## Template: Python CLI\nBuild on Typer scaffold. Idea: {idea}",
    "python-api": "## Template: Python API\nExtend FastAPI scaffold. Idea: {idea}",
    "nextjs": "## Template: Next.js\nExtend app router scaffold. Idea: {idea}",
}


def list_templates() -> List[dict]:
    return [t.to_dict() for t in TEMPLATES.values()]


def _nextjs_package_json() -> str:
    return (
        '{\n  "name": "app",\n  "version": "0.1.0",\n  "private": true,\n'
        '  "scripts": {\n    "dev": "next dev",\n    "build": "next build",\n'
        '    "test": "node --test tests/"\n  },\n'
        '  "dependencies": {\n    "next": "^14.0.0",\n    "react": "^18.0.0",\n'
        '    "react-dom": "^18.0.0"\n  }\n}\n'
    )


def apply_template(workdir: Path, template_id: str, idea: str = "") -> str:
    """Write the template's starter files into `workdir`. Returns the agent's hint block."""
    workdir.mkdir(parents=True, exist_ok=True)
    tid = template_id if template_id in TEMPLATES else DEFAULT_TEMPLATE_ID

    for rel_path, content in _SCAFFOLDS.get(tid, []):
        target = workdir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    if tid == "nextjs":
        (workdir / "package.json").write_text(_nextjs_package_json(), encoding="utf-8")

    return _HINTS.get(tid, "").format(idea=idea)
