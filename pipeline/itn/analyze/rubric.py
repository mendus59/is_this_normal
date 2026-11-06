"""Load versioned rubrics and calibration anchors from the repository."""

import os
from pathlib import Path

import yaml


def _repo_dir(name: str) -> Path:
    if override := os.environ.get(f"ITN_{name.upper()}_DIR"):
        return Path(override)
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for d in (start, *start.parents):
            if (d / name).is_dir() and (d / "schema").is_dir():
                return d / name
    raise FileNotFoundError(f"Could not find {name}/. Run from the repository or set ITN_{name.upper()}_DIR.")


def load_rubric(version: int | None = None) -> dict:
    d = _repo_dir("rubrics")
    if version is None:
        version = max(int(p.stem[1:]) for p in d.glob("v*.yaml"))
    return yaml.safe_load((d / f"v{version}.yaml").read_text())


def load_anchors() -> dict:
    return yaml.safe_load((_repo_dir("calibration") / "anchors.yaml").read_text())
