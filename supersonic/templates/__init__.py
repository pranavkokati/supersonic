"""Project templates — scaffold before the agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


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


def list_templates() -> List[dict]:
    return [t.to_dict() for t in TEMPLATES.values()]


def apply_template(workdir: Path, template_id: str, idea: str = "") -> str:
    """Seed workdir files. Returns agent hint block."""
    workdir.mkdir(parents=True, exist_ok=True)
    tid = template_id if template_id in TEMPLATES else "greenfield"
    if tid == "greenfield":
        return "## Template\nGreenfield — scaffold full project structure."

    if tid == "cli":
        (workdir / "pyproject.toml").write_text(
            """[project]
name = "app"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["typer>=0.12.0", "rich>=13.0.0"]

[project.scripts]
app = "app.main:app"

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
            encoding="utf-8",
        )
        (workdir / "app").mkdir(exist_ok=True)
        (workdir / "app" / "__init__.py").write_text("", encoding="utf-8")
        (workdir / "app" / "main.py").write_text(
            '"""CLI entrypoint."""\nimport typer\n\napp = typer.Typer()\n\n@app.command()\ndef hello():\n    typer.echo("hello")\n\nif __name__ == "__main__":\n    app()\n',
            encoding="utf-8",
        )
        (workdir / "tests").mkdir(exist_ok=True)
        (workdir / "tests" / "test_main.py").write_text(
            "from typer.testing import CliRunner\nfrom app.main import app\n\nrunner = CliRunner()\n\ndef test_hello():\n    r = runner.invoke(app, [\"hello\"])\n    assert r.exit_code == 0\n",
            encoding="utf-8",
        )
        return f"## Template: Python CLI\nBuild on Typer scaffold. Idea: {idea}"

    if tid == "python-api":
        (workdir / "pyproject.toml").write_text(
            """[project]
name = "api"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["fastapi>=0.115.0", "uvicorn[standard]>=0.30.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
""",
            encoding="utf-8",
        )
        (workdir / "api").mkdir(exist_ok=True)
        (workdir / "api" / "__init__.py").write_text("", encoding="utf-8")
        (workdir / "api" / "main.py").write_text(
            'from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get("/health")\ndef health():\n    return {"ok": True}\n',
            encoding="utf-8",
        )
        (workdir / "tests").mkdir(exist_ok=True)
        (workdir / "tests" / "test_api.py").write_text(
            "from fastapi.testclient import TestClient\nfrom api.main import app\n\ndef test_health():\n    assert TestClient(app).get(\"/health\").json()[\"ok\"]\n",
            encoding="utf-8",
        )
        return f"## Template: Python API\nExtend FastAPI scaffold. Idea: {idea}"

    if tid == "nextjs":
        (workdir / "package.json").write_text(
            json_package(idea),
            encoding="utf-8",
        )
        (workdir / "app").mkdir(exist_ok=True)
        (workdir / "app" / "page.tsx").write_text(
            'export default function Home() {\n  return <main><h1>App</h1></main>;\n}\n',
            encoding="utf-8",
        )
        (workdir / "tests").mkdir(exist_ok=True)
        (workdir / "tests" / "smoke.test.js").write_text(
            "test('smoke', () => { expect(1 + 1).toBe(2); });\n",
            encoding="utf-8",
        )
        return f"## Template: Next.js\nExtend app router scaffold. Idea: {idea}"

    return ""


def json_package(idea: str) -> str:
    name = "app"
    return f"""{{
  "name": "{name}",
  "version": "0.1.0",
  "private": true,
  "scripts": {{
    "dev": "next dev",
    "build": "next build",
    "test": "node --test tests/"
  }},
  "dependencies": {{
    "next": "^14.0.0",
    "react": "^18.0.0",
    "react-dom": "^18.0.0"
  }}
}}
"""
