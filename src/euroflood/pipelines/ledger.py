"""Append-only JSONL state ledger for resumable ingestion.

Records per-file download/process state to a JSON-lines file on disk so a crashed
or interrupted ingestion can resume instead of restarting the whole batch, and so
failures are attributable (a dead-letter list). JSONL is append-only and
crash-safe: a torn final line is simply ignored on reload, and the main process
is the only writer (workers return results), so no locking is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# process_status values that mean "do not reprocess this file".
_DONE_STATUSES = frozenset({"complete", "empty"})


class StateLedger:
    """A resumable, append-only record of per-file ingestion state."""

    def __init__(self, path: Path) -> None:
        """Load any existing ledger at `path` (last write per global_id wins)."""
        self.path = path
        self.records: dict[int, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a torn last line from a crash
                gid = record.get("global_id")
                if gid is not None:
                    self.records[gid] = record

    def is_done(self, global_id: int) -> bool:
        """Return True if this file has already been processed (or was empty)."""
        return self.records.get(global_id, {}).get("process_status") in _DONE_STATUSES

    def record(self, global_id: int, **fields: Any) -> None:
        """Merge `fields` into the record for `global_id` and append it to disk."""
        record = {**self.records.get(global_id, {}), "global_id": global_id, **fields}
        self.records[global_id] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def failures(self) -> list[dict[str, Any]]:
        """Return records whose download or processing failed."""
        return [
            record
            for record in self.records.values()
            if record.get("download_status") == "failed"
            or record.get("process_status") == "failed"
        ]
