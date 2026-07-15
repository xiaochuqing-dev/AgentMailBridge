"""可信域名下载器：HTTPS、固定解析 IP、逐跳校验、限流且不解压。"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import mimetypes
import os
import socket
import ssl
import uuid
from email.message import Message
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlsplit

from agent_mail_bridge.security import assert_within_root
from agent_mail_bridge.utils import sanitize_filename, sha256_of_file, split_ext, unique_path


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_BLOCKED_HOSTS = {
    "localhost", "localhost.localdomain", "metadata", "metadata.google.internal",
}


def normalize_trusted_domain(value: str) -> str:
    raw = (value or "").strip().rstrip(".").casefold()
    if raw.startswith("*."):
        raw = raw[2:]
    if not raw or "://" in raw or any(char in raw for char in "/?#@"):
        raise ValueError("可信域名格式无效")
    try:
        normalized = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("可信域名格式无效") from exc
    if len(normalized) > 253 or "." not in normalized:
        raise ValueError("可信域名必须是完整域名")
    labels = normalized.split(".")
    if any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in labels
    ):
        raise ValueError("可信域名格式无效")
    return normalized


def is_host_trusted(hostname: str, rows: list[dict[str, Any]]) -> bool:
    host = (hostname or "").rstrip(".").casefold()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    for row in rows:
        if not row.get("enabled"):
            continue
        domain = str(row.get("domain") or "").casefold()
        if host == domain:
            return True
        if row.get("include_subdomains") and host.endswith(f".{domain}"):
            return True
    return False


def validate_public_https_target(
    url: str,
    *,
    resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
) -> tuple[str, int, list[str]]:
    """解析并验证一跳 URL；所有解析结果都必须是公网地址。"""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError("下载链接无效") from exc
    if parsed.scheme.casefold() != "https":
        raise ValueError("可信下载只允许 HTTPS")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("下载链接主机无效")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in _BLOCKED_HOSTS or hostname.endswith(".localhost"):
        raise ValueError("下载目标属于本机或元数据地址")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("下载链接端口无效") from exc
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [str(literal)]
    except ValueError:
        try:
            answers = resolver(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"下载目标 DNS 解析失败：{exc}") from exc
        addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
    if not addresses:
        raise ValueError("下载目标没有可用 IP")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("下载目标解析到非公网地址")
    return hostname, port, addresses


def download_trusted_url(
    url: str,
    downloads_dir: Path,
    *,
    max_bytes: int,
    timeout_seconds: int,
    max_redirects: int = 3,
    resolver: Callable[..., list[tuple]] = socket.getaddrinfo,
    connection_factory: Callable[[str, int, str, int], Any] | None = None,
) -> dict[str, Any]:
    """下载一个已由上层 allowlist 批准的直链，并返回可审计事实。"""
    if max_bytes <= 0 or timeout_seconds <= 0 or max_redirects < 0:
        raise ValueError("可信下载限制无效")
    target_dir = Path(downloads_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    current_url = url
    redirects = 0
    response = None
    connection = None
    final_mime = "application/octet-stream"
    original_name = "download"
    try:
        while True:
            hostname, port, addresses = validate_public_https_target(
                current_url, resolver=resolver
            )
            parsed = urlsplit(current_url)
            factory = connection_factory or _default_connection_factory
            connection = factory(hostname, port, addresses[0], timeout_seconds)
            request_path = parsed.path or "/"
            if parsed.query:
                request_path += f"?{parsed.query}"
            connection.request(
                "GET",
                request_path,
                headers={
                    "Host": hostname if port == 443 else f"{hostname}:{port}",
                    "User-Agent": "AgentMailBridge/1.0",
                    "Accept": "*/*",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                response.close()
                connection.close()
                response = None
                connection = None
                if not location:
                    raise ValueError("下载重定向缺少目标")
                if redirects >= max_redirects:
                    raise ValueError("下载重定向次数超限")
                current_url = urljoin(current_url, location)
                redirects += 1
                continue
            if response.status < 200 or response.status >= 300:
                raise ValueError(f"下载服务器返回 HTTP {response.status}")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared = int(content_length)
                except ValueError as exc:
                    raise ValueError("下载 Content-Length 无效") from exc
                if declared < 0 or declared > max_bytes:
                    raise ValueError("下载文件超过大小限制")
            final_mime = (response.getheader("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().casefold()
            if final_mime in {"text/html", "application/xhtml+xml"}:
                raise ValueError("直链返回网页内容，未保存为文件")
            original_name = _response_filename(response, current_url)
            break

        safe_name = _safe_download_filename(original_name, final_mime)
        temporary = target_dir / f".{uuid.uuid4().hex}.part"
        assert_within_root(temporary, target_dir)
        digest = hashlib.sha256()
        total = 0
        try:
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("下载流超过大小限制")
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            stem, extension = split_ext(safe_name)
            final_path = target_dir / safe_name
            if final_path.exists():
                if final_path.stat().st_size == total and sha256_of_file(final_path) == digest.hexdigest():
                    temporary.unlink(missing_ok=True)
                else:
                    final_path = unique_path(target_dir, stem, extension)
                    os.replace(temporary, final_path)
            else:
                os.replace(temporary, final_path)
        finally:
            temporary.unlink(missing_ok=True)
        assert_within_root(final_path, target_dir)
        return {
            "url": current_url,
            "saved_path": str(final_path),
            "saved_filename": final_path.name,
            "original_filename": original_name,
            "mime_type": final_mime,
            "size_bytes": total,
            "sha256": digest.hexdigest(),
            "redirects": redirects,
            "status": "downloaded",
        }
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _response_filename(response: Any, url: str) -> str:
    disposition = response.getheader("Content-Disposition") or ""
    if disposition:
        message = Message()
        message["Content-Disposition"] = disposition
        filename = message.get_filename()
        if filename:
            return unquote(str(filename))
    name = unquote(Path(urlsplit(url).path).name)
    return name or "download"


def _safe_download_filename(original_name: str, mime_type: str) -> str:
    raw = Path(original_name).name or "download"
    stem, extension = split_ext(raw)
    safe_stem = sanitize_filename(stem or "download", max_len=80)
    safe_extension = extension.casefold()
    if not safe_extension:
        safe_extension = mimetypes.guess_extension(mime_type) or ""
    return f"{safe_stem}{safe_extension}"


def _default_connection_factory(
    hostname: str, port: int, ip_address: str, timeout_seconds: int
) -> "_PinnedHTTPSConnection":
    return _PinnedHTTPSConnection(
        hostname, port=port, pinned_ip=ip_address, timeout=timeout_seconds
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS 校验证书域名，但 TCP 只连接已验证过的固定 IP。"""

    def __init__(self, host: str, *, port: int, pinned_ip: str, timeout: int):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self.sock = raw_socket
            self._tunnel()
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
