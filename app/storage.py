"""审计结论的冻结存储。

相同审计标识 + 相同契约 → 返回原冻结结论；
相同审计标识 + 任一契约变化 → 拒绝（409）且绝不清写旧结论。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()


def canonical_contract(sender: Any, receiver: Any) -> str:
    """对契约做规范化序列化：仅空白/键序差异不视为契约变化。"""
    return json.dumps(
        {"sender": sender, "receiver": receiver},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def contract_hash(sender: Any, receiver: Any) -> str:
    return hashlib.sha256(canonical_contract(sender, receiver).encode("utf-8")).hexdigest()


class FrozenStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("AUDIT_DATA_PATH", "/data/audits.json")
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._records = data
        except FileNotFoundError:
            self._records = {}
        except (json.JSONDecodeError, OSError):
            # 存储损坏不允许静默覆盖，直接暴露给运维。
            raise

    def _persist(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._records, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, audit_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._records.get(audit_id)
            return json.loads(json.dumps(rec)) if rec else None

    def submit(
        self,
        audit_id: str,
        sender: Any,
        receiver: Any,
        report: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, bool]:
        """返回 (冻结记录, 是否新建, 是否契约冲突)。"""
        digest = contract_hash(sender, receiver)
        with self._lock:
            existing = self._records.get(audit_id)
            now = datetime.now(timezone.utc).isoformat()
            if existing is not None:
                if existing["contract_hash"] == digest:
                    return existing, False, False
                return existing, False, True

            record = {
                "audit_id": audit_id,
                "contract_hash": digest,
                "created_at": now,
                "contract": {"sender": sender, "receiver": receiver},
                "report": report,
            }
            self._records[audit_id] = record
            self._persist()
            return record, True, False

    def count(self) -> int:
        with self._lock:
            return len(self._records)
