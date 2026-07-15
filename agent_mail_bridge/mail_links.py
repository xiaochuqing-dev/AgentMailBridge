"""离线识别邮件中的网页、文件、云文档和外部图片链接。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit, urlunsplit


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING = ".,;:!?，。；：！？)]}）】>"
_DIRECT_FILE_EXTENSIONS = {
    ".7z", ".avi", ".csv", ".doc", ".docx", ".gif", ".gz", ".jpeg",
    ".jpg", ".json", ".md", ".mov", ".mp3", ".mp4", ".pdf", ".png",
    ".ppt", ".pptx", ".rar", ".rtf", ".tar", ".text", ".tif", ".tiff",
    ".tsv", ".txt", ".wav", ".webp", ".xls", ".xlsx", ".xml", ".zip",
}
_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}
_CLOUD_HOSTS = {
    "1drv.ms", "docs.google.com", "drive.google.com", "dropbox.com",
    "notion.site", "notion.so", "onedrive.live.com", "sharepoint.com",
}


def detect_mail_links(plain_text: str, html_text: str) -> list[dict[str, str]]:
    """只做本地字符串分析；函数内没有任何网络访问。"""
    candidates: list[tuple[str, str, str]] = []
    for match in _URL_RE.finditer(plain_text or ""):
        candidates.append((match.group(0).rstrip(_TRAILING), "plain_text", ""))
    parser = _LinkHTMLParser()
    if html_text:
        try:
            parser.feed(html_text)
            parser.close()
        except (TypeError, ValueError):
            pass
    candidates.extend(parser.links)

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url, source_type, anchor_text in candidates:
        item = classify_mail_link(raw_url, source_type=source_type, anchor_text=anchor_text)
        if item is None:
            continue
        key = item["url"].casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def classify_mail_link(
    raw_url: str, *, source_type: str, anchor_text: str = ""
) -> dict[str, str] | None:
    value = (raw_url or "").strip().rstrip(_TRAILING)
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    hostname = parsed.hostname.rstrip(".").casefold()
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = hostname if port is None else f"{hostname}:{port}"
    normalized = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
    suffix = PurePosixPath(unquote(parsed.path)).suffix.casefold()
    cloud = any(hostname == host or hostname.endswith(f".{host}") for host in _CLOUD_HOSTS)
    if cloud:
        link_type = "cloud_document"
        status = "login_may_be_required"
    elif source_type == "html_image" or suffix in _IMAGE_EXTENSIONS:
        link_type = "image_link"
        status = "recognized"
    elif suffix in _DIRECT_FILE_EXTENSIONS:
        link_type = "downloadable_file"
        status = "recognized"
    else:
        link_type = "webpage"
        status = "recognized"
    path_name = unquote(PurePosixPath(parsed.path).name)
    display_name = (anchor_text or "").strip() or path_name or hostname
    return {
        "url": normalized,
        "hostname": hostname,
        "link_type": link_type,
        "source_type": source_type,
        "display_name": display_name,
        "status": status,
    }


class _LinkHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []
        self._anchor_url = ""
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "a" and values.get("href"):
            self._anchor_url = values["href"]
            self._anchor_text = []
        elif lowered == "img" and values.get("src"):
            src = values["src"].strip()
            if not src.casefold().startswith("cid:"):
                self.links.append((src, "html_image", values.get("alt", "")))

    def handle_data(self, data: str) -> None:
        if self._anchor_url:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._anchor_url:
            self.links.append(
                (self._anchor_url, "html_href", "".join(self._anchor_text).strip())
            )
            self._anchor_url = ""
            self._anchor_text = []
