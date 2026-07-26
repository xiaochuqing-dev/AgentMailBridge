"""在随机 AppId 和隔离用户目录中验收安装、升级、卸载与重装。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.application_service import ApplicationService
from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.credentials import WindowsCredentialBackend
from agent_mail_bridge.database import (
    close_connection,
    create_outbound_message,
    save_auto_receive_state,
)
from agent_mail_bridge.mail_archive import archive_normalized_mail
from agent_mail_bridge.mail_common import normalized_mail_from_raw
from agent_mail_bridge.ui.settings_store import save_env_values
from agent_mail_bridge.utils import sha256_of_file


def _close_runtime_handles() -> None:
    close_connection()
    logger = logging.getLogger("agent_mail_bridge")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _run(command: list[str], *, env: dict[str, str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"命令失败，退出码 {completed.returncode}：{Path(command[0]).name}"
        )
    return completed


def _install(installer: Path, install_dir: Path, env: dict[str, str]) -> None:
    _run(
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/MERGETASKS=!desktopicon",
            f"/DIR={install_dir}",
        ],
        env=env,
        timeout=300,
    )
    if not (install_dir / "AgentMailBridge.exe").is_file():
        raise RuntimeError("安装完成后主 EXE 不存在")
    if not (install_dir / "AgentMailBridgeMCP.exe").is_file():
        raise RuntimeError("安装完成后 MCP EXE 不存在")


def _uninstall(install_dir: Path, env: dict[str, str]) -> None:
    uninstallers = sorted(
        install_dir.glob("unins*.exe"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not uninstallers:
        raise RuntimeError("隔离安装缺少卸载器")
    uninstaller = uninstallers[0]
    _run(
        [
            str(uninstaller),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
        ],
        env=env,
        timeout=300,
    )


def _packaged_probe(
    install_dir: Path,
    env: dict[str, str],
    expected_version: str,
    *,
    cfg: AppConfig | None = None,
) -> dict[str, bool]:
    gui = install_dir / "AgentMailBridge.exe"
    mcp = install_dir / "AgentMailBridgeMCP.exe"
    version = _run([str(gui), "--version"], env=env).stdout.strip()
    _run([str(gui), "--packaged-self-test"], env=env)
    probe_env = dict(env)
    migration_backup_created = True
    if expected_version == "1.5.0":
        if cfg is None:
            raise RuntimeError("v1.5.0 packaged probe 缺少隔离配置")
        anonymous_env = dict(env)
        anonymous_env.pop("AGENT_MAIL_BRIDGE_CLIENT_ID", None)
        anonymous_env.pop("AGENT_MAIL_BRIDGE_CLIENT_TOKEN", None)
        bootstrap = subprocess.run(
            [str(mcp)],
            input=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "lifecycle-migration-bootstrap",
                            "version": "1",
                        },
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=anonymous_env,
            timeout=60,
            check=False,
        )
        if bootstrap.returncode != 0:
            raise RuntimeError("隔离 MCP migration bootstrap 失败")
        migration_backup_created = bool(
            list(
                (cfg.data_root_path / "backups").glob(
                    "*before_v1_5_agent_permissions*.db"
                )
            )
        )
        probe_env.update(_provision_lifecycle_client(cfg))
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "lifecycle-validation", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_mail_sync_status", "arguments": {}},
        },
    ]
    completed = subprocess.run(
        [str(mcp)],
        input="".join(
            json.dumps(item, ensure_ascii=False) + "\n" for item in requests
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=probe_env,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("隔离 MCP probe 失败")
    responses = [
        json.loads(line) for line in completed.stdout.splitlines() if line.strip()
    ]
    by_id = {item.get("id"): item for item in responses}
    tools = by_id.get(2, {}).get("result", {}).get("tools", [])
    server_version = (
        by_id.get(1, {})
        .get("result", {})
        .get("serverInfo", {})
        .get("version")
    )
    return {
        "version": version == expected_version,
        "gui_packaged_self_test": True,
        "mcp_initialize": server_version == expected_version,
        "mcp_tools": len(tools) == 7,
        "mcp_sync_status": not bool(by_id.get(3, {}).get("result", {}).get("isError")),
        "mcp_stdout_purity": "Traceback" not in completed.stderr,
        "mcp_eof_exit": completed.returncode == 0,
        "agent_migration_backup": migration_backup_created,
    }


def _baseline_values(home: Path, suffix: str) -> dict[str, str]:
    data_root = home / "Data"
    oauth_root = home / "OAuth"
    gmail_address = f"lifecycle.{suffix}@gmail.com"
    qq_address = f"lifecycle.{suffix}@qq.com"
    owner_address = f"owner.{suffix}@example.test"
    return {
        "GMAIL_ADDRESS": gmail_address,
        "GMAIL_RECEIVE_BACKEND": "gmail_api",
        "GMAIL_API_SCOPES": "https://www.googleapis.com/auth/gmail.readonly",
        "GMAIL_API_CREDENTIALS_PATH": str(oauth_root / "credentials.json"),
        "GMAIL_API_TOKEN_PATH": str(oauth_root / "token.json"),
        "QQ_EMAIL": qq_address,
        "OWNER_GMAIL": owner_address,
        "DATA_ROOT": str(data_root),
        "MCP_MAIL_READ_ENABLED": "false",
        "AUTO_RECEIVE_ENABLED": "false",
    }


def _baseline_config(home: Path, suffix: str) -> AppConfig:
    values = _baseline_values(home, suffix)
    return AppConfig(
        gmail_address=values["GMAIL_ADDRESS"],
        gmail_receive_backend="gmail_api",
        gmail_api_credentials_path=Path(values["GMAIL_API_CREDENTIALS_PATH"]),
        gmail_api_token_path=Path(values["GMAIL_API_TOKEN_PATH"]),
        gmail_api_scopes=[values["GMAIL_API_SCOPES"]],
        qq_email=values["QQ_EMAIL"],
        owner_gmail=values["OWNER_GMAIL"],
        data_root=Path(values["DATA_ROOT"]),
        loaded_env_path=home / "Config" / ".env",
        mcp_mail_read_enabled=False,
        receive_rule_mode="all_scanned",
    )


def _seed_baseline(
    home: Path, *, suffix: str | None = None
) -> tuple[AppConfig, str]:
    suffix = suffix or uuid.uuid4().hex[:10]
    config_path = home / "Config" / ".env"
    values = _baseline_values(home, suffix)
    gmail_address = values["GMAIL_ADDRESS"]
    qq_address = values["QQ_EMAIL"]
    owner_address = values["OWNER_GMAIL"]
    save_env_values(
        values,
        config_path,
    )
    cfg = _baseline_config(home, suffix)
    service = ApplicationService(cfg)
    initialized = service.initialize()
    if not initialized.ok:
        raise RuntimeError("隔离基线初始化失败")

    accounts = list(service.list_mail_accounts().details.get("accounts") or [])
    gmail_id = next(
        str(item["account_id"]) for item in accounts if item["provider"] == "gmail"
    )
    qq_id = next(
        str(item["account_id"]) for item in accounts if item["provider"] == "qq"
    )
    created_163 = service.create_mail_account(
        provider="163",
        email_address=f"lifecycle.{suffix}@163.com",
        display_name="Lifecycle 163",
        secret="isolated-placeholder",
    )
    created_generic = service.create_mail_account(
        provider="generic_imap_smtp",
        email_address=f"lifecycle.{suffix}@example.test",
        display_name="Lifecycle Generic",
        provider_settings={
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "imap_security": "ssl",
            "smtp_host": "smtp.example.test",
            "smtp_port": 465,
            "smtp_security": "ssl",
        },
        secret="isolated-placeholder",
    )
    if not created_163.ok or not created_generic.ok:
        raise RuntimeError("隔离账号基线创建失败")

    accounts = list(service.list_mail_accounts().details.get("accounts") or [])
    supported_accounts = [
        account
        for account in accounts
        if account["provider"] in {"gmail", "qq", "163", "generic_imap_smtp"}
    ]
    for index, account in enumerate(supported_accounts, 1):
        account_id = str(account["account_id"])
        provider = str(account["provider"])
        runtime_cfg = service._account_router.context(account_id).config
        message = EmailMessage()
        message["From"] = f"sender-{index}@example.test"
        message["To"] = str(account["email_address"])
        message["Subject"] = f"Lifecycle {provider}"
        message_id = f"<lifecycle-{provider}-{suffix}@example.test>"
        message["Message-ID"] = message_id
        message.set_content(f"Lifecycle body {provider}")
        message.add_attachment(
            f"Lifecycle attachment {provider}".encode("utf-8"),
            maintype="application",
            subtype="octet-stream",
            filename=f"attachment-{provider}.bin",
        )
        raw = message.as_bytes()
        normalized = normalized_mail_from_raw(
            raw,
            backend="gmail_api" if provider == "gmail" else "imap",
            backend_message_id=f"provider-{provider}-{suffix}",
            thread_id=f"thread-{provider}-{suffix}",
            uid=str(index),
            received_at="2026-07-26 12:00:00",
            saved_date="2026-07-26",
            max_attachment_bytes=1024 * 1024,
            mailbox_ref=f"{provider}:inbox",
        )
        archived = archive_normalized_mail(runtime_cfg, normalized, message_id)
        if archived.status not in {"saved", "success", "ready"}:
            raise RuntimeError(f"隔离邮件归档失败：{provider}")
        save_auto_receive_state(
            cfg.db_path,
            account_id=account_id,
            enabled=False,
            interval_seconds=120,
            last_check_at="2026-07-26 12:01:00",
            last_success_at="2026-07-26 12:01:00",
            last_result="no_changes",
            checkpoint=f"checkpoint-{provider}",
        )

    create_outbound_message(
        cfg.db_path,
        outbound_id=f"outbound-{suffix}",
        sender_account_ref=f"qq:{qq_address}",
        from_account_id=qq_id,
        sender_ref=qq_address,
        source_origin="manual_gui",
        request_id=f"lifecycle-{suffix}",
        subject="Lifecycle outbound",
        body_text="",
        to_emails=[owner_address],
        attachment_count=0,
        link_count=0,
        status="sent",
    )
    account_credentials, account_token = service._account_router.oauth_paths(gmail_id)
    account_credentials.parent.mkdir(parents=True, exist_ok=True)
    account_credentials.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "000000000000-lifecycle.apps.googleusercontent.com",
                    "project_id": "lifecycle-validation",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_secret": "isolated-placeholder",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    account_token.write_text(
        json.dumps(
            {
                "token": "isolated-placeholder",
                "refresh_token": "isolated-placeholder",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "000000000000-lifecycle.apps.googleusercontent.com",
                "client_secret": "isolated-placeholder",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            }
        ),
        encoding="utf-8",
    )
    (home / "Config" / "gui_settings.ini").write_text(
        "[window]\ntheme=dark\n", encoding="utf-8"
    )
    return cfg, gmail_id


def _seed_baseline_from_source(
    source_root: Path,
    home: Path,
    suffix: str,
    env: dict[str, str],
) -> AppConfig:
    source_root = source_root.resolve()
    if not (source_root / "agent_mail_bridge" / "version.py").is_file():
        raise RuntimeError("v1.4.5 基线源码目录无效")
    code = """
import sys
from pathlib import Path
source_root, home, suffix = sys.argv[1:4]
sys.path.insert(0, source_root)
from scripts.release_lifecycle_validation import _close_runtime_handles, _seed_baseline
try:
    _seed_baseline(Path(home), suffix=suffix)
finally:
    _close_runtime_handles()
print("BASELINE_SEEDED")
"""
    completed = _run(
        [sys.executable, "-c", code, str(source_root), str(home), suffix],
        env=env,
        timeout=300,
    )
    if "BASELINE_SEEDED" not in completed.stdout:
        raise RuntimeError("v1.4.5 基线数据创建未完成")
    return _baseline_config(home, suffix)


def _provision_lifecycle_client(cfg: AppConfig) -> dict[str, str]:
    previous = os.environ.get("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE")
    os.environ["AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE"] = "1"
    try:
        service = ApplicationService(cfg)
        initialized = service.initialize()
        if not initialized.ok:
            raise RuntimeError("隔离 Agent Client 初始化失败")
        account_ids = [
            str(item["account_id"])
            for item in service.list_mail_accounts().details.get("accounts", [])
            if item.get("removed_at") is None
        ]
        created = service.create_agent_client(
            client_type="custom",
            display_name="Lifecycle validation",
            config_mode="manual",
            capabilities=["sync.status"],
            account_ids=account_ids[:1],
        )
        if not created.ok:
            raise RuntimeError("隔离 Agent Client 创建失败")
        client_id = str(created.details["client"]["client_id"])
        token = str(created.details["scoped_token"])
        activated = service.set_agent_client_state(
            client_id, "active", enabled=True
        )
        if not activated.ok:
            raise RuntimeError("隔离 Agent Client 启用失败")
        return {
            "AGENT_MAIL_BRIDGE_CLIENT_ID": client_id,
            "AGENT_MAIL_BRIDGE_CLIENT_TOKEN": token,
        }
    finally:
        _close_runtime_handles()
        if previous is None:
            os.environ.pop("AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE", None)
        else:
            os.environ["AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE"] = previous


def _snapshot(home: Path, cfg: AppConfig, credential_exists: bool) -> dict[str, Any]:
    with closing(sqlite3.connect(cfg.db_path)) as connection:
        connection.row_factory = sqlite3.Row
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        schema_row = connection.execute(
            "SELECT schema_version FROM migration_metadata "
            "WHERE migration_key = 'multi_account_core_v1'"
        ).fetchone()
        counts = {
            "accounts": int(
                connection.execute(
                    "SELECT COUNT(*) FROM mail_accounts WHERE removed_at IS NULL"
                ).fetchone()[0]
            ),
            "packages": int(
                connection.execute("SELECT COUNT(*) FROM mail_packages").fetchone()[0]
            ),
            "outbound": int(
                connection.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0]
            ),
            "scheduler": int(
                connection.execute("SELECT COUNT(*) FROM account_sync_states").fetchone()[0]
            ),
        }
        account_ids = sorted(
            str(row[0])
            for row in connection.execute(
                "SELECT account_id FROM mail_accounts "
                "WHERE removed_at IS NULL ORDER BY account_id"
            )
        )
    package_files: dict[str, str] = {}
    mail_root = cfg.received_dir / "mail"
    if mail_root.exists():
        for path in sorted(mail_root.rglob("*")):
            if path.is_file() and (
                path.name == "raw.eml"
                or "attachments" in {part.casefold() for part in path.parts}
            ):
                package_files[path.relative_to(cfg.data_root_path).as_posix()] = (
                    sha256_of_file(path)
                )
    oauth_files = {
        path.relative_to(home).as_posix(): sha256_of_file(path)
        for path in sorted((home / "OAuth").rglob("*.json"))
        if path.is_file()
    }
    return {
        "db_integrity": integrity,
        "schema_version": int(schema_row[0] if schema_row else 0),
        "counts": counts,
        "account_ids": account_ids,
        "raw_eml_count": sum(
            1 for path in package_files if path.endswith("/raw.eml")
        ),
        "attachment_count": sum(
            1 for path in package_files if "/attachments/" in path
        ),
        "package_file_hashes": package_files,
        "oauth_file_hashes": oauth_files,
        "config_exists": (home / "Config" / ".env").is_file(),
        "gui_settings_exists": (home / "Config" / "gui_settings.ini").is_file(),
        "credential_exists": credential_exists,
        "agent_migration_backup_count": len(
            list(
                (cfg.data_root_path / "backups").glob(
                    "*before_v1_5_agent_permissions*.db"
                )
            )
        ),
    }


def _same_persistent_facts(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = (
        "schema_version",
        "counts",
        "account_ids",
        "raw_eml_count",
        "attachment_count",
        "package_file_hashes",
        "oauth_file_hashes",
        "config_exists",
        "gui_settings_exists",
        "credential_exists",
    )
    return all(before.get(key) == after.get(key) for key in keys)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-installer", required=True, type=Path)
    parser.add_argument("--new-installer", required=True, type=Path)
    parser.add_argument("--old-source-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm-isolated-install", action="store_true")
    args = parser.parse_args()
    if not args.confirm_isolated_install:
        raise SystemExit("未确认隔离安装生命周期验收")
    old_installer = args.old_installer.resolve()
    new_installer = args.new_installer.resolve()
    old_source_dir = args.old_source_dir.resolve()
    if not old_installer.is_file() or not new_installer.is_file():
        raise SystemExit("生命周期安装器不存在")

    credential_backend = WindowsCredentialBackend()
    credential_name = f"lifecycle_validation_{uuid.uuid4().hex}"
    credential_value = uuid.uuid4().hex
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "environment": "current Windows user, randomized AppId, isolated app/home paths",
        "old_version": "1.4.5",
        "new_version": "1.5.0",
        "production_install_untouched": True,
        "checks": {},
    }
    final_uninstall_needed = False
    try:
        with tempfile.TemporaryDirectory(prefix="amb-lifecycle-") as temporary:
            root = Path(temporary)
            install_dir = root / "Program Files" / "AgentMailBridge"
            home = root / "User Home"
            env = os.environ.copy()
            env.update(
                {
                    "AGENT_MAIL_BRIDGE_HOME": str(home),
                    "AGENT_MAIL_BRIDGE_CONFIG": str(home / "Config" / ".env"),
                    "AGENT_MAIL_BRIDGE_DISABLE_DOTENV": "0",
                    "AGENT_MAIL_BRIDGE_DISABLE_CREDENTIAL_STORE": "1",
                    "DATA_ROOT": str(home / "Data"),
                    "MCP_MAIL_READ_ENABLED": "false",
                    "ALLOWED_SEND_ROOTS": str(home / "Workspace"),
                }
            )
            baseline_suffix = uuid.uuid4().hex[:10]
            baseline_values = _baseline_values(home, baseline_suffix)
            env.update(baseline_values)
            save_env_values(
                baseline_values, home / "Config" / ".env"
            )
            (home / "Workspace").mkdir(parents=True)
            credential_backend.write(credential_name, credential_value)

            _install(old_installer, install_dir, env)
            final_uninstall_needed = True
            old_probe = _packaged_probe(install_dir, env, "1.4.5")
            try:
                cfg = _seed_baseline_from_source(
                    old_source_dir,
                    home,
                    baseline_suffix,
                    env,
                )
            finally:
                _close_runtime_handles()
            before = _snapshot(
                home,
                cfg,
                credential_backend.read(credential_name) == credential_value,
            )

            _install(new_installer, install_dir, env)
            upgraded_probe = _packaged_probe(
                install_dir, env, "1.5.0", cfg=cfg
            )
            after_upgrade = _snapshot(
                home,
                cfg,
                credential_backend.read(credential_name) == credential_value,
            )

            _uninstall(install_dir, env)
            final_uninstall_needed = False
            after_uninstall = _snapshot(
                home,
                cfg,
                credential_backend.read(credential_name) == credential_value,
            )
            program_removed = not (install_dir / "AgentMailBridge.exe").exists()

            _install(new_installer, install_dir, env)
            final_uninstall_needed = True
            reinstall_probe = _packaged_probe(
                install_dir, env, "1.5.0", cfg=cfg
            )
            after_reinstall = _snapshot(
                home,
                cfg,
                credential_backend.read(credential_name) == credential_value,
            )

            evidence["baseline"] = before
            evidence["checks"] = {
                "old_install": all(old_probe.values()),
                "upgrade_install": all(upgraded_probe.values()),
                "db_integrity": after_upgrade["db_integrity"] == "ok",
                "agent_migration_backup_created": (
                    after_upgrade["agent_migration_backup_count"] > 0
                ),
                "upgrade_persistence": _same_persistent_facts(
                    before, after_upgrade
                ),
                "account_id_preserved": (
                    before["account_ids"] == after_upgrade["account_ids"]
                ),
                "package_hashes_preserved": (
                    before["package_file_hashes"]
                    == after_upgrade["package_file_hashes"]
                ),
                "oauth_files_preserved": (
                    before["oauth_file_hashes"]
                    == after_upgrade["oauth_file_hashes"]
                ),
                "credential_preserved_on_upgrade": bool(
                    after_upgrade["credential_exists"]
                ),
                "program_removed_on_uninstall": program_removed,
                "user_data_preserved_on_uninstall": _same_persistent_facts(
                    before, after_uninstall
                ),
                "credential_preserved_on_uninstall": bool(
                    after_uninstall["credential_exists"]
                ),
                "reinstall_recovery": (
                    all(reinstall_probe.values())
                    and _same_persistent_facts(before, after_reinstall)
                ),
            }
            evidence["counts"] = {
                "before": before["counts"],
                "after_upgrade": after_upgrade["counts"],
                "after_uninstall": after_uninstall["counts"],
                "after_reinstall": after_reinstall["counts"],
            }
            evidence["file_counts"] = {
                "raw_eml": before["raw_eml_count"],
                "attachments": before["attachment_count"],
                "oauth_json": len(before["oauth_file_hashes"]),
            }
            evidence["probes"] = {
                "old": old_probe,
                "upgraded": upgraded_probe,
                "reinstalled": reinstall_probe,
            }
            _uninstall(install_dir, env)
            final_uninstall_needed = False
    finally:
        try:
            credential_backend.delete(credential_name)
        except Exception:
            pass
    evidence["checks"]["test_credential_removed"] = (
        credential_backend.read(credential_name) is None
    )
    evidence["checks"]["no_test_install_left"] = not final_uninstall_needed
    evidence["overall"] = (
        "PASS" if all(evidence["checks"].values()) else "FAIL"
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Release lifecycle validation {evidence['overall']}: {output}")
    return 0 if evidence["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
