"""Agent 邮件资源读取、轻量预览与受控工作区准备。"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import mimetypes
import os
import shutil
import struct
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_mail_bridge.config import AppConfig
from agent_mail_bridge.security import SecurityError, assert_within_root
from agent_mail_bridge.storage import atomic_copy_file, atomic_write_text
from agent_mail_bridge.utils import sanitize_filename, sha256_of_file


MAX_TEXT_CHARS = 50_000
MAX_CSV_ROWS = 100
MAX_CSV_COLUMNS = 200
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".jsonl", ".yaml", ".yml", ".xml", ".toml", ".ini", ".cfg",
    ".conf", ".log", ".sql", ".html", ".htm", ".css", ".scss", ".sh",
    ".ps1", ".bat", ".cmd", ".java", ".c", ".h", ".cpp", ".hpp", ".go",
    ".rs", ".rb", ".php", ".csv", ".tsv",
}
STRUCTURED_SUFFIXES = {".csv", ".tsv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx"}
TEXT_MIME_TYPES = {
    "application/json", "application/ld+json", "application/xml",
    "application/javascript", "application/x-yaml", "application/toml",
    "application/sql", "application/x-httpd-php",
}


class MailAccessError(Exception):
    """带稳定 error_code 的邮件访问错误。"""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def workspace_dtos(cfg: AppConfig) -> list[dict[str, Any]]:
    roots = [Path(item).resolve() for item in cfg.allowed_send_roots]
    rows = []
    for index, root in enumerate(roots):
        rows.append(
            {
                "workspace_id": _workspace_id(root),
                "display_path": str(root),
                "available": root.is_dir(),
                "default": len(roots) == 1 and index == 0,
            }
        )
    return rows


def workspace_id_for_path(path: Path | str) -> str:
    return _workspace_id(Path(path).resolve())


def resource_capabilities(resource: dict[str, Any]) -> list[str]:
    if str(resource.get("internal_type") or "") == "link":
        return ["link"]
    if not resource.get("absolute_path") or not _resource_available(resource):
        return ["unavailable"]
    suffix = Path(str(resource.get("display_name") or resource.get("absolute_path"))).suffix.lower()
    mime = str(resource.get("mime_type") or "").split(";", 1)[0].strip().lower()
    if suffix in STRUCTURED_SUFFIXES or mime in {"text/csv", "text/tab-separated-values"}:
        return ["structured_preview", "directly_readable"]
    if suffix in IMAGE_SUFFIXES or mime.startswith("image/"):
        return ["visual_file"]
    if suffix in DOCUMENT_SUFFIXES or mime in {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }:
        return ["document_file"]
    if suffix in TEXT_SUFFIXES or mime.startswith("text/") or mime in TEXT_MIME_TYPES:
        return ["directly_readable"]
    return ["binary_file"]


def enrich_resource_descriptor(resource: dict[str, Any]) -> dict[str, Any]:
    result = dict(resource)
    capabilities = resource_capabilities(result)
    result["capabilities"] = capabilities
    result["capability"] = capabilities[0]
    path_text = str(result.get("absolute_path") or "")
    path = Path(path_text) if path_text else None
    result["available"] = bool(path and path.is_file() and "unavailable" not in capabilities)
    if path and path.is_file():
        try:
            result["size_bytes"] = int(result.get("size_bytes") or path.stat().st_size)
            result["sha256"] = str(result.get("sha256") or sha256_of_file(path))
            if "visual_file" in capabilities:
                image = image_metadata(path)
                result.update(image)
        except OSError:
            result["available"] = False
            result["capabilities"] = ["unavailable"]
            result["capability"] = "unavailable"
    return result


def read_mail_resource(
    message: dict[str, Any],
    resource_id: str,
    *,
    mode: str = "preview",
    offset: int = 0,
    max_chars: int = 12_000,
    row_offset: int = 0,
    max_rows: int = 20,
) -> dict[str, Any]:
    safe_offset = _bounded_nonnegative(offset, "offset")
    safe_chars = _bounded_positive(max_chars, "max_chars", MAX_TEXT_CHARS)
    safe_row_offset = _bounded_nonnegative(row_offset, "row_offset")
    safe_rows = _bounded_positive(max_rows, "max_rows", MAX_CSV_ROWS)
    normalized_mode = str(mode or "preview").strip().lower()
    if normalized_mode not in {"text", "preview", "csv_preview", "raw"}:
        raise MailAccessError("invalid_range", "mode 仅支持 text、preview、csv_preview 或 raw")

    resource = _find_resource(message, resource_id)
    path = _resource_path(message, resource, raw=normalized_mode == "raw")
    descriptor = enrich_resource_descriptor(resource)
    descriptor.update(
        {
            "mail_id": str(message.get("package_id") or ""),
            "package_id": str(message.get("package_id") or ""),
            "local_path": str(path),
        }
    )
    if normalized_mode == "preview" and "directly_readable" not in descriptor["capabilities"]:
        return descriptor
    if normalized_mode == "csv_preview":
        if "structured_preview" not in descriptor["capabilities"]:
            raise MailAccessError("unsupported_resource_type", "该资源不是可预览的 CSV/TSV")
        return {
            **descriptor,
            **csv_preview(path, row_offset=safe_row_offset, max_rows=safe_rows),
        }
    if normalized_mode == "raw" and str(resource.get("resource_id")) != "raw.eml":
        raise MailAccessError("unsupported_resource_type", "raw 模式只用于当前邮件的 raw.eml")
    if normalized_mode != "raw" and "directly_readable" not in descriptor["capabilities"]:
        if "binary_file" in descriptor["capabilities"] or "visual_file" in descriptor["capabilities"]:
            raise MailAccessError("binary_resource", "该资源是二进制文件，请使用 preview 或准备到资料目录")
        raise MailAccessError("unsupported_resource_type", "该资源不支持文本读取")
    encoding = detect_text_encoding(path, allow_rfc822=normalized_mode == "raw")
    page = _read_text_page(path, encoding, safe_offset, safe_chars)
    return {**descriptor, **page, "encoding": encoding, "mode": normalized_mode}


def csv_preview(path: Path, *, row_offset: int, max_rows: int) -> dict[str, Any]:
    encoding = detect_text_encoding(path)
    with path.open("r", encoding=encoding, errors="strict", newline="") as stream:
        sample = stream.read(64 * 1024)
        stream.seek(0)
        delimiter = _detect_delimiter(sample, path.suffix.lower())
        reader = csv.reader(stream, delimiter=delimiter)
        headers: list[str] = []
        rows: list[list[str]] = []
        total_rows = 0
        malformed = False
        try:
            for index, row in enumerate(reader):
                normalized = [str(value) for value in row[:MAX_CSV_COLUMNS]]
                if index == 0:
                    headers = normalized
                    continue
                if total_rows >= row_offset and len(rows) < max_rows:
                    rows.append(normalized)
                total_rows += 1
        except csv.Error:
            malformed = True
    return {
        "encoding": encoding,
        "delimiter": {"\t": "tab", ",": "comma", ";": "semicolon"}.get(delimiter, delimiter),
        "columns": headers,
        "column_count": len(headers),
        "row_count": total_rows,
        "row_offset": row_offset,
        "rows": rows,
        "truncated": row_offset + len(rows) < total_rows,
        "malformed": malformed,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_of_file(path),
    }


def prepare_mail_resources(
    cfg: AppConfig,
    message: dict[str, Any],
    resource_ids: list[str] | None,
    *,
    mode: str = "resources",
    target_workspace: str | None = None,
    target_subdir: str | None = None,
    overwrite_policy: str = "rename",
) -> dict[str, Any]:
    normalized_mode = str(mode or "resources").strip().lower()
    if normalized_mode not in {"resources", "complete"}:
        raise MailAccessError(
            "invalid_range", "mode 仅支持 resources 或 complete"
        )
    if normalized_mode == "complete":
        return _prepare_complete_mail_package(
            cfg,
            message,
            target_workspace=target_workspace,
            target_subdir=target_subdir,
            overwrite_policy=overwrite_policy,
        )
    if not resource_ids or not all(isinstance(item, str) and item.strip() for item in resource_ids):
        raise MailAccessError("invalid_range", "resource_ids 必须包含至少一个资源标识")
    policy = str(overwrite_policy or "rename").strip().lower()
    if policy not in {"rename", "error", "overwrite"}:
        raise MailAccessError("invalid_range", "overwrite_policy 仅支持 rename、error 或 overwrite")
    workspace = _select_workspace(cfg, target_workspace)
    package_id = str(message.get("package_id") or "")
    subdir = _safe_subdir(target_subdir)
    package_segment = _safe_package_segment(package_id)
    destination_parts = [".agentmailbridge", "mail", package_segment]
    if subdir:
        destination_parts.extend(subdir.parts)
    destination = _ensure_workspace_directory(workspace, destination_parts)

    prepared: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen: set[str] = set()
    for resource_id in resource_ids:
        if resource_id in seen:
            continue
        seen.add(resource_id)
        try:
            resource = _find_resource(message, resource_id)
            source = _resource_path(message, resource, raw=False)
            expected = str(resource.get("sha256") or "")
            source_sha = sha256_of_file(source)
            if expected and source_sha.casefold() != expected.casefold():
                raise MailAccessError("hash_mismatch", "归档资源 Hash 与事实记录不一致")
            display_name = str(resource.get("display_name") or source.name)
            safe_name = sanitize_filename(Path(display_name).stem) + Path(display_name).suffix.lower()
            target = _collision_target(destination / safe_name, policy)
            atomic_copy_file(source, target)
            copied_sha = sha256_of_file(target)
            if copied_sha != source_sha or target.stat().st_size != source.stat().st_size:
                raise MailAccessError("hash_mismatch", "准备后文件与归档源文件不一致")
            prepared.append(
                {
                    "resource_id": resource_id,
                    "filename": target.name,
                    "source_path": str(source),
                    "prepared_path": str(target),
                    "size_bytes": target.stat().st_size,
                    "sha256": copied_sha,
                }
            )
        except MailAccessError as exc:
            failures.append({"resource_id": resource_id, "error_code": exc.code, "message": exc.message})
        except OSError as exc:
            failures.append({"resource_id": resource_id, "error_code": "preparation_failed", "message": str(exc)})

    note = destination / "邮件说明.md"
    note_text = _mail_note(message, prepared)
    atomic_write_text(note, note_text)
    return {
        "mode": "resources",
        "mail_id": package_id,
        "package_id": package_id,
        "workspace_id": _workspace_id(workspace),
        "workspace_path": str(workspace),
        "target_directory": str(destination),
        "note_path": str(note),
        "prepared": prepared,
        "failures": failures,
        "prepared_count": len(prepared),
        "failed_count": len(failures),
        "status": "partial" if failures and prepared else "failed" if failures else "success",
    }


def _prepare_complete_mail_package(
    cfg: AppConfig,
    message: dict[str, Any],
    *,
    target_workspace: str | None,
    target_subdir: str | None,
    overwrite_policy: str,
) -> dict[str, Any]:
    policy = str(overwrite_policy or "rename").strip().lower()
    if policy not in {"rename", "error", "overwrite"}:
        raise MailAccessError(
            "invalid_range",
            "overwrite_policy 仅支持 rename、error 或 overwrite",
        )
    workspace = _select_workspace(cfg, target_workspace)
    package_id = str(message.get("package_id") or "")
    package_segment = _safe_package_segment(package_id)
    try:
        package_root = Path(
            str(message.get("package_root") or "")
        ).resolve(strict=True)
    except OSError as exc:
        raise MailAccessError(
            "complete_mail_prepare_denied",
            "邮件归档目录不可用",
        ) from exc
    source_snapshot = _archive_snapshot(package_root)
    source_fingerprint = _archive_snapshot_fingerprint(source_snapshot)
    requested_subdir = _safe_subdir(target_subdir)
    relative_destination = requested_subdir or Path("完整邮件资料")
    parent_parts = [
        ".agentmailbridge",
        "mail",
        package_segment,
        *relative_destination.parent.parts,
    ]
    parent = _ensure_workspace_directory(workspace, parent_parts)
    destination = parent / relative_destination.name
    existing_copy_modified = False
    if destination.exists():
        reused = _reusable_complete_copy(
            destination,
            package_id=package_id,
            source_fingerprint=source_fingerprint,
            source_manifest=package_root / "manifest.json",
        )
        if reused is not None:
            return {
                "mode": "complete",
                "mail_id": package_id,
                "package_id": package_id,
                "account_id": str(message.get("account_id") or ""),
                "workspace_id": _workspace_id(workspace),
                "workspace_path": str(workspace),
                "target_directory": str(destination),
                "manifest_path": str(destination / "完整资料manifest.json"),
                "prepared": reused,
                "failures": [],
                "prepared_count": len(reused),
                "failed_count": 0,
                "status": "success",
                "atomic_publish": True,
                "reused": True,
                "existing_copy_modified": False,
                "source_archive_unchanged": True,
                "source_archive_file_count": len(source_snapshot),
            }
        existing_copy_modified = True
        if policy == "error":
            raise MailAccessError(
                "complete_mail_copy_modified",
                "现有完整邮件资料副本已变化，未覆盖；可使用 rename 重新准备到新目录",
            )
    destination = _complete_collision_target(destination, policy)
    # 临时名与中文目标名解耦，避免 Windows 未启用长路径时重复准备越过 MAX_PATH。
    stage = parent / f".ambc-{uuid.uuid4().hex[:12]}.tmp"
    rollback: Path | None = None
    prepared: list[dict[str, Any]] = []
    stage_step = "create_stage"
    try:
        stage.mkdir()
        for name in ("附件", "邮件内图片", "下载文件"):
            (stage / name).mkdir()

        stage_step = "copy_body"
        readable = _readable_body_resource(message)
        body_source = _resource_path(message, readable, raw=False)
        _copy_complete_file(
            body_source,
            stage / "邮件正文.md",
            expected_sha=str(readable.get("sha256") or ""),
            resource_id=str(readable.get("resource_id") or "body.readable"),
            source_relative=str(readable.get("path") or ""),
            prepared=prepared,
        )

        stage_step = "copy_raw"
        raw = _find_resource(message, "raw.eml")
        raw_source = _resource_path(message, raw, raw=True)
        _copy_complete_file(
            raw_source,
            stage / "原始邮件.eml",
            expected_sha=str(raw.get("sha256") or ""),
            resource_id="raw.eml",
            source_relative=str(raw.get("path") or ""),
            prepared=prepared,
        )

        stage_step = "copy_resources"
        for resource in message.get("resources") or []:
            internal_type = str(resource.get("internal_type") or "")
            folder = {
                "attachment": "附件",
                "inline_image": "邮件内图片",
                "downloaded_file": "下载文件",
            }.get(internal_type)
            if not folder or not resource.get("absolute_path"):
                continue
            stage_step = f"copy_resource_{internal_type}"
            source = _resource_path(message, dict(resource), raw=False)
            display_name = str(resource.get("display_name") or source.name)
            stem = sanitize_filename(Path(display_name).stem) or "文件"
            safe_name = stem + Path(display_name).suffix.lower()
            target = _collision_target(stage / folder / safe_name, "rename")
            _copy_complete_file(
                source,
                target,
                expected_sha=str(resource.get("sha256") or ""),
                resource_id=str(resource.get("resource_id") or ""),
                source_relative=str(resource.get("path") or ""),
                prepared=prepared,
            )

        stage_step = "copy_source_manifest"
        original_manifest = package_root / "manifest.json"
        try:
            assert_within_root(
                original_manifest.resolve(strict=True), package_root
            )
        except (OSError, SecurityError) as exc:
            raise MailAccessError(
                "resource_not_local", "原始归档 manifest 不可用"
            ) from exc
        _copy_complete_file(
            original_manifest,
            stage / "原始归档manifest.json",
            expected_sha="",
            resource_id="archive.manifest",
            source_relative="manifest.json",
            prepared=prepared,
        )

        stage_step = "write_info"
        info_path = stage / "邮件信息.json"
        _atomic_write_json(info_path, _complete_mail_info(message))
        prepared.append(_prepared_fact(info_path, "mail.info", ""))

        stage_step = "write_manifest"
        manifest_path = stage / "完整资料manifest.json"
        manifest = _complete_package_manifest(
            message,
            prepared,
            source_archive_fingerprint=source_fingerprint,
        )
        _atomic_write_json(manifest_path, manifest)
        manifest_fact = _prepared_fact(manifest_path, "complete.manifest", "")
        prepared.append(manifest_fact)

        stage_step = "verify_staging"
        for fact in prepared:
            path = stage / str(fact["relative_path"])
            if (
                not path.is_file()
                or path.stat().st_size != int(fact["size_bytes"])
                or sha256_of_file(path) != str(fact["sha256"])
            ):
                raise MailAccessError(
                    "complete_mail_hash_mismatch",
                    "完整邮件资料发布前 Hash 复核失败",
                )
        if _archive_snapshot(package_root) != source_snapshot:
            raise MailAccessError(
                "complete_mail_hash_mismatch",
                "完整邮件资料准备期间源归档发生变化",
            )

        stage_step = "publish"
        if policy == "overwrite" and destination.exists():
            if (
                destination.is_symlink()
                or (
                    hasattr(destination, "is_junction")
                    and destination.is_junction()
                )
                or not destination.is_dir()
            ):
                raise MailAccessError(
                    "complete_mail_prepare_denied",
                    "完整邮件资料目标被不安全对象占用",
                )
            rollback = parent / f".ambc-{uuid.uuid4().hex[:12]}.rollback"
            os.replace(destination, rollback)
        os.replace(stage, destination)
        if rollback is not None:
            shutil.rmtree(rollback, ignore_errors=True)
            rollback = None
    except MailAccessError as exc:
        if rollback is not None and rollback.exists() and not destination.exists():
            os.replace(rollback, destination)
            rollback = None
        if exc.code in {"hash_mismatch", "complete_mail_hash_mismatch"}:
            raise MailAccessError(
                "complete_mail_hash_mismatch",
                "完整邮件资料源文件或复制结果 Hash 不一致",
            ) from exc
        if exc.code in {
            "path_not_allowed",
            "workspace_not_found",
            "workspace_required",
            "complete_mail_prepare_denied",
        }:
            raise MailAccessError(
                "complete_mail_prepare_denied",
                "完整邮件资料准备被目录或 ownership 边界拒绝",
            ) from exc
        raise
    except OSError as exc:
        if rollback is not None and rollback.exists() and not destination.exists():
            try:
                os.replace(rollback, destination)
                rollback = None
            except OSError:
                pass
        raise MailAccessError(
            "complete_mail_prepare_failed",
            "完整邮件资料准备失败",
            os_error_type=type(exc).__name__,
            stage_step=stage_step,
            errno=getattr(exc, "errno", None),
            winerror=getattr(exc, "winerror", None),
        ) from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)

    final_prepared = [
        {
            **fact,
            "prepared_path": str(destination / str(fact["relative_path"])),
        }
        for fact in prepared
    ]
    return {
        "mode": "complete",
        "mail_id": package_id,
        "package_id": package_id,
        "account_id": str(message.get("account_id") or ""),
        "workspace_id": _workspace_id(workspace),
        "workspace_path": str(workspace),
        "target_directory": str(destination),
        "manifest_path": str(destination / "完整资料manifest.json"),
        "prepared": final_prepared,
        "failures": [],
        "prepared_count": len(final_prepared),
        "failed_count": 0,
        "status": "success",
        "atomic_publish": True,
        "reused": False,
        "existing_copy_modified": existing_copy_modified,
        "source_archive_unchanged": True,
        "source_archive_file_count": len(source_snapshot),
    }


def _readable_body_resource(message: dict[str, Any]) -> dict[str, Any]:
    for resource in message.get("resources") or []:
        if str(resource.get("internal_type") or "") == "body_readable":
            return dict(resource)
    body = dict(message.get("body") or {})
    path = str(body.get("readable_path") or "")
    absolute_path = str(body.get("readable_absolute_path") or "")
    if not path and not absolute_path:
        raise MailAccessError(
            "resource_not_local", "当前邮件没有可用的可读正文"
        )
    return {
        "resource_id": "body.readable",
        "package_id": str(message.get("package_id") or ""),
        "internal_type": "body_readable",
        "display_name": "邮件正文.txt",
        "path": path,
        "absolute_path": absolute_path,
        "sha256": body.get("text_sha256"),
        "status": "saved",
    }


def _copy_complete_file(
    source: Path,
    target: Path,
    *,
    expected_sha: str,
    resource_id: str,
    source_relative: str,
    prepared: list[dict[str, Any]],
) -> None:
    source_sha = sha256_of_file(source)
    if expected_sha and source_sha.casefold() != expected_sha.casefold():
        raise MailAccessError(
            "complete_mail_hash_mismatch",
            "完整邮件资料源文件 Hash 与归档事实不一致",
        )
    source_size = source.stat().st_size
    atomic_copy_file(source, target)
    copied_sha = sha256_of_file(target)
    if (
        copied_sha != source_sha
        or target.stat().st_size != source_size
        or sha256_of_file(source) != source_sha
        or source.stat().st_size != source_size
    ):
        raise MailAccessError(
            "complete_mail_hash_mismatch",
            "完整邮件资料复制前后大小或 Hash 不一致",
        )
    prepared.append(
        {
            "resource_id": resource_id,
            "relative_path": target.relative_to(
                next(
                    parent
                    for parent in target.parents
                    if parent.name.endswith(".tmp")
                )
            ).as_posix(),
            "source_relative_path": source_relative,
            "size_bytes": source_size,
            "sha256": copied_sha,
        }
    )


def _prepared_fact(
    path: Path, resource_id: str, source_relative: str
) -> dict[str, Any]:
    stage = next(
        parent for parent in path.parents if parent.name.endswith(".tmp")
    )
    return {
        "resource_id": resource_id,
        "relative_path": path.relative_to(stage).as_posix(),
        "source_relative_path": source_relative,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_of_file(path),
    }


def _archive_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    try:
        package_root = Path(root).resolve(strict=True)
        if (
            not package_root.is_dir()
            or package_root.is_symlink()
            or (
                hasattr(package_root, "is_junction")
                and package_root.is_junction()
            )
        ):
            raise OSError("归档根目录不是普通目录")
        snapshot: dict[str, tuple[int, int, str]] = {}
        for candidate in sorted(
            package_root.rglob("*"), key=lambda item: item.as_posix()
        ):
            if candidate.is_symlink() or (
                hasattr(candidate, "is_junction")
                and candidate.is_junction()
            ):
                raise OSError("归档内包含符号链接或目录联接")
            if not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            assert_within_root(resolved, package_root)
            before = resolved.stat()
            digest = sha256_of_file(resolved)
            after = resolved.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise OSError("归档文件读取期间发生变化")
            snapshot[resolved.relative_to(package_root).as_posix()] = (
                after.st_size,
                after.st_mtime_ns,
                digest,
            )
        return snapshot
    except (OSError, SecurityError) as exc:
        raise MailAccessError(
            "complete_mail_prepare_denied",
            "邮件归档包含不安全路径或读取期间发生变化",
        ) from exc


def _archive_snapshot_fingerprint(
    snapshot: dict[str, tuple[int, int, str]]
) -> str:
    digest = hashlib.sha256()
    for relative_path, (size_bytes, _mtime_ns, file_sha256) in sorted(
        snapshot.items()
    ):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _complete_mail_info(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "package_id": str(message.get("package_id") or ""),
        "account_id": str(message.get("account_id") or ""),
        "account_ref": str(message.get("account_ref") or ""),
        "mailbox_ref": str(message.get("mailbox_ref") or ""),
        "message_id": str(message.get("message_id") or ""),
        "provider_message_id": str(message.get("provider_message_id") or ""),
        "thread_ref": str(message.get("thread_ref") or ""),
        "subject": str(message.get("subject") or ""),
        "from": message.get("from_addresses") or message.get("from") or [],
        "to": message.get("to_addresses") or message.get("to") or [],
        "cc": message.get("cc_addresses") or message.get("cc") or [],
        "bcc": message.get("bcc_addresses") or message.get("bcc") or [],
        "sent_at": message.get("sent_at"),
        "received_at": message.get("received_at"),
        "saved_at": message.get("saved_at"),
        "counts": dict(message.get("counts") or {}),
        "archive_status": str(message.get("archive_status") or ""),
        "parse_status": str(message.get("parse_status") or ""),
    }


def _complete_package_manifest(
    message: dict[str, Any],
    prepared: list[dict[str, Any]],
    *,
    source_archive_fingerprint: str,
) -> dict[str, Any]:
    resources = []
    for resource in message.get("resources") or []:
        resources.append(
            {
                key: resource.get(key)
                for key in (
                    "resource_id",
                    "category",
                    "internal_type",
                    "source",
                    "display_name",
                    "original_name",
                    "mime_type",
                    "path",
                    "url",
                    "content_id",
                    "size_bytes",
                    "sha256",
                    "status",
                )
            }
        )
    return {
        "schema_version": 2,
        "package_id": str(message.get("package_id") or ""),
        "account_id": str(message.get("account_id") or ""),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_archive_immutable": True,
        "source_archive_fingerprint": source_archive_fingerprint,
        "files": [dict(item) for item in prepared],
        "resources": resources,
    }


def _reusable_complete_copy(
    destination: Path,
    *,
    package_id: str,
    source_fingerprint: str,
    source_manifest: Path,
) -> list[dict[str, Any]] | None:
    if (
        destination.is_symlink()
        or (
            hasattr(destination, "is_junction")
            and destination.is_junction()
        )
        or not destination.is_dir()
    ):
        raise MailAccessError(
            "complete_mail_prepare_denied",
            "完整邮件资料目标不能是符号链接、目录联接或普通文件",
        )
    manifest_path = destination / "完整资料manifest.json"
    try:
        if (
            manifest_path.is_symlink()
            or (
                hasattr(manifest_path, "is_junction")
                and manifest_path.is_junction()
            )
            or not manifest_path.is_file()
        ):
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or str(manifest.get("package_id") or "") != package_id
        ):
            return None
        recorded_fingerprint = str(
            manifest.get("source_archive_fingerprint") or ""
        )
        files = manifest.get("files")
        if not isinstance(files, list):
            return None
        prepared: list[dict[str, Any]] = []
        original_manifest_sha = ""
        for raw_fact in files:
            if not isinstance(raw_fact, dict):
                return None
            relative_text = str(raw_fact.get("relative_path") or "")
            relative = Path(relative_text)
            if (
                not relative_text
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                return None
            candidate = destination.joinpath(relative)
            if (
                candidate.is_symlink()
                or (
                    hasattr(candidate, "is_junction")
                    and candidate.is_junction()
                )
                or not candidate.is_file()
            ):
                return None
            resolved = candidate.resolve(strict=True)
            assert_within_root(resolved, destination)
            expected_size = int(raw_fact.get("size_bytes"))
            expected_sha = str(raw_fact.get("sha256") or "")
            if (
                resolved.stat().st_size != expected_size
                or not expected_sha
                or sha256_of_file(resolved) != expected_sha
            ):
                return None
            if str(raw_fact.get("resource_id") or "") == "archive.manifest":
                original_manifest_sha = expected_sha
            prepared.append(
                {
                    **raw_fact,
                    "prepared_path": str(resolved),
                }
            )
        if recorded_fingerprint:
            if recorded_fingerprint != source_fingerprint:
                return None
        else:
            if (
                not original_manifest_sha
                or not source_manifest.is_file()
                or sha256_of_file(source_manifest) != original_manifest_sha
            ):
                return None
        prepared.append(
            {
                "resource_id": "complete.manifest",
                "relative_path": "完整资料manifest.json",
                "source_relative_path": "",
                "size_bytes": manifest_path.stat().st_size,
                "sha256": sha256_of_file(manifest_path),
                "prepared_path": str(manifest_path),
            }
        )
        return prepared
    except (
        json.JSONDecodeError,
        OSError,
        SecurityError,
        TypeError,
        ValueError,
    ):
        return None


def _complete_collision_target(path: Path, policy: str) -> Path:
    if not path.exists() or policy == "overwrite":
        return path
    if path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    ):
        raise MailAccessError(
            "complete_mail_prepare_denied",
            "完整邮件资料目标不能是符号链接或目录联接",
        )
    if policy == "error":
        raise MailAccessError(
            "complete_mail_prepare_failed",
            f"完整邮件资料目标已存在：{path.name}",
        )
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.name} ({index})")
        if not candidate.exists():
            return candidate
    raise MailAccessError(
        "complete_mail_prepare_failed", "无法生成安全的完整邮件资料目录名"
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def image_metadata(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = stream.read(64 * 1024)
    width = height = None
    image_format = ""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        image_format = "PNG"
    elif data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        image_format = "GIF"
    elif data.startswith(b"BM") and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        height = abs(height)
        image_format = "BMP"
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
        image_format = "JPEG"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_dimensions(data)
        image_format = "WEBP"
    if not width or not height:
        raise MailAccessError("unsupported_resource_type", "图片格式无效或无法读取尺寸")
    return {"format": image_format, "width": int(width), "height": int(height)}


def detect_text_encoding(path: Path, *, allow_rfc822: bool = False) -> str:
    with path.open("rb") as stream:
        sample = stream.read(128 * 1024)
    if not sample:
        return "utf-8"
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith(codecs.BOM_UTF16_LE):
        return "utf-16"
    if sample.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if not allow_rfc822 and _looks_binary(sample):
        raise MailAccessError("binary_resource", "资源内容检测为二进制，已拒绝文本解码")
    scored: list[tuple[float, int, str]] = []
    for order, encoding in enumerate(("utf-8", "gb18030", "gbk", "big5")):
        try:
            text = sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        score = _text_score(text) - order * 0.01
        if encoding == "utf-8":
            score += 2.0
        scored.append((score, -order, encoding))
    if not scored:
        if allow_rfc822:
            return "latin-1"
        raise MailAccessError("binary_resource", "资源无法使用受支持的文本编码严格解码")
    return max(scored)[2]


def _read_text_page(path: Path, encoding: str, offset: int, max_chars: int) -> dict[str, Any]:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    start = offset
    end = offset + max_chars
    total = 0
    pieces: list[str] = []
    with path.open("rb") as stream:
        while True:
            block = stream.read(64 * 1024)
            final = not block
            text = decoder.decode(block, final=final)
            if text:
                block_start = total
                block_end = total + len(text)
                if block_end > start and block_start < end:
                    pieces.append(text[max(0, start - block_start):min(len(text), end - block_start)])
                total = block_end
            if final:
                break
    if offset > total:
        raise MailAccessError("invalid_range", "offset 超出资源字符范围", character_count=total)
    content = "".join(pieces)
    next_offset = offset + len(content)
    return {
        "content": content,
        "character_count": total,
        "offset": offset,
        "next_offset": next_offset if next_offset < total else None,
        "has_more": next_offset < total,
        "returned_chars": len(content),
        "bytes_returned": len(content.encode("utf-8")),
    }


def _find_resource(message: dict[str, Any], resource_id: str) -> dict[str, Any]:
    package_id = str(message.get("package_id") or "")
    if resource_id == "raw.eml":
        raw = dict(message.get("raw_eml") or {})
        if str(raw.get("status") or "") in {"legacy_missing", "missing", ""} or not raw.get("path"):
            raise MailAccessError("resource_not_local", "当前邮件没有可用的真实 raw.eml")
        return {
            "resource_id": "raw.eml",
            "package_id": package_id,
            "internal_type": "raw_eml",
            "display_name": "raw.eml",
            "mime_type": "message/rfc822",
            "path": raw.get("path"),
            "absolute_path": str(Path(str(message.get("package_root") or "")) / str(raw.get("path"))),
            "sha256": raw.get("sha256"),
            "status": raw.get("status"),
        }
    for resource in message.get("resources") or []:
        if str(resource.get("resource_id") or "") == resource_id:
            if str(resource.get("package_id") or package_id) != package_id:
                break
            return dict(resource)
    raise MailAccessError("resource_not_found", "指定资源不属于当前邮件或不存在")


def _resource_path(message: dict[str, Any], resource: dict[str, Any], *, raw: bool) -> Path:
    root_text = str(message.get("package_root") or "")
    if not root_text:
        raise MailAccessError("resource_not_local", "邮件归档目录不可用")
    root = Path(root_text).resolve()
    if raw and resource.get("resource_id") != "raw.eml":
        raise MailAccessError("unsupported_resource_type", "raw 模式只用于 raw.eml")
    path_text = str(resource.get("absolute_path") or "")
    candidate = Path(path_text) if path_text else root / str(resource.get("path") or "")
    try:
        candidate = candidate.resolve(strict=True)
        assert_within_root(candidate, root)
    except (OSError, SecurityError) as exc:
        raise MailAccessError("path_not_allowed", "资源路径不在当前邮件归档内") from exc
    if not candidate.is_file():
        raise MailAccessError("resource_not_local", "资源本地文件不存在")
    expected_sha = str(resource.get("sha256") or "").strip().casefold()
    if expected_sha and sha256_of_file(candidate).casefold() != expected_sha:
        raise MailAccessError("hash_mismatch", "归档资源 Hash 与事实记录不一致")
    return candidate


def _select_workspace(cfg: AppConfig, requested: str | None) -> Path:
    rows = workspace_dtos(cfg)
    available = [row for row in rows if row["available"]]
    if requested:
        for row in rows:
            if requested in {row["workspace_id"], row["display_path"]}:
                if not row["available"]:
                    raise MailAccessError("workspace_not_found", "授权资料目录当前不可用")
                return Path(row["display_path"]).resolve()
        raise MailAccessError("workspace_not_found", "未找到指定的授权资料目录")
    if len(available) == 1:
        return Path(available[0]["display_path"]).resolve()
    if not available:
        raise MailAccessError("workspace_required", "请先在 Agent 接入页面授权一个资料目录", workspaces=rows)
    raise MailAccessError("workspace_required", "存在多个授权资料目录，请明确指定 target_workspace", workspaces=available)


def _safe_subdir(value: str | None) -> Path | None:
    if not value or not str(value).strip():
        return None
    path = Path(str(value).strip())
    if path.is_absolute() or path.anchor or any(part in {"", ".", ".."} for part in path.parts):
        raise MailAccessError("path_not_allowed", "target_subdir 必须是资料目录内的安全相对目录")
    return path


def _safe_package_segment(value: str) -> str:
    text = str(value or "").strip()
    path = Path(text)
    if (
        not text
        or path.is_absolute()
        or path.anchor
        or len(path.parts) != 1
        or path.parts[0] in {"", ".", ".."}
        or "/" in text
        or "\\" in text
    ):
        raise MailAccessError("path_not_allowed", "邮件标识不能用作安全准备目录")
    return text


def _ensure_workspace_directory(workspace: Path, parts: list[str]) -> Path:
    """逐级解析工作区目录，拒绝已有联接/符号链接逃逸后再写入。"""
    try:
        root = workspace.resolve(strict=True)
    except OSError as exc:
        raise MailAccessError("workspace_not_found", "授权资料目录当前不可用") from exc
    current = root
    for part in parts:
        candidate = current / part
        try:
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or (
                    hasattr(candidate, "is_junction")
                    and candidate.is_junction()
                ):
                    raise MailAccessError(
                        "path_not_allowed",
                        "准备目录不能经过符号链接或目录联接",
                    )
                resolved = candidate.resolve(strict=True)
                if not resolved.is_dir():
                    raise MailAccessError("preparation_failed", "准备目录被同名文件占用")
            else:
                candidate.mkdir()
                resolved = candidate.resolve(strict=True)
            assert_within_root(resolved, root)
        except SecurityError as exc:
            raise MailAccessError("path_not_allowed", "准备目录超出授权资料目录") from exc
        except MailAccessError:
            raise
        except OSError as exc:
            raise MailAccessError("preparation_failed", "无法创建安全准备目录") from exc
        current = resolved
    return current


def _collision_target(path: Path, policy: str) -> Path:
    if not path.exists() or policy == "overwrite":
        return path
    if policy == "error":
        raise MailAccessError("preparation_failed", f"目标文件已存在：{path.name}")
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise MailAccessError("preparation_failed", "无法生成安全的同名冲突文件名")


def _mail_note(message: dict[str, Any], prepared: list[dict[str, Any]]) -> str:
    body = " ".join(str(message.get("body_summary") or "").split())[:500]
    lines = [
        f"邮件主题：{message.get('subject') or '（无主题）'}",
        f"发件人：{message.get('from') or '（未知）'}",
        f"邮件时间：{message.get('sent_at') or message.get('received_at') or '（未知）'}",
        f"邮件标识：{message.get('package_id') or ''}",
        "",
        "正文摘要：",
        body or "（无可读正文）",
        "",
        "已准备资源：",
    ]
    if not prepared:
        lines.append("（无）")
    for item in prepared:
        lines.append(f"{item['filename']}  来源资源 {item['resource_id']}  SHA-256 {item['sha256']}")
    return "\n".join(lines).rstrip() + "\n"


def _resource_available(resource: dict[str, Any]) -> bool:
    return str(resource.get("status") or "").strip().lower() not in {
        "failed", "missing", "download_failed", "legacy_missing", "unavailable"
    }


def _workspace_id(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).encode("utf-8")
    return "workspace-" + hashlib.sha256(normalized).hexdigest()[:16]


def _bounded_nonnegative(value: int, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MailAccessError("invalid_range", f"{field} 必须是非负整数") from exc
    if number < 0:
        raise MailAccessError("invalid_range", f"{field} 必须是非负整数")
    return number


def _bounded_positive(value: int, field: str, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MailAccessError("invalid_range", f"{field} 必须是正整数") from exc
    if number <= 0 or number > maximum:
        raise MailAccessError("invalid_range", f"{field} 必须在 1 到 {maximum} 之间")
    return number


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    if sample.startswith((b"MZ", b"PK\x03\x04", b"%PDF", b"\x89PNG", b"\xff\xd8", b"RIFF")):
        return True
    controls = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return controls / max(1, len(sample)) > 0.02


def _text_score(value: str) -> float:
    if not value:
        return 0.0
    readable = sum(character.isprintable() or character in "\r\n\t" for character in value)
    replacement = value.count("\ufffd")
    private = sum(0xE000 <= ord(character) <= 0xF8FF for character in value)
    cjk = sum("\u3400" <= character <= "\u9fff" for character in value)
    mojibake = sum(value.count(token) for token in ("锟", "烫", "屯", "鈥", "馃"))
    return readable / len(value) * 8 + min(cjk, 80) * 0.02 - replacement * 4 - private * 0.5 - mojibake


def _detect_delimiter(sample: str, suffix: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except csv.Error:
        return "\t" if suffix == ".tsv" else ","


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and index + 7 <= len(data):
            return int.from_bytes(data[index + 5:index + 7], "big"), int.from_bytes(data[index + 3:index + 5], "big")
        index += max(2, length)
    return None, None


def _webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None, None
