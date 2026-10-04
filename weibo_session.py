"""Validate a mobile Cookie candidate without the monitor's response hooks."""

from .weibo_cookies import (
    merge_set_cookie_headers,
    normalize_cookie_text,
    would_remove_login_cookie,
)


class CookieValidationError(ValueError):
    pass


def login_uid(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("login") is not True:
        raise CookieValidationError("接口未确认登录")
    user = data.get("user") or {}
    uid = data.get("uid") or (user.get("id") if isinstance(user, dict) else None)
    if not uid:
        raise CookieValidationError("登录响应缺少账号 UID")
    return str(uid)


async def validate_mobile_cookie(client, cookie, headers, expected_uid=None):
    """Return (UID, verified header); reject logout, account switches or churn.

    The client must have no response hooks. Every returned header has itself
    passed /api/config, including rotations received during validation.
    """
    candidate = normalize_cookie_text(cookie)
    if not candidate:
        raise CookieValidationError("Cookie 为空")
    identity = str(expected_uid) if expected_uid else None
    for _ in range(3):
        request_headers = dict(headers)
        request_headers["Cookie"] = candidate
        response = await client.get(
            "https://m.weibo.cn/api/config", headers=request_headers,
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise CookieValidationError(f"验证状态码 {response.status_code}")
        try:
            uid = login_uid(response.json())
        except (TypeError, ValueError) as exc:
            raise CookieValidationError("验证响应未确认登录账号") from exc
        if identity is not None and uid != identity:
            raise CookieValidationError("候选 Cookie 的账号不一致")
        identity = uid
        updated, _, changed = merge_set_cookie_headers(
            candidate, response.headers.get_list("set-cookie"), "m.weibo.cn",
            allowed_domains=("weibo.cn",),
        )
        if would_remove_login_cookie(candidate, updated):
            raise CookieValidationError("验证响应删除了登录凭据")
        if not changed:
            return uid, candidate
        candidate = updated
    raise CookieValidationError("验证期间 Cookie 持续变化，暂不保存")
