from __future__ import annotations

import json
import os
from pathlib import Path

INITIAL_STATUS = {
    "alpha": "UNPROVEN",
    "backtest": "NOT_RUN",
    "oos": "NOT_RUN",
    "shadow": "NOT_STARTED",
    "paper": "OUT_OF_SCOPE",
    "live": "DISABLED",
}


def write_project_status(path: Path = Path("runtime/project_status.json")) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and set(current) == set(INITIAL_STATUS):
                return {str(key): str(value) for key, value in current.items()}
        except (OSError, ValueError, TypeError):
            raise RuntimeError("project status file is malformed; refusing to overwrite evidence") from None
        raise RuntimeError("project status file is malformed; refusing to overwrite evidence")
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(INITIAL_STATUS, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return dict(INITIAL_STATUS)
