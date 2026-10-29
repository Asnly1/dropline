from __future__ import annotations

import re
import tomllib
from pathlib import Path

REQUIREMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _requirement_names(requirements: list[str]) -> set[str]:
    names = set()
    for requirement in requirements:
        match = REQUIREMENT_NAME_RE.match(requirement)
        assert match is not None, requirement
        names.add(match.group(0))
    return names


def test_pyproject_exposes_cli_script_and_test_extras() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert payload["project"]["scripts"]["dropline"] == "dropline.cli:main"
    optional_dependencies = payload["project"]["optional-dependencies"]
    assert optional_dependencies["test"] == ["pytest"]
    assert _requirement_names(optional_dependencies["dev"]) == {"pytest", "ruff"}
    # ruff is pinned so that CI and local runs lint with the same rule implementation.
    assert any(re.fullmatch(r"ruff==\d+(\.\d+)*", requirement) for requirement in optional_dependencies["dev"])
