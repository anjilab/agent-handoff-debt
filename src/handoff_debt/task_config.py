"""Task TOML loading."""

from __future__ import annotations

import tomllib
from pathlib import Path


def load_task_config(path: Path) -> dict[str, str]:
    data = tomllib.loads(path.read_text())
    return dict(data["task"])
