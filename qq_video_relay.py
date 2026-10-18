"""QQ private-chat video relay helpers for chat-record delivery."""

from __future__ import annotations

from collections.abc import Sequence

from astrbot.api.message_components import Node, Video


class RelayVideoReferenceNode(Node):
    """A forward node that references a video already sent to the relay chat."""

    def toDict(self) -> dict:
        return {"type": "node", "data": {"id": int(self.id or 0)}}

    async def to_dict(self) -> dict:
        return self.toDict()


def parse_qq_private_umo(umo: str) -> tuple[str, int]:
    """Return the platform ID and QQ number from a FriendMessage UMO."""

    parts = str(umo or "").strip().split(":", 2)
    if len(parts) != 3:
        raise ValueError("UMO 格式应为 platform:FriendMessage:QQ号")

    platform_id, message_type, user_id = (part.strip() for part in parts)
    if not platform_id or message_type.casefold() != "friendmessage":
        raise ValueError("中转 UMO 必须是 QQ 私聊会话")
    if not user_id.isdigit():
        raise ValueError("中转 UMO 的私聊 QQ 号必须为数字")
    return platform_id, int(user_id)


def _extract_message_id(result: object) -> int:
    data = result
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    elif hasattr(data, "data"):
        data = getattr(data, "data")

    message_id = None
    if isinstance(data, dict):
        message_id = data.get("message_id")
    elif hasattr(data, "message_id"):
        message_id = getattr(data, "message_id")

    if message_id is None:
        raise ValueError("QQ 私聊发送接口没有返回 message_id")
    try:
        return int(message_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("QQ 私聊发送接口返回了无效的 message_id") from exc


def _get_platform(context, platform_id: str):
    get_platform_inst = getattr(context, "get_platform_inst", None)
    if callable(get_platform_inst):
        return get_platform_inst(platform_id)

    manager = getattr(context, "platform_manager", None)
    for platform in getattr(manager, "platform_insts", ()):
        meta = getattr(platform, "meta", None)
        if callable(meta) and getattr(meta(), "id", None) == platform_id:
            return platform
    return None


async def send_videos_to_private_relay(
    context,
    relay_umo: str,
    videos: Sequence[Video],
) -> list[RelayVideoReferenceNode]:
    """Send videos to the configured QQ private chat and return ID reference nodes.

    Each returned node contains the message ID returned by the send action that
    uploaded its video. This keeps concurrent deliveries associated with their
    own relay messages without searching shared chat history.
    """

    if not videos:
        return []

    platform_id, relay_user_id = parse_qq_private_umo(relay_umo)
    platform = _get_platform(context, platform_id)
    if platform is None:
        raise ValueError(f"未找到中转 UMO 对应的平台实例: {platform_id}")

    get_client = getattr(platform, "get_client", None)
    bot = get_client() if callable(get_client) else getattr(platform, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        raise ValueError("中转平台不支持 OneBot call_action")

    references: list[RelayVideoReferenceNode] = []
    for video in videos:
        payload = await video.to_dict()
        result = await call_action(
            "send_private_msg",
            user_id=relay_user_id,
            message=[payload],
        )
        references.append(
            RelayVideoReferenceNode(id=_extract_message_id(result), content=[])
        )

    return references
