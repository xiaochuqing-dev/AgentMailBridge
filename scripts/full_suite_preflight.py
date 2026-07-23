"""在完整 pytest 前执行版本、能力、schema、语法和定向回归检查。"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_mail_bridge.database import MULTI_ACCOUNT_SCHEMA_VERSION
from agent_mail_bridge.provider_adapters import get_provider_adapter
from agent_mail_bridge.provider_foundation import PROVIDER_PROFILES
from agent_mail_bridge.version import __version__


TARGET_VERSION = "1.4.4"
TARGETED_TESTS = (
    "tests/test_v1_4_3_provider_hardening.py",
    "tests/test_v1_4_2_generic_mail.py",
    "tests/test_v1_4_1_multi_account_runtime.py",
    "tests/test_v1_4_1_multi_account_ui.py",
    "tests/test_account_management_ui.py",
    "tests/test_windows_productization.py",
)


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _run(command: list[str], label: str) -> None:
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")
    print(f"PASS {label}")


def _check_version_consistency() -> None:
    _require(__version__ == TARGET_VERSION, "version.py is not v1.4.4")
    version_tuple = tuple(int(part) for part in TARGET_VERSION.split(".")) + (0,)
    tuple_text = ", ".join(str(part) for part in version_tuple)
    for relative in (
        "packaging/windows/version_info.txt",
        "packaging/windows/version_info_mcp.txt",
    ):
        content = _text(relative)
        _require(
            f"filevers=({tuple_text})" in content
            and f"prodvers=({tuple_text})" in content
            and f"u'FileVersion', u'{TARGET_VERSION}'" in content
            and f"u'ProductVersion', u'{TARGET_VERSION}'" in content,
            f"stale Windows version metadata in {relative}",
        )
    _require(
        f'#define MyAppVersion "{TARGET_VERSION}"'
        in _text("packaging/windows/AgentMailBridge.iss"),
        "stale installer version",
    )
    markers = {
        "README.md": f"AgentMailBridge v{TARGET_VERSION}",
        "CHANGELOG.md": f"## {TARGET_VERSION} ",
        "AGENTS.md": f"AgentMailBridge v{TARGET_VERSION} is the current product version",
        "docs/GUI使用说明.md": f"AgentMailBridge {TARGET_VERSION}",
        "docs/MCP使用说明.md": f"AgentMailBridge v{TARGET_VERSION}",
        "docs/Windows安装与升级说明.md": f"AgentMailBridge-{TARGET_VERSION}-Setup.exe",
        "docs/发布检查清单.md": f"v{TARGET_VERSION}",
    }
    for relative, marker in markers.items():
        _require(marker in _text(relative), f"missing current version marker in {relative}")
    product_test = _text("tests/test_windows_productization.py")
    _require(
        f'assert __version__ == "{TARGET_VERSION}"' in product_test
        and f"filevers=({tuple_text})" in product_test,
        "stale version assertion in productization tests",
    )
    stale_assertions: list[str] = []
    pattern = re.compile(r'__version__\s*==\s*"([^"]+)"')
    for path in (ROOT / "tests").glob("test_*.py"):
        for value in pattern.findall(path.read_text(encoding="utf-8")):
            if value != TARGET_VERSION:
                stale_assertions.append(f"{path.name}:{value}")
    _require(not stale_assertions, f"stale version assertions: {stale_assertions}")
    print("PASS version consistency")


def _check_provider_status() -> None:
    profile_statuses = {
        profile.profile_id: profile.status for profile in PROVIDER_PROFILES
    }
    expected = "supported"
    for provider in ("qq", "163"):
        adapter = get_provider_adapter(provider)
        _require(adapter.status == expected, f"{provider} adapter status was promoted")
        _require(
            profile_statuses.get(provider) == expected,
            f"{provider} profile and adapter status differ",
        )
    generic_expected = "implementation_ready_e2e_required"
    generic = get_provider_adapter("generic_imap_smtp")
    _require(
        generic.status == generic_expected,
        "Generic status was promoted without E2E",
    )
    matrix = _text("docs/Provider 支持矩阵与 QQ 163 配置说明.md")
    for provider in ("QQ", "163", "Generic"):
        _require(provider in matrix, f"{provider} missing from Provider matrix")
    _require(
        "NOT_TESTED" in matrix
        and expected in matrix
        and generic_expected in matrix,
        "Provider matrix does not distinguish implementation from real E2E",
    )
    print("PASS Provider status consistency")


def _check_schema_consistency() -> None:
    _require(
        MULTI_ACCOUNT_SCHEMA_VERSION == 3,
        "unexpected Multi-Account schema version",
    )
    runtime_tests = _text("tests/test_v1_4_1_multi_account_runtime.py")
    _require(
        "assert schema_version == 3" in runtime_tests,
        "schema migration assertion is stale",
    )
    print("PASS schema consistency")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()
    try:
        _check_version_consistency()
        _check_provider_status()
        _check_schema_consistency()
        _run(["git", "diff", "--check"], "git diff --check")
        _run(
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "agent_mail_bridge",
                "scripts",
            ],
            "compileall",
        )
        if not args.skip_tests:
            _run(
                [sys.executable, "-m", "pytest", "-q", *TARGETED_TESTS],
                "targeted pytest",
            )
    except RuntimeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print("Full Suite Preflight PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
