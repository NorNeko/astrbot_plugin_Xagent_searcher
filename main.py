import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Image, Plain, Video

from .core.x_api_client import XApiClient
from .core.media_processor import MediaProcessor
from .core.security_acl import SecurityACL
from .models.x_response_models import (
    TweetResponse,
    UserTimelineResponse,
    SearchResponse,
    TrendsResponse
)

# ============================================================================
# WOEID 预设地区映射（Yahoo! Where On Earth ID）
# 仅支持以下预设地区的热点趋势查询，其他地区不提供此功能。
# ============================================================================
SUPPORTED_REGIONS: dict[str, int] = {
    "全球": 1,
    "日本": 23424856,
    "美国": 23424977,
    "韩国": 23424868,
}

# 地区别名映射：将用户可能输入的各种写法统一映射到标准地区名
REGION_ALIASES: dict[str, str] = {
    # 全球
    "全球": "全球", "global": "全球", "world": "全球", "worldwide": "全球",
    # 日本
    "日本": "日本", "jp": "日本", "japan": "日本",
    # 美国
    "美国": "美国", "us": "美国", "usa": "美国", "america": "美国",
    # 韩国
    "韩国": "韩国", "kr": "韩国", "korea": "韩国",
}

# 推文 URL 正则匹配模式（用于自动检测聊天中的推文链接）
TWEET_URL_PATTERN = re.compile(
    r'https?://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d+)'
)


# ============================================================================
# 分页缓存数据结构
# ============================================================================

@dataclass
class PagedCache:
    """分页缓存会话，存储当前活动列表的状态。
    
    每次新搜索/趋势/时间线拉取时创建新实例替换旧实例，
    翻页时在现有实例上追加数据。
    """
    items: list = field(default_factory=list)            # 已显示的全部条目（累积）
    buffer: list = field(default_factory=list)           # 从 API 获取但尚未显示的条目
    next_token: str | None = None                       # X API 游标（搜索/时间线）
    query_type: str = ""                                # "search" | "trend" | "timeline"
    query_param: str = ""                               # 关键词 / woeid / user_id
    page_offset: int = 0                                # 已显示条目总数（用于连续编号）


@register("astrbot_plugin_Xagent_searcher", "B2347", "高度可配置的推特数据智能体工具集，支持异步并发、媒体处理与黑白名单管控", "0.0.1")
class XAgentToolkitPlugin(Star):
    """
    X (Twitter) API v2 集成插件
    
    核心功能：
    1. 推文搜索工具（keyword search）
    2. 热点趋势拉取（trends fetching）
    3. 推文链接深度解析（tweet parsing）
    4. 用户时间线获取（user timeline）
    """
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.logger = logger
        
        # 初始化核心业务逻辑层
        try:
            # API 客户端注入配置（支持 OAuth 1.0a 三级降级策略）
            self.api_client = XApiClient(
                bearer_token=self.config.get("api_bearer_token", ""),
                api_key=self.config.get("api_key", ""),
                api_key_secret=self.config.get("api_key_secret", ""),
                oauth_access_token=self.config.get("oauth_access_token", ""),
                oauth_access_token_secret=self.config.get("oauth_access_token_secret", ""),
                cookie_auth_token=self.config.get("cookie_auth_token", ""),
                cookie_ct0=self.config.get("cookie_ct0", ""),
                graphql_tweet_query_id=self.config.get("graphql_tweet_query_id", ""),
                enable_proxy=self.config.get("enable_proxy", True),
                proxy_url=self.config.get("proxy_url", "http://127.0.0.1:7890")
            )
            
            # 媒体处理管线初始化
            self.media_processor = MediaProcessor(
                forward_threshold_mb=self.config.get("forward_threshold_mb", 25),
                pil_compress_target_kb=self.config.get("pil_compress_target_kb", 2048),
                display_media_details=self.config.get("display_media_details", True),
                enable_proxy=self.config.get("enable_proxy", True),
                proxy_url=self.config.get("proxy_url", "http://127.0.0.1:7890")
            )
            
            # 安全访问控制层初始化
            self.security_acl = SecurityACL(
                acl_mode=self.config.get("acl_mode", "Off"),  # Off / Whitelist / Blacklist
                allowed_list=self.config.get("allowed_list", []),
                banned_list=self.config.get("banned_list", [])
            )
            
            # 速率限制状态机（防护 429 Too Many Requests）
            self.rate_limit_state = {
                "remaining": float('inf'),
                "reset_at": None,
                "circuit_breaker_active": False
            }
            
            # 搜索结果分页缓存（按会话/用户隔离，供 /xparse 二次解析与 /xnext 翻页使用）
            # key: cache_key, value: PagedCache
            self._paged_cache: dict[str, PagedCache] = {}
            
            # 媒体缓存目录（遵循 AstrBot 存储规范：StarTools.get_data_dir）
            self._cache_dir: Path = StarTools.get_data_dir("astrbot_plugin_Xagent_searcher")
            self._cache_images: Path = self._cache_dir / "images"
            self._cache_thumbnails: Path = self._cache_dir / "thumbnails"
            self._cache_videos: Path = self._cache_dir / "videos"
            for d in (self._cache_images, self._cache_thumbnails, self._cache_videos):
                d.mkdir(parents=True, exist_ok=True)
            
            # 缓存自动清理周期（天），0 = 不清理
            self._cache_expire_days: int = self.config.get("cache_expire_days", 3)
            
            self.logger.info("XAgentToolkitPlugin 初始化完成")
        except Exception as e:
            self.logger.error(f"插件初始化失败: {str(e)}", exc_info=True)
            raise
    
    async def initialize(self):
        """插件加载/重载时异步调用，适合做网络探测、资源预加载等操作。"""
        # 启动时立即执行一次过期缓存清理
        self._cleanup_expired_cache()
        self.logger.info("XAgentToolkitPlugin 异步初始化完成")
    
    # ============================================================================
    # 缓存 Key 计算（群组隔离支持）
    # ============================================================================
    
    def _get_cache_key(self, event: AstrMessageEvent) -> str:
        """
        计算分页缓存 key。
        - 默认: UMO（会话级别共享，群组成员共用最新列表）
        - 群组隔离模式: UMO + sender_id（群成员独立）
        - 私聊: 始终为 UMO
        """
        umo = event.unified_msg_origin
        if self.config.get("enable_group_isolation", False):
            if "GroupMessage" in umo:
                sender_id = event.get_sender_id()
                return f"{umo}:{sender_id}"
        return umo
    
    # ============================================================================
    # 指令 1: 推文搜索（/xsearch <关键词>）
    # ============================================================================
    
    @filter.command("xsearch", alias={"推特搜索"})
    async def cmd_search_tweets(self, event: AstrMessageEvent, keyword: str = ""):
        """按关键词搜索推特上的最新推文。用法: /xsearch <关键词>"""
        try:
            # 速率限制检验：检查断路器状态（包含自动恢复逻辑）
            if self._is_rate_limited():
                yield event.plain_result(
                    f"❌ API 限流保护：推特 API 速率限制已触发。请于稍后重试。\n"
                    f"重置时间: {self.rate_limit_state['reset_at']}"
                )
                return
            
            # 前置安全校验：ACL 中间件评估（使用 AstrBot 标准 UMO）
            is_allowed = await self.security_acl.check_access(
                unified_msg_origin=event.unified_msg_origin
            )
            
            if not is_allowed:
                yield event.plain_result(
                    "❌ 权限被拒：您所在的群组或账户未被授权访问推特搜索功能。请联系管理员。"
                )
                return
            
            # 参数有效性校验
            if not keyword or not keyword.strip():
                yield event.plain_result("❌ 搜索失败：关键词不能为空。")
                return
            
            keyword = keyword.strip()
            
            # 获取配置项
            max_return_count = self.config.get("max_return_count", 10)
            enable_public_metrics = self.config.get("enable_public_metrics", True)
            fetch_count = self.config.get("fetch_count", 3)
            
            self.logger.info(f"<search_tweets> 用户 {event.get_sender_id()} 搜索: {keyword}")

            # 异步网络请求：委托给共用过滤搜索辅助方法（含客户端 min_faves 多轮过滤）
            sort_order = self.config.get("search_sort_order", "relevancy")
            min_faves = int(self.config.get("search_min_faves", 0))
            max_rounds = int(self.config.get("search_filter_max_rounds", 0))

            all_items, next_token = await self._search_with_filter(
                query=keyword,
                start_token=None,
                max_return_count=max_return_count,
                sort_order=sort_order,
                min_faves=min_faves,
                max_rounds=max_rounds,
            )

            # 分割：显示批次 + 缓冲区
            display_items = all_items[:fetch_count]
            buffer_items = all_items[fetch_count:]
            
            # 构建消息：逐条推文 [(缩略图), 文本摘要]
            # 为防止 NapCat/NTQQ 多图消息超时（retcode=1200），
            # 每条推文单独发送（最多 1 图 + 文本），最终尾部提示 yield 返回。
            tweet_messages: list[list] = []  # 每个元素是一条推文的消息组件列表
            
            for i, item in enumerate(display_items, 1):
                msg_parts, _ = await self._build_tweet_message_parts(
                    i, item["_tweet"], item["_response"], enable_public_metrics
                )
                tweet_messages.append(msg_parts)
            
            # 存储分页缓存（覆盖旧列表 — 即时清理语义）
            cache_key = self._get_cache_key(event)
            self._paged_cache[cache_key] = PagedCache(
                items=list(display_items),
                buffer=buffer_items,
                next_token=next_token,
                query_type="search",
                query_param=keyword,
                page_offset=len(display_items)
            )

            # 逐条发送推文（方案B：图文优先，超时降级纯文本重试）
            if tweet_messages:
                for idx_offset, msg_parts in enumerate(tweet_messages, 1):
                    await self._send_with_degradation(event, msg_parts, idx_offset)
                
                has_more = bool(buffer_items) or bool(next_token)
                more_hint = " 使用 /xnext 查看更多。" if has_more else ""
                yield event.plain_result(
                    f"💡 第 1-{len(display_items)} 条。"
                    f"使用 /xparse <序号> 解析指定推文。{more_hint}"
                )
            else:
                yield event.plain_result("🔍 搜索结果：未找到相关推文。")
            
        except PermissionError as e:
            yield event.plain_result(f"❌ 权限控制：{str(e)}")
        except ValueError as e:
            yield event.plain_result(f"❌ 参数错误：{str(e)}")
        except Exception as e:
            self.logger.error(f"<search_tweets> 异常: {str(e)}", exc_info=True)
            yield event.plain_result(
                f"❌ 搜索失败：遇到系统异常。请检查网络代理或 API 配额状态。\n"
                f"错误类型: {type(e).__name__}"
            )

    # ============================================================================
    # 指令 2: 热点趋势（/xtrend [地区名]）
    # ============================================================================
    
    @filter.command("xtrend", alias={"推特趋势"})
    async def cmd_fetch_trends(self, event: AstrMessageEvent, region: str = "全球"):
        """获取指定地区的推特热点趋势。用法: /xtrend [地区名] 可选: 全球、日本(JP)、美国(US)、韩国(KR)"""
        try:
            # 别名解析：将用户输入统一映射为标准地区名
            normalized = REGION_ALIASES.get(region.strip().lower())
            if normalized is None:
                supported = "、".join(f"{k}({'/'.join(a for a, v in REGION_ALIASES.items() if v == k and a != k)})" for k in SUPPORTED_REGIONS)
                yield event.plain_result(
                    f"❌ 不支持的地区「{region}」。本插件仅提供以下地区的热点趋势查询：\n{supported}"
                )
                return
            region = normalized
            
            # 预设地区校验
            woeid = SUPPORTED_REGIONS[region]
            
            # 速率限制检验（包含自动恢复逻辑）
            if self._is_rate_limited():
                yield event.plain_result(
                    f"❌ API 限流保护：推特 API 速率限制已触发。请于稍后重试。"
                )
                return
            
            # ACL 安全检验（使用 AstrBot 标准 UMO）
            is_allowed = await self.security_acl.check_access(
                unified_msg_origin=event.unified_msg_origin
            )
            
            if not is_allowed:
                yield event.plain_result("❌ 权限被拒：您无权访问趋势信息。")
                return
            
            self.logger.info(f"<fetch_trends> 用户 {event.get_sender_id()} 请求地区「{region}」(WOEID={woeid}) 的趋势")
            
            # 异步调用 X API 趋势端点
            response: TrendsResponse = await self.api_client.get_trends(woeid=woeid)
            
            # 更新速率限制状态
            self._update_rate_limit_state(response)
            
            # 构建趋势缓存条目
            fetch_count = self.config.get("fetch_count", 3)
            all_items: list[dict[str, str]] = []
            if response.data:
                for trend in response.data:
                    all_items.append({
                        "trend_name": trend.trend_name,
                        "tweet_count": str(trend.tweet_count) if trend.tweet_count else "",
                        "text": f"#{trend.trend_name} ({trend.tweet_count or 'N/A'} 条推文)",
                        "type": "trend"
                    })
            
            display_items = all_items[:fetch_count]
            buffer_items = all_items[fetch_count:]
            
            # 存入分页缓存（覆盖旧列表 — 即时清理语义）
            cache_key = self._get_cache_key(event)
            self._paged_cache[cache_key] = PagedCache(
                items=list(display_items),
                buffer=buffer_items,
                next_token=None,  # 趋势不支持 API 分页
                query_type="trend",
                query_param=str(woeid),
                page_offset=len(display_items)
            )
            
            # 格式化显示
            if display_items:
                output_lines = [f"📊 {region}当前趋势：\n"]
                for i, item in enumerate(display_items, 1):
                    tweet_count_str = f"   💬 {item.get('tweet_count', 'N/A')} 条推文" if item.get('tweet_count') else ""
                    output_lines.append(f"{i}. #{item['trend_name']}\n{tweet_count_str}\n")
                has_more = bool(buffer_items)
                more_hint = " 使用 /xnext 查看更多趋势。" if has_more else ""
                output_lines.append(
                    f"\n💡 第 1-{len(display_items)} 条。"
                    f"使用 /xparse <序号> 搜索指定趋势。{more_hint}"
                )
                yield event.plain_result("".join(output_lines))
            else:
                yield event.plain_result(f"📊 {region}趋势：暂无数据。")
            
        except PermissionError as e:
            yield event.plain_result(f"❌ 权限控制：{str(e)}")
        except ConnectionError as e:
            yield event.plain_result(
                "❌ 网络错误：无法连接推特 API。请检查代理配置（默认: http://127.0.0.1:7890）。"
            )
        except Exception as e:
            self.logger.error(f"<fetch_trends> 异常: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 趋势获取失败：{type(e).__name__}")

    # ============================================================================
    # 指令 4: 按序号解析搜索结果中的推文（/xparse <序号>）
    # ============================================================================
    
    @filter.command("xparse", alias={"推特解析"})
    async def cmd_parse_from_list(self, event: AstrMessageEvent, index: str = ""):
        """解析缓存列表中指定序号的条目。推文条目解析详情，趋势条目自动搜索。用法: /xparse <序号>"""
        cache_key = self._get_cache_key(event)
        
        # 参数校验
        if not index or not index.strip().isdigit():
            yield event.plain_result("❌ 请提供有效的序号（数字）。用法: /xparse 1")
            return
        
        idx = int(index.strip())
        
        # 缓存校验
        cache = self._paged_cache.get(cache_key)
        if not cache or not cache.items:
            yield event.plain_result("❌ 暂无缓存列表。请先使用 /xsearch、/xtrend、/xtl 或 /xhome 获取列表。")
            return
        
        if idx < 1 or idx > len(cache.items):
            yield event.plain_result(
                f"❌ 序号超出范围。当前列表共 {len(cache.items)} 条，请输入 1-{len(cache.items)} 之间的数字。"
            )
            return
        
        selected_item = cache.items[idx - 1]
        
        # ===== 趋势条目：自动触发搜索，替换当前缓存 =====
        if selected_item.get("type") == "trend":
            trend_keyword = selected_item["trend_name"]
            self.logger.info(f"<xparse> 趋势条目转搜索: {trend_keyword}")
            
            try:
                if self._is_rate_limited():
                    yield event.plain_result("❌ API 限流保护：请稍后重试。")
                    return
                
                is_allowed = await self.security_acl.check_access(
                    unified_msg_origin=event.unified_msg_origin
                )
                if not is_allowed:
                    yield event.plain_result("❌ 权限被拒：您无权搜索推文。")
                    return
                
                max_return_count = self.config.get("max_return_count", 10)
                fetch_count = self.config.get("fetch_count", 3)
                enable_public_metrics = self.config.get("enable_public_metrics", True)
                
                sort_order = self.config.get("search_sort_order", "relevancy")
                min_faves = int(self.config.get("search_min_faves", 0))
                max_rounds = int(self.config.get("search_filter_max_rounds", 0))

                # 委托给共用过滤搜索辅助方法（与 /xsearch 行为完全一致）
                all_items, next_token = await self._search_with_filter(
                    query=trend_keyword,
                    start_token=None,
                    max_return_count=max_return_count,
                    sort_order=sort_order,
                    min_faves=min_faves,
                    max_rounds=max_rounds,
                )

                display_items = all_items[:fetch_count]
                buffer_items = all_items[fetch_count:]
                
                # 替换缓存为搜索结果（即时清理语义）
                self._paged_cache[cache_key] = PagedCache(
                    items=list(display_items),
                    buffer=buffer_items,
                    next_token=next_token,
                    query_type="search",
                    query_param=trend_keyword,
                    page_offset=len(display_items)
                )
                
                # 逐条图文发送（与 /xsearch、/xtl 格式对齐）
                tweet_messages: list[list] = []
                for i, item in enumerate(display_items, 1):
                    msg_parts, _ = await self._build_tweet_message_parts(
                        i, item["_tweet"], item["_response"], enable_public_metrics
                    )
                    tweet_messages.append(msg_parts)
                
                if tweet_messages:
                    await event.send(event.chain_result([Plain(f"🔍 趋势「{trend_keyword}」搜索结果：\n")]))
                    for idx_offset, msg_parts in enumerate(tweet_messages, 1):
                        await self._send_with_degradation(event, msg_parts, idx_offset)
                    
                    has_more = bool(buffer_items) or bool(next_token)
                    more_hint = " 使用 /xnext 查看更多。" if has_more else ""
                    yield event.plain_result(
                        f"💡 第 1-{len(display_items)} 条。"
                        f"使用 /xparse <序号> 解析指定推文。{more_hint}"
                    )
                else:
                    yield event.plain_result(f"🔍 趋势关键词「{trend_keyword}」未找到相关推文。")
            except Exception as e:
                self.logger.error(f"<xparse> 趋势搜索异常: {str(e)}", exc_info=True)
                yield event.plain_result(f"❌ 趋势搜索失败：{type(e).__name__}")
            return
        
        # ===== 推文条目：解析推文详情 =====
        tweet_url = selected_item.get("url", "")
        tweet_id = self._extract_tweet_id_from_url(tweet_url)
        
        if not tweet_id:
            yield event.plain_result("❌ 缓存中的推文 URL 无效。请重新搜索。")
            return
        
        self.logger.info(f"<xparse> 用户 {event.get_sender_id()} 解析缓存序号 {idx} → {tweet_url}")
        
        # 复用推文解析逻辑（与 on_message 相同）
        try:
            if self._is_rate_limited():
                yield event.plain_result("❌ API 限流保护：推特 API 速率限制已触发。请于稍后重试。")
                return
            
            is_allowed = await self.security_acl.check_access(
                unified_msg_origin=event.unified_msg_origin
            )
            if not is_allowed:
                yield event.plain_result("❌ 权限被拒：您无权解析推文。")
                return
            
            enable_public_metrics = self.config.get("enable_public_metrics", True)
            
            response: TweetResponse = await self.api_client.get_tweet(
                tweet_id=tweet_id,
                expansions="author_id,attachments.media_keys",
                tweet_fields="created_at,public_metrics,entities",
                media_fields="url,variants,type,preview_image_url",
                user_fields="name,username,profile_image_url"
            )
            
            self._update_rate_limit_state(response)
            processed_response = await self._process_tweet_media(response)
            
            compressed_output = self._compress_tweet_response(
                processed_response,
                enable_public_metrics=enable_public_metrics
            )
            
            image_components = await self._send_tweet_media(event, processed_response)
            
            # 逐张发送图片，避免多图消息链触发 NapCat 超时
            if image_components:
                for img in image_components[:-1]:
                    try:
                        await event.send(event.chain_result([img]))
                    except Exception as e:
                        self.logger.warning(f"<xparse> 图片逐条发送失败: {e}")
                yield event.chain_result([image_components[-1], Plain(compressed_output)])
            else:
                yield event.plain_result(compressed_output)
        
        except FileNotFoundError:
            yield event.plain_result("❌ 推文不存在：该推文已被删除或不可访问。")
        except Exception as e:
            self.logger.error(f"<xparse> 异常: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 解析失败：{type(e).__name__}")

    # ============================================================================
    # 自动检测: 推文链接解析（聊天中出现推文 URL 时自动触发）
    # ============================================================================
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，自动检测并解析推文 URL。无需指令前缀，直接发送推文链接即可触发。"""
        text = event.message_str
        if not text:
            return
        
        # 跳过指令消息（以 / 开头），由对应的 @filter.command 处理器处理
        if text.startswith("/"):
            return
        
        # 正则匹配推文 URL（不匹配则静默放行，不阻止事件传播）
        match = TWEET_URL_PATTERN.search(text)
        if not match:
            return
        
        tweet_id = match.group(1)
        
        try:
            # 速率限制检验（包含自动恢复逻辑）
            if self._is_rate_limited():
                yield event.plain_result(
                    f"❌ API 限流保护：推特 API 速率限制已触发。请于稍后重试。"
                )
                return
            
            # ACL 安全检验（使用 AstrBot 标准 UMO）
            is_allowed = await self.security_acl.check_access(
                unified_msg_origin=event.unified_msg_origin
            )
            
            if not is_allowed:
                yield event.plain_result("❌ 权限被拒：您无权解析推文。")
                return
            
            self.logger.info(f"<on_message> 用户 {event.get_sender_id()} 解析推文 {tweet_id}")
            
            # 获取配置
            enable_public_metrics = self.config.get("enable_public_metrics", True)
            
            # 异步调用 API 获取推文详情
            response: TweetResponse = await self.api_client.get_tweet(
                tweet_id=tweet_id,
                expansions="author_id,attachments.media_keys",
                tweet_fields="created_at,public_metrics,entities",
                media_fields="url,variants,type,preview_image_url",
                user_fields="name,username,profile_image_url"
            )
            
            # 更新速率限制状态
            self._update_rate_limit_state(response)
            
            # 媒体处理与优化（变体筛选）
            processed_response = await self._process_tweet_media(response)
            
            # 数据压缩输出（文本摘要，不含原始媒体 URL）
            compressed_output = self._compress_tweet_response(
                processed_response,
                enable_public_metrics=enable_public_metrics
            )
            
            # 下载图片组件（视频在内部单独发送）
            image_components = await self._send_tweet_media(event, processed_response)
            
            # 逐张发送图片，避免多图消息链触发 NapCat 超时
            if image_components:
                for img in image_components[:-1]:
                    try:
                        await event.send(event.chain_result([img]))
                    except Exception as e:
                        self.logger.warning(f"<on_message> 图片逐条发送失败: {e}")
                yield event.chain_result([image_components[-1], Plain(compressed_output)])
            else:
                yield event.plain_result(compressed_output)
            
        except PermissionError as e:
            yield event.plain_result(f"❌ 权限控制：{str(e)}")
        except ValueError as e:
            yield event.plain_result(f"❌ URL 解析失败：{str(e)}")
        except FileNotFoundError:
            yield event.plain_result("❌ 推文不存在：该推文已被删除或不可访问。")
        except Exception as e:
            self.logger.error(f"<on_message> 异常: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 解析失败：{type(e).__name__}")

    # ============================================================================
    # 指令 3: 用户时间线（/xtl <用户名>）
    # ============================================================================
    
    @filter.command("xtl", alias={"推特时间线"})
    async def cmd_fetch_user_timeline(self, event: AstrMessageEvent, username: str = ""):
        """获取指定推特用户的最新推文时间线。用法: /xtl <用户名或数字ID>"""
        try:
            # 速率限制检验（包含自动恢复逻辑）
            if self._is_rate_limited():
                yield event.plain_result(
                    f"❌ API 限流保护：推特 API 速率限制已触发。请于稍后重试。"
                )
                return
            
            # ACL 安全检验（使用 AstrBot 标准 UMO）
            is_allowed = await self.security_acl.check_access(
                unified_msg_origin=event.unified_msg_origin
            )
            
            if not is_allowed:
                yield event.plain_result("❌ 权限被拒：您无权获取用户信息。")
                return
            
            # 参数有效性校验
            if not username or not username.strip():
                yield event.plain_result(
                    "❌ 用户名不能为空。请提供有效的推特用户名或数字用户ID。"
                )
                return
            
            username = username.strip().lstrip("@")  # 移除 @ 前缀
            
            self.logger.info(
                f"<fetch_user_timeline> 用户 {event.get_sender_id()} 查询用户 {username} 的时间线"
            )
            
            # 获取配置
            max_return_count = self.config.get("max_return_count", 10)
            enable_public_metrics = self.config.get("enable_public_metrics", True)
            
            # 自动判断：纯数字视为用户 ID，否则视为用户名并查询 ID
            if username.isdigit():
                user_id = username
            else:
                user_id = await self.api_client.get_user_id_by_username(username)
            
            response: UserTimelineResponse = await self.api_client.get_user_tweets(
                user_id=user_id,
                max_results=max_return_count,
                expansions="author_id,attachments.media_keys",
                tweet_fields="created_at,public_metrics",
                media_fields="url,variants,type,preview_image_url",
                user_fields="name,username,created_at,description,public_metrics"
            )
            
            # 更新速率限制状态
            self._update_rate_limit_state(response)
            
            # 构建时间线缓存条目
            fetch_count = self.config.get("fetch_count", 3)
            author_username = "unknown"
            if response.includes and response.includes.users:
                author_username = response.includes.users[0].username
            
            # _tweet/_response 供翻页时生成缩略图，不参与序列化
            all_items: list[dict] = []
            if response.data:
                for tweet in response.data:
                    tweet_url = f"https://x.com/{author_username}/status/{tweet.id}"
                    all_items.append({
                        "url": tweet_url, "text": tweet.text[:80], "type": "tweet",
                        "_tweet": tweet, "_response": response,
                    })
            
            display_items = all_items[:fetch_count]
            buffer_items = all_items[fetch_count:]
            
            # 存入分页缓存（覆盖旧列表 — 即时清理语义）
            cache_key = self._get_cache_key(event)
            next_token = response.meta.next_token if response.meta else None
            self._paged_cache[cache_key] = PagedCache(
                items=list(display_items),
                buffer=buffer_items,
                next_token=next_token,
                query_type="timeline",
                query_param=user_id,
                page_offset=len(display_items)
            )
            
            # 构建消息：逐条推文 [(缩略图), 文本摘要]
            # 为防止 NapCat/NTQQ 多图消息超时（retcode=1200），
            # 每条推文单独发送（最多 1 图 + 文本），最终尾部提示 yield 返回。
            tweet_messages: list[list] = []  # 每个元素是一条推文的消息组件列表
            
            if response.data:
                for i, tweet in enumerate(response.data[:fetch_count], 1):
                    msg_parts, _ = await self._build_tweet_message_parts(
                        i, tweet, response, enable_public_metrics
                    )
                    tweet_messages.append(msg_parts)
            
            # 逐条发送推文（方案B：图文优先，超时降级纯文本重试）
            if tweet_messages:
                for idx_offset, msg_parts in enumerate(tweet_messages, 1):
                    await self._send_with_degradation(event, msg_parts, idx_offset)
                
                has_more = bool(buffer_items) or bool(next_token)
                more_hint = " 使用 /xnext 查看更多。" if has_more else ""
                yield event.plain_result(
                    f"💡 第 1-{len(display_items)} 条。"
                    f"使用 /xparse <序号> 解析指定推文。{more_hint}"
                )
            else:
                yield event.plain_result("👤 该用户暂无推文。")
            
        except PermissionError as e:
            yield event.plain_result(f"❌ 权限控制：{str(e)}")
        except ValueError as e:
            yield event.plain_result(f"❌ 参数错误：{str(e)}")
        except FileNotFoundError:
            yield event.plain_result("❌ 用户不存在：请检查用户名是否正确。")
        except Exception as e:
            self.logger.error(f"<fetch_user_timeline> 异常: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 获取失败：{type(e).__name__}")

    # ============================================================================
    # 指令 4: 主页时间线（/xhome）
    # ============================================================================
    
    @filter.command("xhome", alias={"主页时间线"})
    async def cmd_fetch_home_timeline(self, event: AstrMessageEvent):
        """获取当前认证用户的主页时间线（Following）。用法: /xhome"""
        try:
            # 速率限制检验（包含自动恢复逻辑）
            if self._is_rate_limited():
                yield event.plain_result(
                    f"❌ API 限流保护：推特 API 速率限制已触发。请于稍后重试。"
                )
                return
            
            # ACL 安全检验（使用 AstrBot 标准 UMO）
            is_allowed = await self.security_acl.check_access(
                unified_msg_origin=event.unified_msg_origin
            )
            
            if not is_allowed:
                yield event.plain_result("❌ 权限被拒：您无权获取主页时间线。")
                return
            
            self.logger.info(
                f"<fetch_home_timeline> 用户 {event.get_sender_id()} 请求主页时间线"
            )
            
            # 获取配置
            max_return_count = self.config.get("max_return_count", 10)
            enable_public_metrics = self.config.get("enable_public_metrics", True)
            fetch_count = self.config.get("fetch_count", 3)
            
            # 获取认证用户 ID
            auth_user_id = await self.api_client.get_authenticated_user_id()
            
            # 拉取主页时间线
            response: UserTimelineResponse = await self.api_client.get_home_timeline(
                user_id=auth_user_id,
                max_results=max_return_count,
                expansions="author_id,attachments.media_keys",
                tweet_fields="created_at,public_metrics",
                media_fields="url,variants,type,preview_image_url",
                user_fields="name,username,created_at,description,public_metrics"
            )
            
            # 更新速率限制状态
            self._update_rate_limit_state(response)
            
            # 构建时间线缓存条目（主页时间线包含多位作者）
            all_items: list[dict] = []
            if response.data:
                for tweet in response.data:
                    author = self._get_author_from_includes(response, tweet.author_id)
                    tweet_url = f"https://x.com/{author['username']}/status/{tweet.id}"
                    all_items.append({
                        "url": tweet_url, "text": tweet.text[:80], "type": "tweet",
                        "_tweet": tweet, "_response": response,
                    })
            
            display_items = all_items[:fetch_count]
            buffer_items = all_items[fetch_count:]
            
            # 存入分页缓存（覆盖旧列表 — 即时清理语义）
            cache_key = self._get_cache_key(event)
            next_token = response.meta.next_token if response.meta else None
            self._paged_cache[cache_key] = PagedCache(
                items=list(display_items),
                buffer=buffer_items,
                next_token=next_token,
                query_type="home",
                query_param=auth_user_id,
                page_offset=len(display_items)
            )
            
            # 构建消息：逐条推文 [(缩略图), 文本摘要]
            tweet_messages: list[list] = []
            
            if response.data:
                for i, tweet in enumerate(response.data[:fetch_count], 1):
                    msg_parts, _ = await self._build_tweet_message_parts(
                        i, tweet, response, enable_public_metrics
                    )
                    tweet_messages.append(msg_parts)
            
            # 逐条发送推文（方案B：图文优先，超时降级纯文本重试）
            if tweet_messages:
                for idx_offset, msg_parts in enumerate(tweet_messages, 1):
                    await self._send_with_degradation(event, msg_parts, idx_offset)
                
                has_more = bool(buffer_items) or bool(next_token)
                more_hint = " 使用 /xnext 查看更多。" if has_more else ""
                yield event.plain_result(
                    f"🏠 主页时间线 第 1-{len(display_items)} 条。"
                    f"使用 /xparse <序号> 解析指定推文。{more_hint}"
                )
            else:
                yield event.plain_result("🏠 主页时间线暂无推文。")
            
        except PermissionError as e:
            yield event.plain_result(f"❌ 权限控制：{str(e)}")
        except ValueError as e:
            yield event.plain_result(f"❌ 参数错误：{str(e)}")
        except Exception as e:
            self.logger.error(f"<fetch_home_timeline> 异常: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 主页时间线获取失败：{type(e).__name__}")

    # ============================================================================
    # 指令 5: 翻页（/xnext）
    # ============================================================================
    
    @filter.command("xnext", alias={"下一页"})
    async def cmd_next_page(self, event: AstrMessageEvent):
        """加载下一页推文/趋势。用法: /xnext"""
        try:
            # ACL 安全检验
            is_allowed = await self.security_acl.check_access(
                unified_msg_origin=event.unified_msg_origin
            )
            if not is_allowed:
                yield event.plain_result("❌ 权限被拒：您无权使用翻页功能。")
                return
            
            cache_key = self._get_cache_key(event)
            cache = self._paged_cache.get(cache_key)
            
            if not cache or not cache.items:
                yield event.plain_result("❌ 暂无缓存列表。请先使用 /xsearch、/xtrend、/xtl 或 /xhome 获取列表。")
                return
            
            fetch_count = self.config.get("fetch_count", 3)
            max_cache_items = self.config.get("max_cache_items", 50)
            
            # 检查最大缓存条目限制
            if len(cache.items) >= max_cache_items:
                yield event.plain_result(
                    f"❌ 已达最大缓存条目限制（{max_cache_items} 条）。请发起新的搜索或趋势查询。"
                )
                return
            
            # 计算本次最多可显示的条数（受 max_cache_items 限制）
            remaining_capacity = max_cache_items - len(cache.items)
            effective_fetch = min(fetch_count, remaining_capacity)
            
            items_to_show: list[dict[str, str]] = []
            
            # 优先从 buffer 取数据
            while len(items_to_show) < effective_fetch and cache.buffer:
                items_to_show.append(cache.buffer.pop(0))
            
            # 如果 buffer 不足且有 next_token，从 API 获取更多数据
            if len(items_to_show) < effective_fetch and cache.next_token and cache.query_type in ("search", "timeline", "home"):
                if self._is_rate_limited():
                    if not items_to_show:
                        yield event.plain_result("❌ API 限流保护：请稍后重试。")
                        return
                else:
                    try:
                        new_items, new_next_token = await self._fetch_more_items(cache)
                        cache.buffer.extend(new_items)
                        cache.next_token = new_next_token
                        # 从新填充的 buffer 中继续取数据
                        while len(items_to_show) < effective_fetch and cache.buffer:
                            items_to_show.append(cache.buffer.pop(0))
                    except Exception as e:
                        self.logger.warning(f"<xnext> API 翻页请求失败: {e}")
                        if not items_to_show:
                            yield event.plain_result(f"❌ 翻页失败：{type(e).__name__}")
                            return
            
            if not items_to_show:
                yield event.plain_result("📄 已到最后一页，没有更多内容了。")
                return
            
            # 更新缓存状态
            start_index = cache.page_offset + 1
            cache.items.extend(items_to_show)
            cache.page_offset += len(items_to_show)
            
            # 格式化输出
            if cache.query_type == "trend":
                output_lines = [f"📊 趋势（续）：\n"]
                for i, item in enumerate(items_to_show, start_index):
                    tweet_count_str = f"   💬 {item.get('tweet_count', 'N/A')} 条推文" if item.get('tweet_count') else ""
                    output_lines.append(f"{i}. #{item['trend_name']}\n{tweet_count_str}\n")
                has_more = bool(cache.buffer)
                more_hint = " 使用 /xnext 继续翻页。" if has_more else ""
                output_lines.append(
                    f"\n💡 第 {start_index}-{cache.page_offset} 条。"
                    f"使用 /xparse <序号> 搜索指定趋势。{more_hint}"
                )
            else:
                # 搜索/时间线翻页：逐条图文发送（与首次拉取格式对齐）
                enable_public_metrics = self.config.get("enable_public_metrics", True)
                tweet_messages: list[list] = []
                for i, item in enumerate(items_to_show, start_index):
                    tweet_obj = item.get("_tweet")
                    resp_obj = item.get("_response")
                    
                    if tweet_obj and resp_obj:
                        msg_parts, _ = await self._build_tweet_message_parts(
                            i, tweet_obj, resp_obj, enable_public_metrics
                        )
                    else:
                        # 防御性降级：缓存数据不完整时仅构建纯文本
                        text_line = (
                            f"{i}. \n"
                            f"   📝 {item.get('text', '')}...\n"
                        )
                        msg_parts = [Plain(text_line)]
                    tweet_messages.append(msg_parts)
                
                has_more = bool(cache.buffer) or bool(cache.next_token)
                more_hint = " 使用 /xnext 继续翻页。" if has_more else ""
                
                if tweet_messages:
                    for idx_offset, msg_parts in enumerate(tweet_messages, start_index):
                        await self._send_with_degradation(event, msg_parts, idx_offset)
                    
                    yield event.plain_result(
                        f"💡 第 {start_index}-{cache.page_offset} 条。"
                        f"使用 /xparse <序号> 解析指定推文。{more_hint}"
                    )
                    return
            
            yield event.plain_result("".join(output_lines))
            
        except Exception as e:
            self.logger.error(f"<xnext> 异常: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 翻页失败：{type(e).__name__}")

    # ============================================================================
    # 指令 6: 重试缩略图（/xretry <序号>）
    # ============================================================================
    
    @filter.command("xretry", alias={"重试缩略图"})
    async def cmd_retry_thumbnail(self, event: AstrMessageEvent, index: str = ""):
        """重新获取指定序号推文的缩略图（列表格式）。用法: /xretry <序号>"""
        cache_key = self._get_cache_key(event)
        
        # 参数校验
        if not index or not index.strip().isdigit():
            yield event.plain_result("❌ 请提供有效的序号（数字）。用法: /xretry 1")
            return
        
        idx = int(index.strip())
        
        # 缓存校验
        cache = self._paged_cache.get(cache_key)
        if not cache or not cache.items:
            yield event.plain_result("❌ 暂无缓存列表。请先使用 /xsearch、/xtrend、/xtl 或 /xhome 获取列表。")
            return
        
        if idx < 1 or idx > len(cache.items):
            yield event.plain_result(
                f"❌ 序号超出范围。当前列表共 {len(cache.items)} 条，请输入 1-{len(cache.items)} 之间的数字。"
            )
            return
        
        selected_item = cache.items[idx - 1]
        
        # 仅支持推文类型条目
        if selected_item.get("type") != "tweet":
            yield event.plain_result("❌ 该条目不是推文，无法重试缩略图。")
            return
        
        tweet_obj = selected_item.get("_tweet")
        resp_obj = selected_item.get("_response")
        
        if not tweet_obj or not resp_obj:
            yield event.plain_result("❌ 缓存中的推文数据不完整，无法重新获取缩略图。请发起新的搜索。")
            return
        
        self.logger.info(f"<xretry> 用户 {event.get_sender_id()} 重试第 {idx} 条缩略图")
        
        enable_public_metrics = self.config.get("enable_public_metrics", True)
        msg_parts, text_line = await self._build_tweet_message_parts(
            idx, tweet_obj, resp_obj, enable_public_metrics
        )
        
        # 检查是否包含缩略图
        has_thumb = any(isinstance(p, Image) for p in msg_parts)
        if not has_thumb:
            yield event.plain_result(f"{text_line}\n⚠️ 该推文无可用媒体缩略图。")
            return
        
        try:
            await event.send(event.chain_result(msg_parts))
            yield event.plain_result(f"✅ 第 {idx} 条推文缩略图重试成功。")
        except Exception as e:
            self.logger.warning(f"<xretry> 缩略图重试仍然超时: {e}")
            yield event.plain_result(
                f"{text_line}\n⚠️ 缩略图再次发送超时。可尝试使用 /xparse {idx} 获取完整推文。"
            )

    async def _search_with_filter(
        self,
        query: str,
        start_token: Optional[str],
        max_return_count: int,
        sort_order: str,
        min_faves: int,
        max_rounds: int,
    ) -> tuple[list[dict], str | None]:
        """
        带客户端 min_faves 过滤的多轮搜索辅助方法。

        由 cmd_search_tweets、cmd_parse_from_list（趋势路径）和 _fetch_more_items
        共同复用，确保三处搜索过滤行为完全一致。

        Args:
            query:            搜索关键词
            start_token:      起始分页令牌（None 表示从第一页开始）
            max_return_count: 目标收集条数上限
            sort_order:       排序方式（relevancy / recency）
            min_faves:        客户端最小点赞数过滤（0 = 不过滤）
            max_rounds:       最大额外轮次（0 = 仅拉取一次）

        Returns:
            tuple: (收集到的条目列表, 最后一次响应的 next_token 或 None)
        """
        all_items: list[dict] = []
        pagination_token = start_token
        last_next_token: str | None = None

        for _ in range(1 + max_rounds):
            response: SearchResponse = await self.api_client.search_recent(
                query=query,
                max_results=max_return_count,
                sort_order=sort_order,
                pagination_token=pagination_token,
                expansions="author_id,attachments.media_keys",
                tweet_fields="created_at,public_metrics,author_id",
                media_fields="url,variants,type,preview_image_url",
                user_fields="name,username,profile_image_url"
            )
            self._update_rate_limit_state(response)
            last_next_token = response.meta.next_token if response.meta else None

            if response.data:
                for tweet in response.data:
                    if min_faves > 0:
                        like_count = tweet.public_metrics.like_count if tweet.public_metrics else 0
                        if like_count < min_faves:
                            continue
                    author = self._get_author_from_includes(response, tweet.author_id)
                    tweet_url = f"https://x.com/{author['username']}/status/{tweet.id}"
                    all_items.append({
                        "url": tweet_url, "text": tweet.text[:80], "type": "tweet",
                        "_tweet": tweet, "_response": response,
                    })

            # 够了 / 没有更多 / 达到轮次上限
            if len(all_items) >= max_return_count:
                break
            # relevancy 模式下，next_token 分页是按时间倒序推进的（越翻越旧），
            # 而非"继续按相关度取更多结果"。继续分页只会带来更旧的推文，
            # 与补充热门内容的语义相悖，因此 relevancy 模式不进行多轮分页。
            if sort_order == "relevancy":
                break
            pagination_token = last_next_token
            if not pagination_token:
                break

        return all_items, last_next_token

    async def _fetch_more_items(self, cache: PagedCache) -> tuple[list[dict[str, str]], str | None]:
        """
        通过 next_token 从 API 获取下一页数据。
        仅供 cmd_next_page 调用，封装搜索/时间线的分页请求。

        搜索分支委托给 _search_with_filter，与 /xsearch 过滤行为完全一致。

        Returns:
            tuple: (新条目列表, 新的 next_token 或 None)
        """
        max_return_count = self.config.get("max_return_count", 10)

        if cache.query_type == "search":
            sort_order = self.config.get("search_sort_order", "relevancy")
            min_faves = int(self.config.get("search_min_faves", 0))
            max_rounds = int(self.config.get("search_filter_max_rounds", 0))
            items, next_token = await self._search_with_filter(
                query=cache.query_param,
                start_token=cache.next_token,
                max_return_count=max_return_count,
                sort_order=sort_order,
                min_faves=min_faves,
                max_rounds=max_rounds,
            )
            return items, next_token
        
        elif cache.query_type == "timeline":
            response: UserTimelineResponse = await self.api_client.get_user_tweets(
                user_id=cache.query_param,
                max_results=max_return_count,
                pagination_token=cache.next_token,
                expansions="author_id,attachments.media_keys",
                tweet_fields="created_at,public_metrics",
                media_fields="url,variants,type,preview_image_url",
                user_fields="name,username,profile_image_url"
            )
            self._update_rate_limit_state(response)
            
            author_username = "unknown"
            if response.includes and response.includes.users:
                author_username = response.includes.users[0].username
            
            items = []
            if response.data:
                for tweet in response.data:
                    tweet_url = f"https://x.com/{author_username}/status/{tweet.id}"
                    items.append({
                        "url": tweet_url, "text": tweet.text[:80], "type": "tweet",
                        "_tweet": tweet, "_response": response,
                    })
            
            next_token = response.meta.next_token if response.meta else None
            return items, next_token
        
        elif cache.query_type == "home":
            response: UserTimelineResponse = await self.api_client.get_home_timeline(
                user_id=cache.query_param,
                max_results=max_return_count,
                pagination_token=cache.next_token,
                expansions="author_id,attachments.media_keys",
                tweet_fields="created_at,public_metrics",
                media_fields="url,variants,type,preview_image_url",
                user_fields="name,username,profile_image_url"
            )
            self._update_rate_limit_state(response)
            
            items = []
            if response.data:
                for tweet in response.data:
                    author = self._get_author_from_includes(response, tweet.author_id)
                    tweet_url = f"https://x.com/{author['username']}/status/{tweet.id}"
                    items.append({
                        "url": tweet_url, "text": tweet.text[:80], "type": "tweet",
                        "_tweet": tweet, "_response": response,
                    })
            
            next_token = response.meta.next_token if response.meta else None
            return items, next_token
        
        return [], None

    # ============================================================================
    # 内部辅助方法：速率限制管理与数据压缩
    # ============================================================================
    
    def _update_rate_limit_state(self, response) -> None:
        """
        从 X API 响应头提取速率限制信息，更新内部状态机。
        规范要求：必须根据 x-rate-limit-remaining 和 x-rate-limit-reset 字段
        实现断路器保护机制，防止因速率超限而导致账户封禁。
        """
        try:
            # 从响应头提取速率限制信息
            if hasattr(response, 'headers'):
                remaining = int(response.headers.get('x-rate-limit-remaining', float('inf')))
                reset_timestamp = int(response.headers.get('x-rate-limit-reset', 0))
                
                self.rate_limit_state["remaining"] = remaining
                
                # 安全阈值：当剩余请求数 < 5 时触发断路器
                if remaining < 5:
                    self.rate_limit_state["circuit_breaker_active"] = True
                    self.rate_limit_state["reset_at"] = reset_timestamp
                    self.logger.warning(
                        f"⚠️ 速率限制警告：剩余请求数 {remaining}，"
                        f"将于 {reset_timestamp} 重置。断路器已激活。"
                    )
                else:
                    self.rate_limit_state["circuit_breaker_active"] = False
                    
        except Exception as e:
            self.logger.error(f"速率限制状态更新失败: {str(e)}")
    
    def _is_rate_limited(self) -> bool:
        """
        检查断路器是否仍然激活。
        如果速率限制窗口已重置（当前时间 > reset_at），则自动解除断路器。
        """
        if not self.rate_limit_state["circuit_breaker_active"]:
            return False
        
        reset_at = self.rate_limit_state.get("reset_at")
        if reset_at and time.time() > reset_at:
            self.rate_limit_state["circuit_breaker_active"] = False
            self.rate_limit_state["remaining"] = float('inf')
            self.rate_limit_state["reset_at"] = None
            self.logger.info("断路器已自动恢复：速率限制窗口已重置。")
            return False
        
        return True
    
    async def _build_tweet_message_parts(
        self, index: int, tweet, response, enable_public_metrics: bool
    ) -> tuple[list, str]:
        """
        构建单条推文的列表格式消息组件和文本行。
        统一用于 /xsearch、/xparse、/xtl、/xnext、/xretry 的推文格式化。
        
        Args:
            index: 显示序号
            tweet: 推文对象
            response: 包含 includes 的响应对象
            enable_public_metrics: 是否显示互动指标
        
        Returns:
            tuple: (msg_parts 消息组件列表, text_line 纯文本行)
        """
        author = self._get_author_from_includes(response, tweet.author_id)
        
        metrics_str = ""
        if enable_public_metrics and tweet.public_metrics:
            metrics_str = f" | ❤️{tweet.public_metrics.like_count} 🔄{tweet.public_metrics.retweet_count}"
        
        img_count, vid_count = self._count_tweet_media(tweet, response)
        media_summary = ""
        if img_count or vid_count:
            media_parts = []
            if img_count:
                media_parts.append(f"{img_count}张图片")
            if vid_count:
                media_parts.append(f"{vid_count}个视频")
            media_summary = f"\n   📎 完整推文包含：{'，'.join(media_parts)}，使用 /xparse {index} 获取完整推文。"
        else:
            media_summary = f"\n   📄 纯文本推文，使用 /xparse {index} 查看完整内容。"
        
        text_line = (
            f"{index}. @{author['username']} ({author['name']})\n"
            f"   📝 {tweet.text[:150]}...\n"
            f"   ⏰ {tweet.created_at}{metrics_str}{media_summary}\n"
        )
        
        msg_parts = []
        thumb = await self._get_first_media_thumbnail(tweet, response)
        if thumb:
            msg_parts.append(thumb)
        msg_parts.append(Plain(text_line))
        return msg_parts, text_line
    
    def _count_tweet_media(self, tweet, response) -> tuple[int, int]:
        """
        统计推文中的媒体数量。GIF 计入图片。
        
        Returns:
            tuple: (图片+GIF数量, 视频数量)
        """
        if not tweet.attachments or not tweet.attachments.media_keys:
            return 0, 0
        if not response.includes:
            return 0, 0
        
        img_count = 0
        vid_count = 0
        for key in tweet.attachments.media_keys:
            media = response.includes.find_media_by_key(key)
            if media:
                if media.type in ("photo", "animated_gif"):
                    img_count += 1
                elif media.type == "video":
                    vid_count += 1
        return img_count, vid_count
    
    async def _send_with_degradation(
        self, event: AstrMessageEvent, msg_parts: list, display_index: int
    ) -> None:
        """
        方案B：尝试发送图文消息，超时/失败则自动降级为纯文本重试。
        
        降级后在文本末尾追加提示，告知用户可使用 /xretry 重试缩略图。
        
        Args:
            event: 消息事件
            msg_parts: 消息组件列表（可能含 Image）
            display_index: 该条推文在列表中的显示序号（用于 /xretry 提示）
        """
        has_image = any(isinstance(p, Image) for p in msg_parts)
        
        try:
            await event.send(event.chain_result(msg_parts))
        except Exception as e:
            if not has_image:
                self.logger.error(f"纯文本消息发送也失败: {e}")
                return
            
            self.logger.warning(f"图文消息发送超时，降级为纯文本重试: {e}")
            text_parts = [p for p in msg_parts if isinstance(p, Plain)]
            text_parts.append(Plain(
                f"\n⚠️ 缩略图发送超时，已降级为纯文本。"
                f"可使用 /xretry {display_index} 指令尝试重新查看缩略图"
            ))
            try:
                await event.send(event.chain_result(text_parts))
            except Exception as e2:
                self.logger.error(f"降级纯文本重试也失败: {e2}")
    
    async def _get_first_media_thumbnail(
        self, tweet, response: SearchResponse
    ) -> Optional[Image]:
        """
        获取推文的第一个媒体的缩略图组件。

        图片类型：下载原图后生成缩略图。
        视频/GIF 类型：使用 X API 返回的 preview_image_url（视频预览图）。
        所有媒体统一 resize 为低分辨率 JPEG 缩略图。

        Args:
            tweet: 推文对象
            response: 包含 includes 的响应对象

        Returns:
            Image 组件，或 None（无媒体/下载失败）
        """
        if not tweet.attachments or not tweet.attachments.media_keys:
            return None
        if not response.includes or not response.includes.media:
            return None
        
        # 取第一个 media_key 对应的媒体
        first_key = tweet.attachments.media_keys[0]
        target_media = response.includes.find_media_by_key(first_key)
        
        if not target_media:
            return None
        
        # 确定缩略图源 URL
        thumb_url = None
        if target_media.type == "photo" and target_media.url:
            thumb_url = target_media.url
        elif target_media.type in ("video", "animated_gif"):
            # 视频/GIF 优先使用 preview_image_url
            thumb_url = target_media.preview_image_url
        
        if not thumb_url:
            return None
        
        try:
            img_data = await self.media_processor.download_media(thumb_url)
            if not img_data:
                return None
            thumb_data = await self.media_processor.generate_thumbnail(img_data)
            # 缩略图落盘缓存
            thumb_filename = f"thumb_{tweet.id}_{hash(thumb_url) & 0xFFFFFFFF}.jpg"
            thumb_path = self._cache_thumbnails / thumb_filename
            thumb_path.write_bytes(thumb_data)
            return Image.fromFileSystem(str(thumb_path))
        except Exception as e:
            self.logger.warning(f"缩略图生成失败: {thumb_url} - {e}")
            return None
    
    def _compress_trends_response(self, response: TrendsResponse, region_name: str = "全球") -> str:
        """将趋势数据压缩为简洁列表。"""
        if not response.data:
            return f"📊 {region_name}趋势：暂无数据。"
        
        output_lines = [f"📊 {region_name}当前趋势：\n"]
        
        for i, trend in enumerate(response.data[:10], 1):  # 限制输出前 10 条
            output_lines.append(
                f"{i}. #{trend.trend_name}\n"
                f"   💬 {trend.tweet_count or 'N/A'} 条推文\n"
            )
        
        output_lines.append("\n💡 使用 /xsearch <趋势关键词> 搜索相关推文，然后 /xparse <序号> 解析指定推文。")
        
        return "".join(output_lines)
    
    def _compress_tweet_response(self, response: TweetResponse, enable_public_metrics: bool) -> str:
        """压缩单条推文详情。"""
        if not response.data:
            return "❌ 推文不存在或已删除。"
        
        tweet = response.data
        author = self._get_author_from_includes(response, tweet.author_id)
        
        metrics_str = ""
        if enable_public_metrics and tweet.public_metrics:
            metrics_str = (
                f"❤️ {tweet.public_metrics.like_count} | "
                f"🔄 {tweet.public_metrics.retweet_count} | "
                f"💬 {tweet.public_metrics.reply_count}"
            )
        
        media_str = self._extract_media_info(response, tweet)
        
        return (
            f"📄 推文详情\n"
            f"👤 {author['name']} (@{author['username']})\n"
            f"📝 {tweet.text}\n"
            f"⏰ {tweet.created_at}\n"
            f"{metrics_str}\n"
            f"{media_str}"
        )
    
    def _compress_user_timeline_response(self, response: UserTimelineResponse, enable_public_metrics: bool) -> str:
        """压缩用户时间线。"""
        if not response.data:
            return "👤 该用户暂无推文。"
        
        # 从 includes 中提取作者用户名，用于构建推文链接
        author_username = "unknown"
        if response.includes and response.includes.users:
            author_username = response.includes.users[0].username
        
        output_lines = ["👤 用户时间线：\n"]
        
        for i, tweet in enumerate(response.data[:5], 1):
            metrics_str = ""
            if enable_public_metrics and tweet.public_metrics:
                metrics_str = f" | ❤️{tweet.public_metrics.like_count}"
            
            tweet_url = f"https://x.com/{author_username}/status/{tweet.id}"
            output_lines.append(
                f"{i}. {tweet.text[:100]}...{metrics_str}\n   🔗 {tweet_url}\n"
            )
        
        output_lines.append(
            "\n💡 以上为纯文本摘要。直接发送推文链接到聊天即可自动解析完整内容与媒体。"
        )
        
        return "".join(output_lines)
    
    def _get_author_from_includes(self, response, author_id: str) -> dict:
        """从 includes 中查询作者信息。"""
        if response.includes:
            return response.includes.get_author_display(author_id)
        return {"name": "Unknown", "username": "unknown"}
    
    def _extract_media_info(self, response: TweetResponse, tweet) -> str:
        """提取推文中的媒体摘要信息（不含原始 URL，媒体由 _send_tweet_media 直接发送）。"""
        if not tweet.attachments or not tweet.attachments.media_keys:
            return ""
        
        photo_count = 0
        video_count = 0
        gif_count = 0
        if response.includes and response.includes.media:
            for key in tweet.attachments.media_keys:
                for media in response.includes.media:
                    if str(media.media_key) == str(key):
                        if media.type == "photo":
                            photo_count += 1
                        elif media.type == "video":
                            video_count += 1
                        elif media.type == "animated_gif":
                            gif_count += 1
        
        parts = []
        if video_count:
            parts.append(f"🎬 {video_count} 个视频")
        if gif_count:
            parts.append(f"🎞️ {gif_count} 个GIF")
        
        if parts:
            return "📎 " + ", ".join(parts) + "（已发送至聊天）"
        if photo_count:
            return f"📸 {photo_count} 张图片"
        return ""
    
    async def _process_tweet_media(self, response: TweetResponse) -> TweetResponse:
        """
        媒体处理管线：变体筛选（为视频/GIF 选择最优 MP4 变体）。
        """
        if not response.includes or not response.includes.media:
            return response
        
        for media in response.includes.media:
            if media.variants:
                best_variant = self.media_processor.select_best_variant(media.variants)
                if best_variant:
                    media.url = best_variant.get("url")
        
        return response
    
    async def _send_tweet_media(self, event: AstrMessageEvent, response: TweetResponse) -> list:
        """
        处理推文媒体：下载图片并返回组件列表（供图文混排），视频单独发送。
        图片：下载 → 压缩 → 返回 Image 组件列表。
        视频/GIF：下载 → 保存临时文件 → 直接发送 Video 组件。
        大文件：熔断拦截，降级输出直链文本。

        Returns:
            图片组件列表，用于与推文文字合并成图文混排消息。
        """
        tweet = response.data
        if not tweet or not tweet.attachments or not tweet.attachments.media_keys:
            return []
        if not response.includes or not response.includes.media:
            return []
        
        image_components = []
        video_tasks = []
        
        for key in tweet.attachments.media_keys:
            for media in response.includes.media:
                if str(media.media_key) != str(key):
                    continue
                
                if media.type == "photo" and media.url:
                    try:
                        intercepted, size = await self.media_processor.should_intercept(media.url)
                        if intercepted:
                            fallback = self.media_processor.build_fallback_text("photo", media.url, size)
                            image_components.append(Plain(fallback))
                            continue
                        
                        img_data = await self.media_processor.download_media(media.url)
                        if img_data:
                            img_data = await self.media_processor.compress_image(img_data)
                            # 图片落盘缓存
                            img_filename = f"img_{tweet.id}_{hash(media.url) & 0xFFFFFFFF}.jpg"
                            img_path = self._cache_images / img_filename
                            img_path.write_bytes(img_data)
                            image_components.append(Image.fromFileSystem(str(img_path)))
                    except Exception as e:
                        self.logger.warning(f"图片下载失败: {media.url} - {e}")
                
                elif media.type in ("video", "animated_gif") and media.url:
                    video_tasks.append({"url": media.url, "type": media.type})
        
        # 发送视频（逐条单独发送）
        for video_info in video_tasks:
            url = video_info["url"]
            try:
                intercepted, size = await self.media_processor.should_intercept(url)
                if intercepted:
                    fallback = self.media_processor.build_fallback_text(video_info["type"], url, size)
                    await event.send(event.chain_result([Plain(fallback)]))
                    continue
                
                vid_data = await self.media_processor.download_media(url)
                if vid_data:
                    vid_path = self._cache_videos / f"vid_{tweet.id}_{hash(url) & 0xFFFFFFFF}.mp4"
                    vid_path.write_bytes(vid_data)
                    await event.send(event.chain_result([Video(str(vid_path))]))
            except Exception as e:
                self.logger.warning(f"视频发送失败: {url} - {e}")
        
        return image_components
    
    def _extract_tweet_id_from_url(self, url: str) -> Optional[str]:
        """
        从推文 URL 中解析推文 ID。
        支持格式: https://x.com/username/status/1234567890
               https://twitter.com/username/status/1234567890
        """
        match = re.search(r'/status/(\d+)', url)
        return match.group(1) if match else None
    
    # ============================================================================
    # 缓存清理
    # ============================================================================
    
    def _cleanup_expired_cache(self) -> None:
        """
        清理超过 cache_expire_days 天的媒体缓存文件。
        遍历 images / thumbnails / videos 三个子目录，按文件修改时间判定过期。
        """
        if self._cache_expire_days <= 0:
            return
        
        expire_seconds = self._cache_expire_days * 86400
        now = time.time()
        cleaned = 0
        
        for cache_sub in (self._cache_images, self._cache_thumbnails, self._cache_videos):
            if not cache_sub.exists():
                continue
            for f in cache_sub.iterdir():
                if f.is_file():
                    try:
                        if now - f.stat().st_mtime > expire_seconds:
                            f.unlink()
                            cleaned += 1
                    except OSError as e:
                        self.logger.warning(f"缓存清理失败: {f} - {e}")
        
        if cleaned > 0:
            self.logger.info(f"缓存清理完成：已删除 {cleaned} 个过期文件（>{self._cache_expire_days}天）")
    
    # ============================================================================
    # 生命周期管理
    # ============================================================================
    
    async def terminate(self):
        """
        插件卸载/停用时调用。用于清理资源、关闭连接等。
        """
        try:
            # 关闭异步 HTTP 客户端连接
            if hasattr(self.api_client, 'close'):
                await self.api_client.close()
            
            # 关闭媒体处理器的 HTTP 客户端
            if hasattr(self.media_processor, 'close'):
                await self.media_processor.close()
            
            self.logger.info("XAgentToolkitPlugin 已卸载，资源已清理")
        except Exception as e:
            self.logger.error(f"插件卸载时发生错误: {str(e)}", exc_info=True)
