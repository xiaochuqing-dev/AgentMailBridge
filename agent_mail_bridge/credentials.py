"""Windows 单用户凭据存储与旧配置迁移。"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


SERVICE_NAME = "AgentMailBridge"
GMAIL_IMAP_SECRET = "gmail_imap_app_password"
QQ_SMTP_SECRET = "qq_smtp_auth_code"
SECRET_ENV_KEYS = {
    GMAIL_IMAP_SECRET: "GMAIL_APP_PASSWORD",
    QQ_SMTP_SECRET: "QQ_AUTH_CODE",
}


class CredentialError(RuntimeError):
    """本机安全存储不可用或操作失败。"""


class CredentialBackend(Protocol):
    def read(self, name: str) -> str | None: ...
    def write(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class MemoryCredentialBackend:
    """仅供隔离测试使用的内存后端。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def read(self, name: str) -> str | None:
        return self.values.get(name)

    def write(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class WindowsCredentialBackend:
    """通过 Windows Credential Manager 保存当前用户通用凭据。"""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32),
            ("Type", ctypes.c_uint32),
            ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p),
            ("LastWritten", ctypes.c_uint64),
            ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", ctypes.c_uint32),
            ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p),
            ("UserName", ctypes.c_wchar_p),
        ]

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError("Windows Credential Manager 仅在 Windows 可用")
        self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._api.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(self.CREDENTIALW)),
        ]
        self._api.CredReadW.restype = ctypes.c_bool
        self._api.CredWriteW.argtypes = [ctypes.POINTER(self.CREDENTIALW), ctypes.c_uint32]
        self._api.CredWriteW.restype = ctypes.c_bool
        self._api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self._api.CredDeleteW.restype = ctypes.c_bool
        self._api.CredFree.argtypes = [ctypes.c_void_p]

    @staticmethod
    def _target(name: str) -> str:
        return f"{SERVICE_NAME}:{name}"

    def read(self, name: str) -> str | None:
        pointer = ctypes.POINTER(self.CREDENTIALW)()
        if not self._api.CredReadW(
            self._target(name), self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
        ):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"读取 Windows 凭据失败，错误码 {error}")
        try:
            item = pointer.contents
            if not item.CredentialBlob or item.CredentialBlobSize == 0:
                return ""
            data = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
            return data.decode("utf-16-le")
        finally:
            self._api.CredFree(pointer)

    def write(self, name: str, value: str) -> None:
        if not value:
            raise CredentialError("凭据不能为空")
        data = value.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        item = self.CREDENTIALW()
        item.Type = self.CRED_TYPE_GENERIC
        item.TargetName = self._target(name)
        item.CredentialBlobSize = len(data)
        item.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        item.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        item.UserName = SERVICE_NAME
        if not self._api.CredWriteW(ctypes.byref(item), 0):
            raise CredentialError(f"写入 Windows 凭据失败，错误码 {ctypes.get_last_error()}")

    def delete(self, name: str) -> None:
        if not self._api.CredDeleteW(self._target(name), self.CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != self.ERROR_NOT_FOUND:
                raise CredentialError(f"删除 Windows 凭据失败，错误码 {error}")


@dataclass
class MigrationResult:
    migrated: list[str]
    skipped: list[str]
    failed: dict[str, str]


class CredentialService:
    """供 GUI、CLI 和 ApplicationService 共用的凭据入口。"""

    def __init__(self, backend: CredentialBackend | None = None) -> None:
        self.backend = backend or WindowsCredentialBackend()

    def get(self, name: str) -> str | None:
        return self.backend.read(name)

    def set(self, name: str, value: str) -> None:
        self.backend.write(name, value)
        if self.backend.read(name) != value:
            raise CredentialError("凭据写入后验证失败")

    def delete(self, name: str) -> None:
        self.backend.delete(name)

    def status(self) -> dict[str, bool]:
        return {name: bool(self.get(name)) for name in SECRET_ENV_KEYS}

    def migrate_env(self, env_path: Path) -> MigrationResult:
        """先写入并验证安全存储，全部成功项才从 .env 清空。"""
        from dotenv import dotenv_values
        from agent_mail_bridge.ui.settings_store import save_env_values

        values = dotenv_values(env_path) if env_path.exists() else {}
        migrated: list[str] = []
        skipped: list[str] = []
        failed: dict[str, str] = {}
        clear_values: dict[str, str] = {}
        for name, env_key in SECRET_ENV_KEYS.items():
            value = str(values.get(env_key) or "").strip()
            if not value:
                skipped.append(env_key)
                continue
            try:
                self.set(name, value)
            except Exception as exc:  # noqa: BLE001
                failed[env_key] = str(exc)
                continue
            migrated.append(env_key)
            clear_values[env_key] = ""
        if clear_values:
            save_env_values(clear_values, env_path)
        return MigrationResult(migrated, skipped, failed)


def load_secure_secrets() -> dict[str, str]:
    """读取安全存储；测试或后端不可用时安全回退为空。"""
    if os.getenv("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE") == "1":
        return {}
    try:
        service = CredentialService()
        return {
            env_key: value
            for name, env_key in SECRET_ENV_KEYS.items()
            if (value := service.get(name))
        }
    except CredentialError:
        return {}
