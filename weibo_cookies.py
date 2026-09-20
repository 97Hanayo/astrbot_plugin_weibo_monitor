"""微博 Cookie 文本解析与本地文件持久化工具。"""

from __future__ import annotations

import os
import re
import tempfile
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from time import time
from typing import Dict, Iterable, Tuple


COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def is_weibo_domain(value: str) -> bool:
    """只允许微博官方主域及其子域。"""
    domain = str(value or "").strip().lower().lstrip(".")
    return domain in {"weibo.com", "weibo.cn"} or domain.endswith(
        (".weibo.com", ".weibo.cn")
    )


def _is_safe_cookie_value(value: str) -> bool:
    return not any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def parse_cookie_text(text: str) -> Dict[str, str]:
    """解析 Cookie 请求头或 Netscape cookies.txt 内容。"""
    raw_text = str(text or "").lstrip("\ufeff").strip()
    if not raw_text:
        return {}

    netscape_cookies: Dict[str, str] = {}
    for original_line in raw_text.splitlines():
        line = original_line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            fields = line.split(None, 6)
        if len(fields) != 7:
            continue
        domain, _subdomains, _path, _secure, _expires, name, value = fields
        name = name.strip()
        value = value.strip()
        if (
            is_weibo_domain(domain)
            and COOKIE_NAME_RE.fullmatch(name)
            and _is_safe_cookie_value(value)
        ):
            netscape_cookies[name] = value

    # 避免把 Netscape 文件整行再次误当成 HTTP 请求头。
    if netscape_cookies:
        return netscape_cookies

    header_cookies: Dict[str, str] = {}
    for original_line in raw_text.splitlines():
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip()
        for item in line.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            name, value = item.split("=", 1)
            name = name.strip()
            value = value.strip()
            if COOKIE_NAME_RE.fullmatch(name) and _is_safe_cookie_value(value):
                header_cookies[name] = value
    return header_cookies


def serialize_cookie_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def normalize_cookie_text(text: str) -> str:
    return serialize_cookie_header(parse_cookie_text(text))


def _is_deletion(morsel) -> bool:
    max_age = str(morsel["max-age"] or "").strip()
    if max_age:
        try:
            if int(max_age) <= 0:
                return True
        except ValueError:
            pass
    expires = str(morsel["expires"] or "").strip()
    if expires:
        try:
            if parsedate_to_datetime(expires).timestamp() <= time():
                return True
        except (TypeError, ValueError, OverflowError):
            pass
    return False


def merge_set_cookie_headers(
    current_header: str, set_cookie_headers: Iterable[str], response_host: str
) -> Tuple[str, bool, bool]:
    """合并微博响应 Cookie，返回 (新请求头, 是否收到, 是否变更)。"""
    if not is_weibo_domain(response_host):
        return normalize_cookie_text(current_header), False, False
    cookies = parse_cookie_text(current_header)
    accepted = False
    changed = False
    for header in set_cookie_headers:
        parsed = SimpleCookie()
        try:
            parsed.load(str(header or ""))
        except Exception:
            continue
        for name, morsel in parsed.items():
            domain = str(morsel["domain"] or response_host)
            if (
                not COOKIE_NAME_RE.fullmatch(name)
                or not is_weibo_domain(domain)
                or not _is_safe_cookie_value(morsel.value)
            ):
                continue
            accepted = True
            if _is_deletion(morsel):
                if name in cookies:
                    del cookies[name]
                    changed = True
                continue
            if cookies.get(name) != morsel.value:
                cookies[name] = morsel.value
                changed = True
    return serialize_cookie_header(cookies), accepted, changed


def would_remove_login_cookie(current_header: str, updated_header: str) -> bool:
    """判断一次服务端更新是否会清空现有登录凭据。

    微博在返回 login=false 时可能同时下发删除 Cookie。保留原值比把仍可
    诊断或重新验证的 Cookie 直接覆盖成残缺值更安全。
    """
    current = parse_cookie_text(current_header)
    updated = parse_cookie_text(updated_header)
    if current.get("SUB") and current.get("SUBP"):
        return not (updated.get("SUB") and updated.get("SUBP"))
    if current.get("SUB"):
        return not updated.get("SUB")
    if current.get("SUBP"):
        return not updated.get("SUBP")
    if current.get("WBPSESS"):
        return not updated.get("WBPSESS")
    return False


class WeiboCookieFile:
    """管理插件数据目录下的 cookies/weibo_cookies.txt。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> str:
        try:
            return normalize_cookie_text(self.path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, UnicodeError):
            return ""

    def save(self, cookie_text: str) -> bool:
        normalized = normalize_cookie_text(cookie_text)
        if not normalized:
            return False
        temporary_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(normalized + "\n")
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return True
        except OSError:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
