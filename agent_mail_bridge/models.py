"""应用服务的结构化结果模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class OperationStatus(StrEnum):
    """核心操作统一状态。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    AUTH_REQUIRED = "auth_required"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"


@dataclass
class ServiceResult:
    """所有应用服务结果的公共字段。"""

    status: OperationStatus
    error_code: str | None = None
    message: str = ""
    needs_auth: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {OperationStatus.SUCCESS, OperationStatus.PARTIAL}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["ok"] = self.ok
        return data


@dataclass
class ReceiveResult(ServiceResult):
    """收件操作结果。"""

    backend: str = ""
    scanned: int = 0
    accepted: int = 0
    saved: int = 0
    skipped: int = 0
    duplicates: int = 0
    failed: int = 0
    attachments: int = 0
    saved_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class SendResult(ServiceResult):
    """发件操作结果。"""

    request_id: str = ""
    send_status: str = "not_sent"
    source_path: str = ""
    send_copy_path: str = ""
    sent_copy_path: str = ""
    subject: str = ""
    to_email: str = ""
    sent_at: str = ""
