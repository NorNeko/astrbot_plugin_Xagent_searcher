from typing import List

from astrbot.api import logger


class SecurityACL:
    """
    黑白名单安全访问控制中间件。

    支持三种模式：
    - Off: 关闭 ACL，允许所有访问
    - Whitelist: 白名单模式，仅允许名单中的群组/用户（名单为空时不做限制）
    - Blacklist: 黑名单模式，拒绝名单中的群组/用户

    使用 AstrBot 的 event.unified_msg_origin (UMO) 进行匹配。
    UMO 格式示例：aiocqhttp:GroupMessage:123456、telegram:FriendMessage:789
    用户可通过聊天中的 sid 命令获取完整 UMO。
    """

    def __init__(
        self,
        acl_mode: str = "Off",
        allowed_list: List[str] = None,
        banned_list: List[str] = None,
    ):
        self.acl_mode = acl_mode
        self._allowed_set = self._normalize_to_set(allowed_list or [])
        self._banned_set = self._normalize_to_set(banned_list or [])

        logger.info(
            f"SecurityACL 初始化: mode={self.acl_mode}, "
            f"allowed={len(self._allowed_set)}, banned={len(self._banned_set)}"
        )

    # ------------------------------------------------------------------
    # 条目标准化
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_entry(entry: str) -> str:
        """
        将用户输入的单条条目标准化。
        直接存储用户填写的完整 UMO 字符串（如 aiocqhttp:GroupMessage:123456）。
        """
        return entry.strip()

    @classmethod
    def _normalize_to_set(cls, raw_list: List[str]) -> set:
        """将原始列表中的所有条目标准化并去重，返回集合以加速查找。"""
        result = set()
        for entry in raw_list:
            norm = cls._normalize_entry(str(entry))
            if norm:
                result.add(norm)
        return result

    # ------------------------------------------------------------------
    # 核心鉴权接口
    # ------------------------------------------------------------------

    async def check_access(self, unified_msg_origin: str) -> bool:
        """
        评估当前请求是否被授权访问。

        直接使用 AstrBot 的 event.unified_msg_origin 进行匹配，
        参考 astrbot_plugin_parser 的白名单实现方式。

        Args:
            unified_msg_origin: AstrBot 事件的 unified_msg_origin 字符串

        Returns:
            True 表示允许访问，False 表示拒绝。
        """
        if self.acl_mode == "Off":
            return True

        if self.acl_mode == "Whitelist":
            # 白名单为空时不做限制（与参考插件 astrbot_plugin_parser 行为一致）
            if not self._allowed_set:
                return True
            return unified_msg_origin in self._allowed_set

        if self.acl_mode == "Blacklist":
            return unified_msg_origin not in self._banned_set

        logger.warning(f"未知的 ACL 模式: {self.acl_mode}，默认允许访问")
        return True
