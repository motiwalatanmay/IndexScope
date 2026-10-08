"""Append-only archive helpers shared by the global-index scripts.

Rules every writer follows:
  - never drop a (key, date) point that is already archived;
  - values older than REVISE_DAYS are frozen (a later fetch cannot rewrite them);
  - values inside the window may be revised (finalises intraday/partial bars);
  - writes are atomic (temp file + fsync + os.replace), so a crash mid-write
    cannot truncate the file on disk;
  - a write that would leave any key with fewer points than before is refused.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

REVISE_DAYS = 10


class ArchiveError(RuntimeError):
    pass


def load_json_strict(path: Path, default=None):
    """Return the parsed file; `default` only when the file does not exist.

    An existing but unreadable file raises. Callers must not treat a corrupt
    archive as empty, because the next write would replace it with a stub.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        raise ArchiveError(f"{path.name} exists but cannot be parsed: {e}") from e


def atomic_write_json(path: Path, doc) -> None:
    text = json.dumps(doc, separators=(",", ":")) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; keep data files world-readable
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_series(old: list, new: list, today: str, revise_days: int = REVISE_DAYS):
    """Union of [date, value] points. Never removes a date.

    For a date present in both, the new value wins only if the date is within
    `revise_days` of `today`; older archived values are frozen.
    Returns (merged, stats) where stats = {added, revised, frozen_diff}.
    """
    cutoff = (date.fromisoformat(today) - timedelta(days=revise_days)).isoformat()
    merged = {p[0]: p[1] for p in old}
    stats = {"added": 0, "revised": 0, "frozen_diff": 0}
    for d, v in new:
        if v is None:
            continue
        if d not in merged:
            merged[d] = v
            stats["added"] += 1
        elif merged[d] != v:
            if d >= cutoff:
                merged[d] = v
                stats["revised"] += 1
            else:
                stats["frozen_diff"] += 1
    return [[d, merged[d]] for d in sorted(merged)], stats


def assert_no_loss(old_by_key: dict, new_by_key: dict, label: str) -> None:
    """Refuse a write that drops a key, shrinks a series, or loses a date."""
    for key, old in (old_by_key or {}).items():
        new = new_by_key.get(key)
        if new is None:
            raise ArchiveError(f"{label}: key '{key}' would be dropped")
        if len(new) < len(old):
            raise ArchiveError(f"{label}: '{key}' would shrink {len(old)} -> {len(new)}")
        missing = {p[0] for p in old} - {p[0] for p in new}
        if missing:
            raise ArchiveError(f"{label}: '{key}' would lose dates {sorted(missing)[:5]}")
