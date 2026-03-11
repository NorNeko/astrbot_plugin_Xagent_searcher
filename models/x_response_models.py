"""
X API v2 响应数据模型定义
基于 Pydantic v2.4+ 实现类型安全与自动验证

规范文档参考：
- 关系型数据水合与扩展字段映射
- 多维度的变体解析与最优码率择优
- 执行上下文管理与防污染压缩
- 速率限制规避与分页调度算法

官方 API 文档：https://docs.x.com/x-api/
AstrBot 开发: https://docs.astrbot.app/dev/star/guides/ai.html
"""

from __future__ import annotations

import functools
from typing import Any
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, field_validator


# ============================================================================
# 基础模型配置
# ============================================================================

class XBaseModel(BaseModel):
    """
    所有 X API 模型的公共基类。

    ConfigDict 策略：
    - extra="ignore"      : 忽略 API 返回中未定义的字段，防止 X API 新增字段导致解析崩溃
    - populate_by_name    : 同时支持字段别名与原名赋值
    - str_strip_whitespace: 自动去除字符串首尾空白
    """
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# ============================================================================
# 速率限制信息模型
# ============================================================================

class RateLimitInfo(XBaseModel):
    """
    结构化的速率限制信息模型。

    从 X API 响应头中提取 x-rate-limit-* 字段，
    供断路器状态机判定是否触发限流保护。

    规范文档参考：速率限制规避与分页调度算法
    """
    remaining: int | None = Field(None, description="当前 15 分钟窗口内剩余请求次数")
    limit: int | None = Field(None, description="当前 15 分钟窗口的总请求上限")
    reset_at: int | None = Field(None, description="速率限制重置的 Unix 时间戳")

    @property
    def is_near_limit(self) -> bool:
        """剩余请求 < 5 时触发断路器（规范安全阈值）"""
        return self.remaining is not None and self.remaining < 5

    @property
    def reset_datetime(self) -> datetime | None:
        """将 Unix 时间戳转为 datetime 对象"""
        if self.reset_at is not None:
            return datetime.fromtimestamp(self.reset_at)
        return None

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> RateLimitInfo:
        """
        从 HTTP 响应头字典中提取速率限制信息。

        X API 响应头包含：
        - x-rate-limit-remaining : 剩余额度
        - x-rate-limit-limit     : 窗口总额度
        - x-rate-limit-reset     : 重置时间戳
        """
        def _safe_int(val: Any) -> int | None:
            if val is None:
                return None
            try:
                return int(val)
            except (ValueError, TypeError):
                return None

        return cls(
            remaining=_safe_int(headers.get("x-rate-limit-remaining")),
            limit=_safe_int(headers.get("x-rate-limit-limit")),
            reset_at=_safe_int(headers.get("x-rate-limit-reset")),
        )


# ============================================================================
# 基础数据结构
# ============================================================================

class MediaVariant(XBaseModel):
    """
    媒体变体模型（用于视频/GIF 的多分辨率表示）

    X API 对视频与 GIF 采取多变体存储策略，返回包含多种分辨率和编码格式的数组。
    解析算法需首先过滤 video/mp4 变体，排除 HLS (application/x-mpegURL)。

    规范文档参考：多维度的变体解析与最优码率择优
    """
    content_type: str = Field(..., description="媒体类型，如 video/mp4, application/x-mpegURL")
    url: str = Field(..., description="变体直链地址")
    bit_rate: int | None = Field(None, description="比特率（仅视频，单位 bps）")

    @property
    def is_mp4(self) -> bool:
        """是否为 MP4 格式（排除 HLS 流媒体）"""
        return self.content_type.lower() == "video/mp4"

    @property
    def is_hls(self) -> bool:
        """是否为 HLS 流媒体格式"""
        return "mpegurl" in self.content_type.lower()


class PublicMetrics(XBaseModel):
    """
    推文互动指标模型

    规范文档参考：执行上下文管理与防污染压缩
    """
    like_count: int = Field(default=0, description="点赞数")
    retweet_count: int = Field(default=0, description="转发数")
    reply_count: int = Field(default=0, description="回复数")
    impression_count: int | None = Field(None, description="展示数")
    bookmark_count: int | None = Field(None, description="书签数")
    quote_count: int | None = Field(None, description="引用数")

    def to_display_str(self) -> str:
        """生成简洁的互动指标展示字符串，用于 LLM 上下文压缩"""
        return f"❤️{self.like_count} 🔄{self.retweet_count} 💬{self.reply_count}"


class UserPublicMetrics(XBaseModel):
    """
    用户级互动指标模型（区别于推文级 PublicMetrics）

    X API v2 中用户公共指标结构与推文指标不同，
    包含粉丝数、关注数、推文总数等账户级数据。
    """
    followers_count: int = Field(default=0, description="粉丝数")
    following_count: int = Field(default=0, description="关注数")
    tweet_count: int = Field(default=0, description="推文总数")
    listed_count: int | None = Field(None, description="被列入列表数")

    def to_display_str(self) -> str:
        """生成简洁的用户指标展示字符串"""
        return f"粉丝:{self.followers_count} 关注:{self.following_count} 推文:{self.tweet_count}"


class Media(XBaseModel):
    """
    媒体对象模型（图片/视频/GIF）

    X API v2 将媒体实体与推文实体解耦，通过 media_key 在 includes.media 中关联。
    解析器需在推文 attachments 中发现 media_key 后，到 includes.media 中执行哈希匹配。

    规范文档参考：关系型数据水合与扩展字段映射
    """
    media_key: str = Field(..., description="媒体唯一标识符")
    type: str = Field(..., description="媒体类型：photo, video, animated_gif")
    url: str | None = Field(None, description="媒体直链 URL（图片类型）")
    preview_image_url: str | None = Field(None, description="视频/GIF 预览缩略图 URL")
    variants: list[MediaVariant] | None = Field(None, description="视频/GIF 的多变体列表")
    width: int | None = Field(None, description="媒体宽度（像素）")
    height: int | None = Field(None, description="媒体高度（像素）")
    alt_text: str | None = Field(None, description="无障碍替代文本")
    duration_ms: int | None = Field(None, description="视频时长（毫秒）")
    public_metrics: PublicMetrics | None = Field(None, description="媒体互动指标")

    @property
    def is_photo(self) -> bool:
        return self.type == "photo"

    @property
    def is_video(self) -> bool:
        return self.type == "video"

    @property
    def is_gif(self) -> bool:
        return self.type == "animated_gif"

    def get_mp4_variants(self) -> list[MediaVariant]:
        """
        过滤出所有 video/mp4 变体（排除 HLS 流媒体）。

        规范文档参考：首先过滤出 content_type 明确为 video/mp4 的变体，
        从而排除不支持在常规聊天软件中直接播放的 HLS 流媒体格式。
        """
        if not self.variants:
            return []
        return [v for v in self.variants if v.is_mp4 and v.url]

    def get_best_variant(self, max_bitrate: int | None = None) -> MediaVariant | None:
        """
        选择最优 MP4 变体（最高比特率，或不超过指定上限的最高比特率）。

        规范文档参考：选择拥有最高画质（比特率最大），但同时根据推算其文件体积
        又不会触碰当前平台转发上限的变体链接进行下载。

        Args:
            max_bitrate: 可选的比特率上限（bps），限制选择范围
        """
        mp4s = self.get_mp4_variants()
        if not mp4s:
            return None
        if max_bitrate is not None:
            filtered = [v for v in mp4s if v.bit_rate is not None and v.bit_rate <= max_bitrate]
            if filtered:
                return max(filtered, key=lambda v: v.bit_rate or 0)
        return max(mp4s, key=lambda v: v.bit_rate or 0)

    @property
    def display_url(self) -> str | None:
        """
        获取最优展示直链：图片返回 url，视频/GIF 返回最佳 MP4 变体链接，
        均无则返回预览缩略图。
        """
        if self.url:
            return self.url
        if self.is_video or self.is_gif:
            best = self.get_best_variant()
            if best:
                return best.url
        return self.preview_image_url

    def to_compact_dict(self) -> dict[str, Any]:
        """
        生成精简的媒体信息字典，用于 LLM 上下文防污染压缩。

        规范文档参考：仅提取智能体构建回复所需的硬核语义信息，
        剥离所有冗余元数据。
        """
        result: dict[str, Any] = {
            "type": self.type,
            "url": self.display_url,
        }
        if self.width and self.height:
            result["dimensions"] = f"{self.width}x{self.height}"
        if self.duration_ms:
            result["duration_sec"] = round(self.duration_ms / 1000, 1)
        if self.alt_text:
            result["alt_text"] = self.alt_text
        return result


class Attachments(XBaseModel):
    """
    推文附件字段（媒体密钥列表）

    规范文档参考：关系型数据水合与扩展字段映射
    """
    media_keys: list[str] | None = Field(None, description="媒体 ID 列表，关联 includes.media")
    poll_ids: list[str] | None = Field(None, description="投票 ID 列表")


class EntityUrl(XBaseModel):
    """URL 实体子对象"""
    start: int | None = Field(None, description="起始位置")
    end: int | None = Field(None, description="结束位置")
    url: str | None = Field(None, description="短链接 (t.co)")
    expanded_url: str | None = Field(None, description="完整展开 URL")
    display_url: str | None = Field(None, description="展示用 URL")
    title: str | None = Field(None, description="链接页面标题")
    description: str | None = Field(None, description="链接页面描述")


class EntityHashtag(XBaseModel):
    """标签实体子对象"""
    start: int | None = Field(None, description="起始位置")
    end: int | None = Field(None, description="结束位置")
    tag: str | None = Field(None, description="标签文本（不含#）")


class EntityMention(XBaseModel):
    """提及实体子对象"""
    start: int | None = Field(None, description="起始位置")
    end: int | None = Field(None, description="结束位置")
    username: str | None = Field(None, description="被提及用户名")
    id: str | None = Field(None, description="被提及用户 ID")


class EntityAnnotation(XBaseModel):
    """注解实体子对象（NLP 提取的命名实体）"""
    start: int | None = Field(None, description="起始位置")
    end: int | None = Field(None, description="结束位置")
    probability: float | None = Field(None, description="置信度")
    type: str | None = Field(None, description="实体类型（Person, Place, Product 等）")
    normalized_text: str | None = Field(None, description="归一化文本")


class Entities(XBaseModel):
    """
    推文实体字段（URL、标签、提及等）

    X API v2 的 entities 字段对推文文本中的结构化信息进行标注，
    包括 URL 短链展开、话题标签、@提及 以及 NLP 命名实体。
    """
    urls: list[EntityUrl] | None = Field(None, description="URL 实体列表")
    hashtags: list[EntityHashtag] | None = Field(None, description="标签实体列表")
    mentions: list[EntityMention] | None = Field(None, description="提及实体列表")
    annotations: list[EntityAnnotation] | None = Field(None, description="注解实体列表")

    def get_expanded_urls(self) -> list[str]:
        """提取所有展开后的 URL（过滤 t.co 短链）"""
        if not self.urls:
            return []
        return [u.expanded_url for u in self.urls if u.expanded_url]

    def get_hashtag_texts(self) -> list[str]:
        """提取所有标签文本"""
        if not self.hashtags:
            return []
        return [h.tag for h in self.hashtags if h.tag]

    def get_mentioned_usernames(self) -> list[str]:
        """提取所有被提及的用户名"""
        if not self.mentions:
            return []
        return [m.username for m in self.mentions if m.username]


class Tweet(XBaseModel):
    """
    推文对象模型

    X API v2 推文核心数据载体，包含文本、作者引用、时间、互动指标、
    附件引用及实体标注。通过 author_id 关联 includes.users，
    通过 attachments.media_keys 关联 includes.media。

    规范文档参考：
    - 最新新闻与内容搜索 - GET /2/tweets/search/recent
    - 推文链接深度解析 - GET /2/tweets/:id
    """
    id: str = Field(..., description="推文唯一 ID")
    text: str = Field(..., description="推文文本内容")
    author_id: str | None = Field(None, description="作者 ID，对应 includes.users")
    created_at: datetime | None = Field(None, description="创建时间（ISO 8601）")
    public_metrics: PublicMetrics | None = Field(None, description="互动指标")
    attachments: Attachments | None = Field(None, description="媒体附件")
    entities: Entities | None = Field(None, description="实体信息")
    conversation_id: str | None = Field(None, description="对话线程 ID")
    in_reply_to_user_id: str | None = Field(None, description="回复目标用户 ID")
    lang: str | None = Field(None, description="推文语言（BCP 47）")
    source: str | None = Field(None, description="发布客户端")
    edit_history_tweet_ids: list[str] | None = Field(
        None, description="编辑历史 ID 列表（压缩时应移除）"
    )

    @property
    def media_keys(self) -> list[str]:
        """便捷获取媒体密钥列表"""
        if self.attachments and self.attachments.media_keys:
            return self.attachments.media_keys
        return []

    @property
    def has_media(self) -> bool:
        """推文是否包含媒体附件"""
        return len(self.media_keys) > 0

    def to_compact_dict(self, enable_metrics: bool = True) -> dict[str, Any]:
        """
        生成精简的推文字典，剥离冗余元数据用于 LLM 上下文。

        规范文档参考（防污染压缩）：剥离 edit_history_tweet_ids、
        promoted_metrics 等冗余元数据，仅提取作者名称、发推时间、
        推文纯文本、点赞与转发数值以及多媒体类型和直接访问链接。
        """
        result: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "author_id": self.author_id,
        }
        if self.created_at:
            result["created_at"] = self.created_at.isoformat()
        if enable_metrics and self.public_metrics:
            result["metrics"] = self.public_metrics.to_display_str()
        if self.lang:
            result["lang"] = self.lang
        return result


class User(XBaseModel):
    """
    用户对象模型

    X API v2 用户核心数据载体。通过 includes.users 水合到推文数据中，
    提供作者名称、用户名、头像、简介等信息。

    规范文档参考：关系型数据水合与扩展字段映射
    - user.fields=name,username,profile_image_url
    """
    id: str = Field(..., description="用户唯一 ID")
    name: str = Field(..., description="用户显示名称")
    username: str = Field(..., description="用户账户名（不含@）")
    description: str | None = Field(None, description="用户简介")
    created_at: datetime | None = Field(None, description="账户创建时间")
    public_metrics: UserPublicMetrics | None = Field(None, description="用户级互动指标")
    profile_image_url: str | None = Field(None, description="头像 URL")
    verified: bool | None = Field(None, description="是否官方认证")
    protected: bool | None = Field(None, description="是否锁推")
    location: str | None = Field(None, description="用户所在地")
    url: str | None = Field(None, description="用户个人网站 URL")

    @field_validator("username", mode="before")
    @classmethod
    def strip_at_prefix(cls, v: str) -> str:
        """自动去除用户名前的 @ 符号"""
        if isinstance(v, str):
            return v.lstrip("@")
        return v

    def to_compact_dict(self) -> dict[str, Any]:
        """生成精简的用户字典用于 LLM 上下文"""
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "username": self.username,
        }
        if self.description:
            result["bio"] = self.description[:100]
        if self.public_metrics:
            result["stats"] = self.public_metrics.to_display_str()
        if self.verified:
            result["verified"] = True
        return result


class Includes(XBaseModel):
    """
    响应数据水合容器（递归查询容器）

    X API v2 将推文实体与用户实体、媒体实体解耦，
    通过 includes 字典进行关联存储。数据解析中间件必须执行
    递归的哈希表匹配以完成数据水合。

    规范文档参考：关系型数据水合与扩展字段映射
    """
    users: list[User] | None = Field(None, description="用户对象列表")
    media: list[Media] | None = Field(None, description="媒体对象列表")
    tweets: list[Tweet] | None = Field(None, description="推文对象列表（引用/转推）")
    polls: list[dict[str, Any]] | None = Field(None, description="投票对象列表")

    @functools.cached_property
    def _user_map(self) -> dict[str, User]:
        """按 user.id 索引的字典映射，将查找复杂度从 O(N) 降为 O(1)。"""
        if not self.users:
            return {}
        return {str(u.id): u for u in self.users}

    @functools.cached_property
    def _media_map(self) -> dict[str, Media]:
        """按 media.media_key 索引的字典映射，将查找复杂度从 O(N) 降为 O(1)。"""
        if not self.media:
            return {}
        return {str(m.media_key): m for m in self.media}

    def find_user_by_id(self, user_id: str | None) -> User | None:
        """
        通过用户 ID 在 includes.users 中执行哈希匹配查找。

        规范文档参考：递归的哈希表匹配 - 当解析器发现 author_id 时，
        必须在 includes.users 中查找对应的用户对象。
        """
        if not user_id:
            return None
        return self._user_map.get(str(user_id))

    def find_media_by_key(self, media_key: str) -> Media | None:
        """
        通过 media_key 在 includes.media 中执行哈希匹配查找。

        规范文档参考：递归的哈希表匹配 - 当解析器在推文主体的
        attachments 节点中发现 media_key 时，必须在 includes.media
        中查找对应的键值。
        """
        return self._media_map.get(str(media_key))

    def resolve_tweet_media(self, tweet: Tweet) -> list[Media]:
        """
        解析推文的所有关联媒体（完整数据水合）。

        遍历推文 attachments.media_keys，逐一从 includes.media
        中匹配并返回完整的 Media 对象列表。
        """
        if not tweet.media_keys:
            return []
        resolved = []
        for key in tweet.media_keys:
            media = self.find_media_by_key(key)
            if media is not None:
                resolved.append(media)
        return resolved

    def get_author_display(self, author_id: str | None) -> dict[str, str]:
        """
        获取作者展示信息。匹配失败时返回安全默认值。

        规范文档参考：提取作者名称用于 LLM 上下文压缩。
        """
        user = self.find_user_by_id(author_id)
        if user:
            return {"name": user.name, "username": user.username}
        return {"name": "Unknown", "username": "unknown"}


class PaginationMeta(XBaseModel):
    """
    分页元数据模型

    用于游标（Cursor）分页抓取。代码逻辑必须解析 meta.next_token 字段，
    通过游标机制实现稳定的分页抓取，防止内存溢出。

    规范文档参考：速率限制规避与分页调度算法
    """
    next_token: str | None = Field(None, description="下一页游标，用于分页抓取")
    previous_token: str | None = Field(None, description="上一页游标")
    result_count: int | None = Field(None, description="本页结果数量")
    oldest_id: str | None = Field(None, description="最旧推文 ID")
    newest_id: str | None = Field(None, description="最新推文 ID")

    @property
    def has_next_page(self) -> bool:
        """是否存在下一页数据"""
        return self.next_token is not None


class Trend(XBaseModel):
    """
    趋势话题模型

    规范文档参考：热点趋势获取 - GET /2/trends/by/woeid/:woeid
    https://docs.x.com/x-api/trends/trends-by-woeid/introduction

    X API v2 返回字段：trend_name, tweet_count
    """
    trend_name: str = Field(..., description="趋势话题名称或标题")
    tweet_count: int | None = Field(None, description="该趋势相关的推文数量")

    def to_display_str(self, index: int = 0) -> str:
        """生成单行展示字符串"""
        vol = f" ({self.tweet_count} 条推文)" if self.tweet_count else ""
        prefix = f"{index}. " if index > 0 else ""
        return f"{prefix}#{self.trend_name}{vol}"


# ============================================================================
# X API 响应模型（包含 headers 字段用于速率限制提取）
# ============================================================================

class _BaseResponse(XBaseModel):
    """
    所有 API 响应模型的公共基类。

    提供 HTTP 响应头存储与速率限制信息提取能力。
    headers 字段由 x_api_client.py 在反序列化时注入。
    """
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="HTTP 响应头（包含 x-rate-limit-* 速率限制字段）"
    )

    def extract_rate_limit(self) -> RateLimitInfo:
        """
        从响应头中提取结构化的速率限制信息。

        规范文档参考：每次网络请求的返回头中均包含
        x-rate-limit-remaining 与 x-rate-limit-reset 字段。
        系统需在内存中维护一个速率状态机。
        """
        return RateLimitInfo.from_headers(self.headers)


class SearchResponse(_BaseResponse):
    """
    搜索响应模型

    规范文档参考：最新新闻与内容搜索 - GET /2/tweets/search/recent
    https://docs.x.com/x-api/tweets/search/integrate/build-a-query
    """
    data: list[Tweet] | None = Field(None, description="推文列表")
    includes: Includes | None = Field(None, description="数据水合容器")
    meta: PaginationMeta | None = Field(None, description="分页元数据")

    def hydrate_tweets(self) -> list[dict[str, Any]]:
        """
        执行完整数据水合：将推文、作者、媒体合并为紧凑字典列表，
        用于 LLM 智能体上下文防污染压缩。

        规范文档参考：数据压缩与降噪层 - 剥离冗余元数据，
        仅提取智能体构建回复所需的硬核语义信息。
        """
        if not self.data:
            return []
        results = []
        for tweet in self.data:
            entry = tweet.to_compact_dict()
            if self.includes:
                author = self.includes.get_author_display(tweet.author_id)
                entry["author"] = f"@{author['username']} ({author['name']})"
                media_list = self.includes.resolve_tweet_media(tweet)
                if media_list:
                    entry["media"] = [m.to_compact_dict() for m in media_list]
            results.append(entry)
        return results


class TweetResponse(_BaseResponse):
    """
    单条推文响应模型

    规范文档参考：推文链接深度解析 - GET /2/tweets/:id
    https://docs.x.com/x-api/tweets/lookup/integrate/get-a-tweet
    """
    data: Tweet | None = Field(None, description="推文对象")
    includes: Includes | None = Field(None, description="数据水合容器")

    def hydrate_tweet(self) -> dict[str, Any] | None:
        """执行单条推文的完整数据水合"""
        if not self.data:
            return None
        entry = self.data.to_compact_dict()
        if self.includes:
            author = self.includes.get_author_display(self.data.author_id)
            entry["author"] = f"@{author['username']} ({author['name']})"
            media_list = self.includes.resolve_tweet_media(self.data)
            if media_list:
                entry["media"] = [m.to_compact_dict() for m in media_list]
        return entry


class UserTimelineResponse(_BaseResponse):
    """
    用户时间线响应模型

    规范文档参考：个人主页与时间线 - GET /2/users/:id/tweets
    https://docs.x.com/x-api/tweets/timelines/integrate/user-timeline-by-id
    """
    data: list[Tweet] | None = Field(None, description="推文列表")
    includes: Includes | None = Field(None, description="数据水合容器")
    meta: PaginationMeta | None = Field(None, description="分页元数据")

    def hydrate_tweets(self) -> list[dict[str, Any]]:
        """执行时间线推文的完整数据水合"""
        if not self.data:
            return []
        results = []
        for tweet in self.data:
            entry = tweet.to_compact_dict()
            if self.includes:
                media_list = self.includes.resolve_tweet_media(tweet)
                if media_list:
                    entry["media"] = [m.to_compact_dict() for m in media_list]
            results.append(entry)
        return results


class TrendsResponse(_BaseResponse):
    """
    趋势响应模型

    规范文档参考：热点趋势获取 - GET /2/trends/by/woeid/:woeid
    https://docs.x.com/x-api/trends/lookup/integrate/get-trends
    """
    data: list[Trend] | None = Field(None, description="趋势数据对象列表")


class UserLookupResponse(_BaseResponse):
    """
    用户查询响应模型

    规范文档参考：用户名查询
    https://docs.x.com/x-api/users/lookup/integrate/get-a-user-by-username
    """
    data: User | None = Field(None, description="用户对象")
