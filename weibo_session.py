"""Desktop session endpoint and optional account information."""

WEIBO_LOGIN_URL = "https://weibo.com/ajax/config/get_config"


def login_data(payload):
    """Read optional account information without interpreting login state."""
    data = payload.get("data") if isinstance(payload, dict) else None
    return dict(data) if isinstance(data, dict) else {}
