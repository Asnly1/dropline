from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path


def test_pyproject_exposes_cli_script_and_test_extras() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert payload["project"]["scripts"]["dropline"] == "dropline.cli:main"
    optional_dependencies = payload["project"]["optional-dependencies"]
    assert optional_dependencies["test"] == ["pytest"]
    assert set(optional_dependencies["dev"]) == {"pytest", "ruff"}


def test_pyproject_ships_the_memory_ui_page() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert payload["tool"]["setuptools"]["package-data"]["dropline"] == ["static/*.html"]
    page = resources.files("dropline").joinpath("static", "memory_ui.html")
    assert page.is_file()
    assert "__DROPLINE_TOKEN__" in page.read_text(encoding="utf-8")
