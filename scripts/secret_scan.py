"""发布产物秘密和用户数据排除检查；不输出任何秘密值。"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from dotenv import dotenv_values


FORBIDDEN_NAMES = {
    ".env",
    "credentials.json",
    "token.json",
    "agent_mail_bridge.db",
    "agent_mail_bridge.db-wal",
    "agent_mail_bridge.db-shm",
}
SECRET_KEYS = ("GMAIL_APP_PASSWORD", "QQ_AUTH_CODE")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    targets = [root / "dist", root / "release"]
    bad_paths: list[str] = []
    files: list[Path] = []
    for target in targets:
        if not target.exists():
            continue
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            files.append(path)
            relative = path.relative_to(root).as_posix()
            lowered_parts = [part.lower() for part in path.parts]
            if path.name.lower() in FORBIDDEN_NAMES or "secrets" in lowered_parts:
                bad_paths.append(relative)
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path) as archive:
                    for name in archive.namelist():
                        parts = [part.lower() for part in Path(name).parts]
                        if Path(name).name.lower() in FORBIDDEN_NAMES or "secrets" in parts:
                            bad_paths.append(f"{relative}!{name}")

    secrets = dotenv_values(root / ".env") if (root / ".env").exists() else {}
    markers = [
        (key, str(secrets.get(key) or "").encode("utf-8"))
        for key in SECRET_KEYS
        if str(secrets.get(key) or "").strip()
    ]
    leaked_secret_keys: set[str] = set()
    for path in files:
        if path.suffix.lower() == ".zip":
            continue
        data = path.read_bytes()
        for key, marker in markers:
            if len(marker) >= 6 and marker in data:
                leaked_secret_keys.add(key)

    if bad_paths or leaked_secret_keys:
        if bad_paths:
            print("Forbidden artifact paths: " + ", ".join(bad_paths), file=sys.stderr)
        if leaked_secret_keys:
            print("Secret values detected for keys: " + ", ".join(sorted(leaked_secret_keys)), file=sys.stderr)
        return 1
    print(f"secret scan PASS: {len(files)} files, {len(markers)} configured secret markers checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
