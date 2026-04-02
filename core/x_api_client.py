"""
X (Twitter) API v2 异步 HTTP 客户端实现

规范文档参考：
- 异步网络基建与事件循环保护 (Line 237-250)
- 全局网络代理与流量调度架构 (Line 252-280)
- X (Twitter) API v2 深度集成策略与数据交换架构 (Line 74-141)

认证方式：
- 主要: OAuth 1.0a 用户上下文认证（HMAC-SHA1 签名，需 Consumer Key/Secret + Access Token/Secret）
- 备用: OAuth 2.0 App-Only Bearer Token
- 降级: Cookie 模拟浏览器请求

官方文档：
- https://docs.x.com/x-api/
- https://docs.x.com/fundamentals/authentication/oauth-1-0a/authorizing-a-request
- https://docs.x.com/fundamentals/authentication/oauth-1-0a/creating-a-signature
"""

import asyncio
import hashlib
import hmac
import re
import secrets
import time
import urllib.parse
from base64 import b64encode
from typing import Optional, Dict, Any
from datetime import datetime
import json
import httpx

from astrbot.api import logger
from ..models.x_response_models import (
    SearchResponse, 
    TweetResponse, 
    UserTimelineResponse, 
    TrendsResponse,
    UserLookupResponse
)


# Twitter 网页端内嵌的公开客户端 Bearer Token（Twitter Web App 全局固定值）
# 该 Token 不代表任何用户，是 Twitter 自身网页客户端对内部 API 的公开应用凭证。
# 已由 snscrape 等多个知名开源项目长期验证，保持稳定。
_TWITTER_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs="
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL TweetResultByRestId 端点配置
# queryId 为 Twitter 前端部署期变量，随前端更新可能变更；
# 可在 WebUI 中通过 graphql_tweet_query_id 字段覆写
# （浏览器 F12 → Network → 过滤 TweetResultByRestId 请求获取最新值）。
_GRAPHQL_TWEET_QUERY_ID = "0hWvDhmW8YQ-S_ib3azIrw"

# features 参数：与 Twitter Web App 默认特性集合对齐（控制响应中包含的数据集合）
_GRAPHQL_TWEET_FEATURES = json.dumps(
    {
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    },
    separators=(",", ":"),
)


class XApiClient:
    """
    X API v2 异步客户端
    
    核心责任：
    1. 管理 httpx.AsyncClient 连接池与代理配置
    2. 实现 5 个核心 API 端点的异步调用
    3. 提取速率限制头部信息
    4. 处理错误码与降级策略（302 重定向、403 Forbidden Cookie 降级、429 限流）
    5. 支持 Pydantic 模型自动验证与反序列化
    
    规范文档参考：
    - 身份验证矩阵与降级访问策略 (Line 81-94)
    - 大文件拦截与动态压缩流水线 (Line 227-242)
    """
    
    def __init__(
        self,
        bearer_token: str = "",
        api_key: Optional[str] = None,
        api_key_secret: Optional[str] = None,
        oauth_access_token: Optional[str] = None,
        oauth_access_token_secret: Optional[str] = None,
        cookie_auth_token: Optional[str] = None,
        cookie_ct0: Optional[str] = None,
        graphql_tweet_query_id: Optional[str] = None,
        enable_proxy: bool = True,
        proxy_url: str = "http://127.0.0.1:7890"
    ):
        """
        初始化 X API 客户端
        
        认证优先级（三级降级策略）：
        1. OAuth 1.0a 用户上下文（需要全部 4 个凭据）—— 拥有完整的用户级权限
        2. OAuth 2.0 App-Only Bearer Token —— 仅限公开数据的只读操作
        3. Cookie 降级 —— 当 API 额度耗尽或权限不足时，通过 Twitter 网页端内部 API 获取数据
        
        Args:
            bearer_token (str): OAuth 2.0 Bearer Token（备用认证）
            api_key (str, optional): Consumer Key / API Key（OAuth 1.0a）
            api_key_secret (str, optional): Consumer Secret / API Key Secret（OAuth 1.0a）
            oauth_access_token (str, optional): Access Token（OAuth 1.0a）
            oauth_access_token_secret (str, optional): Access Token Secret（OAuth 1.0a）
            cookie_auth_token (str, optional): Twitter 网页端登录凭证（auth_token Cookie 值）
            cookie_ct0 (str, optional): Twitter CSRF 防护令牌（ct0 Cookie 值，与 auth_token 配对）
            graphql_tweet_query_id (str, optional): GraphQL TweetResultByRestId queryId（留空使用内置默认值）
            enable_proxy (bool): 是否启用代理（默认 True）
            proxy_url (str): 代理地址（默认 http://127.0.0.1:7890 - Clash）
            
        规范文档参考：
        - 身份验证矩阵与降级访问策略 (Line 81-94)
        - 全局网络代理与流量调度架构 (Line 252-280)
        - OAuth 1.0a: https://docs.x.com/fundamentals/authentication/oauth-1-0a/authorizing-a-request
        """
        self.bearer_token = bearer_token
        self.api_key = api_key
        self.api_key_secret = api_key_secret
        self.oauth_access_token = oauth_access_token
        self.oauth_access_token_secret = oauth_access_token_secret
        self.cookie_auth_token = cookie_auth_token
        self.cookie_ct0 = cookie_ct0
        self.graphql_tweet_query_id = graphql_tweet_query_id or ""
        self.enable_proxy = enable_proxy
        self.proxy_url = proxy_url
        
        # 判定 OAuth 1.0a 是否可用（需要全部 4 个凭据）
        self.oauth1_available = all([
            self.api_key,
            self.api_key_secret,
            self.oauth_access_token,
            self.oauth_access_token_secret
        ])
        
        # 判定 Cookie 降级是否可用（auth_token 与 ct0 必须同时配置）
        self.cookie_available = bool(self.cookie_auth_token and self.cookie_ct0)
        
        # 初始化异步 HTTP 客户端
        # 规范要求：必须使用 httpx.AsyncClient，支持 HTTP/2、连接池、代理隧道
        self.client = None
        self.session_started = False
        
        auth_mode = (
            "OAuth 1.0a（用户上下文）" if self.oauth1_available
            else "Bearer Token（App-Only）" if self.bearer_token
            else "Cookie 降级（v1.1 内部 API）" if self.cookie_available
            else "无认证"
        )
        logger.info(
            f"XApiClient 已初始化 | "
            f"认证模式: {auth_mode} | "
            f"Cookie 降级: {'已就绪' if self.cookie_available else '未配置'} | "
            f"Proxy: {'已启用 (' + proxy_url + ')' if enable_proxy else '已禁用'}"
        )
    
    async def _ensure_client(self):
        """
        延迟初始化 asyncio 上下文下的 HTTP 客户端
        
        规范要求：严禁在 __init__ 中执行网络操作，必须延后至事件循环启动
        规范文档参考：与 AstrBot 接口的对接范式与异常处理 (Line 220-235)
        """
        if self.client is None:
            # 构建代理配置
            proxy = None
            if self.enable_proxy:
                proxy = self.proxy_url
            
            # 初始化异步客户端
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),  # 默认 10 秒超时
                proxy=proxy,  # 代理配置（httpx >= 0.25.0 使用单数形式）
                verify=True,  # HTTPS 验证
                http2=True  # 启用 HTTP/2
            )
            self.session_started = True
    
    # ========================================================================
    # OAuth 1.0a HMAC-SHA1 签名生成
    # 参考：https://docs.x.com/fundamentals/authentication/oauth-1-0a/creating-a-signature
    # ========================================================================
    
    @staticmethod
    def _percent_encode(s: str) -> str:
        """
        OAuth 1.0a 规范的百分号编码。
        RFC 5849 要求对除 unreserved 字符（A-Z, a-z, 0-9, -, ., _, ~）外的所有字符进行编码。
        """
        return urllib.parse.quote(str(s), safe="")
    
    def _generate_oauth1_header(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        生成 OAuth 1.0a Authorization 请求头。
        
        完整实现 HMAC-SHA1 签名流程：
        1. 收集所有 oauth_* 参数和请求查询参数
        2. 按字母序排列并构建参数字符串
        3. 构建签名基础字符串 (signature base string)
        4. 使用 Consumer Secret 和 Token Secret 组合为签名密钥
        5. HMAC-SHA1 签名并 Base64 编码
        6. 组装 Authorization 头部
        
        参考文档：
        - https://docs.x.com/fundamentals/authentication/oauth-1-0a/authorizing-a-request
        - https://docs.x.com/fundamentals/authentication/oauth-1-0a/creating-a-signature
        
        Args:
            method: HTTP 方法 (GET/POST)
            url: 完整的 API 端点 URL（不含查询参数）
            params: 请求查询参数字典
            
        Returns:
            str: 完整的 OAuth Authorization 头部值
        """
        # Step 1: 生成 OAuth 基础参数
        oauth_nonce = secrets.token_hex(16)
        oauth_timestamp = str(int(time.time()))
        
        oauth_params = {
            "oauth_consumer_key": self.api_key,
            "oauth_nonce": oauth_nonce,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": oauth_timestamp,
            "oauth_token": self.oauth_access_token,
            "oauth_version": "1.0"
        }
        
        # Step 2: 收集所有参数（oauth_* + 查询参数），用于签名计算
        all_params = dict(oauth_params)
        if params:
            for k, v in params.items():
                if v is not None:
                    all_params[k] = str(v)
        
        # Step 3: 按 key 字母序排列，构建参数字符串
        sorted_params = sorted(all_params.items(), key=lambda x: (x[0], x[1]))
        param_string = "&".join(
            f"{self._percent_encode(k)}={self._percent_encode(v)}"
            for k, v in sorted_params
        )
        
        # Step 4: 解析 base URL（去除查询字符串）
        parsed = urllib.parse.urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        
        # Step 5: 构建签名基础字符串
        # 格式: METHOD&percent_encode(base_url)&percent_encode(param_string)
        signature_base = (
            f"{method.upper()}&"
            f"{self._percent_encode(base_url)}&"
            f"{self._percent_encode(param_string)}"
        )
        
        # Step 6: 构建签名密钥
        # 格式: percent_encode(consumer_secret)&percent_encode(token_secret)
        signing_key = (
            f"{self._percent_encode(self.api_key_secret)}&"
            f"{self._percent_encode(self.oauth_access_token_secret)}"
        )
        
        # Step 7: HMAC-SHA1 签名 + Base64 编码
        hashed = hmac.new(
            signing_key.encode("utf-8"),
            signature_base.encode("utf-8"),
            hashlib.sha1
        )
        oauth_signature = b64encode(hashed.digest()).decode("utf-8")
        
        # Step 8: 组装 Authorization 头部
        oauth_params["oauth_signature"] = oauth_signature
        auth_header = "OAuth " + ", ".join(
            f'{self._percent_encode(k)}="{self._percent_encode(v)}"'
            for k, v in sorted(oauth_params.items())
        )
        
        return auth_header

    # ========================================================================
    # Cookie 降级认证 — v1.1 内部 API 通道
    # ========================================================================

    def _build_cookie_auth_headers(self) -> Dict[str, str]:
        """构建 Cookie 降级认证所需的完整请求头（适用于 Twitter v1.1 内部 API）"""
        return {
            "Authorization": f"Bearer {_TWITTER_WEB_BEARER}",
            "Cookie": f"auth_token={self.cookie_auth_token}; ct0={self.cookie_ct0}",
            "x-csrf-token": self.cookie_ct0 or "",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://twitter.com/",
        }

    @staticmethod
    def _parse_v1_datetime(date_str: str) -> Optional[str]:
        """将 v1.1 API 的日期格式 ("Thu Apr 06 15:28:43 +0000 2023") 转换为 ISO 8601"""
        if not date_str:
            return None
        try:
            dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
            return dt.isoformat()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _adapt_v1_media(v1_media: dict) -> dict:
        """将 v1.1 媒体对象转换为 v2 兼容格式"""
        media_id = v1_media.get("id_str", "")
        media_key = f"3_{media_id}"
        media_type = v1_media.get("type", "photo")
        result: Dict[str, Any] = {"media_key": media_key, "type": media_type}
        if media_type == "photo":
            result["url"] = v1_media.get("media_url_https", "")
        else:
            result["preview_image_url"] = v1_media.get("media_url_https", "")
            video_info = v1_media.get("video_info", {})
            variants = []
            for v in video_info.get("variants", []):
                variant = {
                    "content_type": v.get("content_type", ""),
                    "url": v.get("url", ""),
                }
                if "bitrate" in v:
                    variant["bit_rate"] = v["bitrate"]
                variants.append(variant)
            result["variants"] = variants
            if "duration_millis" in video_info:
                result["duration_ms"] = video_info["duration_millis"]
        return result

    def _adapt_v1_tweets_to_v2(self, v1_tweets: list) -> dict:
        """将 v1.1 推文列表适配为 v2 data/includes 结构"""
        tweets_v2: list = []
        users_map: dict = {}
        media_list: list = []

        for v1_tweet in v1_tweets:
            v1_user = v1_tweet.get("user", {})
            author_id = v1_user.get("id_str", "")
            tweet_media_keys: list = []

            # 处理媒体附件（优先 extended_entities，保留 GIF/视频变体）
            ext_ent = v1_tweet.get("extended_entities") or v1_tweet.get("entities") or {}
            for m in ext_ent.get("media", []):
                adapted_media = self._adapt_v1_media(m)
                media_list.append(adapted_media)
                tweet_media_keys.append(adapted_media["media_key"])

            tweet_v2: Dict[str, Any] = {
                "id": v1_tweet.get("id_str", ""),
                "text": v1_tweet.get("full_text", v1_tweet.get("text", "")),
                "author_id": author_id,
                "created_at": self._parse_v1_datetime(v1_tweet.get("created_at")),
                "public_metrics": {
                    "like_count": v1_tweet.get("favorite_count", 0),
                    "retweet_count": v1_tweet.get("retweet_count", 0),
                    "reply_count": v1_tweet.get("reply_count", 0),
                },
                "edit_history_tweet_ids": [v1_tweet.get("id_str", "")],
                "lang": v1_tweet.get("lang"),
            }
            if tweet_media_keys:
                tweet_v2["attachments"] = {"media_keys": tweet_media_keys}
            tweets_v2.append(tweet_v2)

            if author_id and author_id not in users_map:
                users_map[author_id] = {
                    "id": author_id,
                    "name": v1_user.get("name", ""),
                    "username": v1_user.get("screen_name", ""),
                    "profile_image_url": v1_user.get("profile_image_url_https", ""),
                }

        return {
            "data": tweets_v2,
            "includes": {"users": list(users_map.values()), "media": media_list},
            "meta": {},
        }

    def _translate_v2_to_v1_url(
        self, v2_url: str, v2_params: Dict[str, Any]
    ) -> tuple:
        """
        将 v2 API URL 和参数翻译为 v1.1 等价调用。

        Returns:
            tuple: (v1_url, v1_params, endpoint_type)
        """
        if "/2/tweets/search/recent" in v2_url:
            v1_params = {
                "q": v2_params.get("query", ""),
                "count": min(int(v2_params.get("max_results", 20)), 100),
                "result_type": "recent",
                "include_entities": "true",
                "tweet_mode": "extended",
            }
            return "https://twitter.com/i/api/1.1/search/tweets.json", v1_params, "search"

        m = re.match(r".*/2/tweets/(\d+)$", v2_url)
        if m:
            return (
                "https://twitter.com/i/api/1.1/statuses/show.json",
                {"id": m.group(1), "include_entities": "true", "tweet_mode": "extended"},
                "tweet",
            )

        m = re.match(r".*/2/users/(\d+)/tweets$", v2_url)
        if m:
            return (
                "https://twitter.com/i/api/1.1/statuses/user_timeline.json",
                {
                    "user_id": m.group(1),
                    "count": min(int(v2_params.get("max_results", 20)), 200),
                    "include_rts": "true",
                    "exclude_replies": "false",
                    "tweet_mode": "extended",
                },
                "timeline",
            )

        m = re.match(r".*/2/users/by/username/([^/]+)$", v2_url)
        if m:
            return (
                "https://twitter.com/i/api/1.1/users/show.json",
                {"screen_name": m.group(1), "include_entities": "false"},
                "user_lookup",
            )

        raise ValueError(f"❌ Cookie 降级认证不支持该 API 端点：{v2_url}")

    def _adapt_v1_response(self, v1_data: Any, endpoint_type: str) -> dict:
        """将 v1.1 响应数据适配为 v2 兼容格式"""
        if endpoint_type == "search":
            return self._adapt_v1_tweets_to_v2(v1_data.get("statuses", []))
        elif endpoint_type == "tweet":
            adapted = self._adapt_v1_tweets_to_v2([v1_data])
            return {
                "data": adapted["data"][0] if adapted["data"] else {},
                "includes": adapted["includes"],
            }
        elif endpoint_type == "timeline":
            return self._adapt_v1_tweets_to_v2(
                v1_data if isinstance(v1_data, list) else []
            )
        elif endpoint_type == "user_lookup":
            return {
                "data": {
                    "id": v1_data.get("id_str", ""),
                    "name": v1_data.get("name", ""),
                    "username": v1_data.get("screen_name", ""),
                    "profile_image_url": v1_data.get("profile_image_url_https", ""),
                }
            }
        return v1_data

    def _adapt_graphql_tweet_response(self, gql_data: dict) -> dict:
        """将 GraphQL TweetResultByRestId 响应适配为 v2 兼容的单推文格式。"""
        result = (
            gql_data
            .get("data", {})
            .get("tweetResult", {})
            .get("result", {})
        )
        # __typename 可能为 "Tweet" 或 "TweetUnavailable"（限制访问/已删除）
        if not result or result.get("__typename") != "Tweet":
            return {"data": {}, "includes": {"users": [], "media": []}}
        legacy = result.get("legacy", {})
        user_legacy = (
            result
            .get("core", {})
            .get("user_results", {})
            .get("result", {})
            .get("legacy", {})
        )
        # GraphQL legacy 字段与 v1.1 字段高度兼容，复用现有适配器
        v1_like = {
            "id_str": legacy.get("id_str", ""),
            "full_text": legacy.get("full_text", legacy.get("text", "")),
            "created_at": legacy.get("created_at", ""),
            "favorite_count": legacy.get("favorite_count", 0),
            "retweet_count": legacy.get("retweet_count", 0),
            "reply_count": legacy.get("reply_count", 0),
            "lang": legacy.get("lang"),
            "extended_entities": legacy.get("extended_entities"),
            "entities": legacy.get("entities"),
            "user": {
                "id_str": user_legacy.get("id_str", ""),
                "name": user_legacy.get("name", ""),
                "screen_name": user_legacy.get("screen_name", ""),
                "profile_image_url_https": user_legacy.get("profile_image_url_https", ""),
            },
        }
        adapted_list = self._adapt_v1_tweets_to_v2([v1_like])
        return {
            "data": adapted_list["data"][0] if adapted_list["data"] else {},
            "includes": adapted_list["includes"],
        }

    async def _make_graphql_tweet_request(
        self,
        tweet_id: str,
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        使用 GraphQL TweetResultByRestId 端点获取单条推文（Cookie 降级认证专用）。

        Twitter v1.1 statuses/show.json 对 Cookie 鉴权已失效；
        Twitter Web 客户端已全面迁移至 GraphQL，此方法复现该行为。
        queryId 为 Twitter 前端部署期变量，可在 WebUI 中通过 graphql_tweet_query_id 覆写。
        """
        query_id = self.graphql_tweet_query_id or _GRAPHQL_TWEET_QUERY_ID
        url = f"https://twitter.com/i/api/graphql/{query_id}/TweetResultByRestId"
        variables = json.dumps(
            {
                "tweetId": tweet_id,
                "withCommunity": False,
                "includePromotedContent": False,
                "withVoice": False,
            },
            separators=(",", ":"),
        )
        params = {"variables": variables, "features": _GRAPHQL_TWEET_FEATURES}
        logger.info(
            f"Cookie GraphQL 推文查询 | tweet_id: {tweet_id} | queryId: {query_id}"
        )
        try:
            response = await self.client.request(
                "GET", url, headers=headers, params=params
            )
            if response.status_code >= 400:
                logger.warning(
                    f"GraphQL 推文查询错误响应 [{response.status_code}] | "
                    f"Body: {response.text[:500]}"
                )
            if response.status_code == 401:
                raise ValueError(
                    "❌ Cookie GraphQL 认证失败 (401)：auth_token 或 ct0 无效或已过期。\n"
                    "请重新从浏览器获取最新 auth_token 和 ct0 后更新配置。"
                )
            elif response.status_code == 403:
                raise ValueError(
                    "❌ Cookie GraphQL 认证失败 (403)：CSRF Token 不匹配或账户存在访问限制。"
                )
            elif response.status_code == 404:
                raise ValueError(
                    "❌ Cookie GraphQL 请求失败 (404)：queryId 可能已失效。\n"
                    "请在 WebUI 中更新「graphql_tweet_query_id」字段"
                    "（浏览器 F12 → Network → 过滤 TweetResultByRestId 获取最新值）。"
                )
            elif response.status_code == 429:
                raise ValueError(
                    "⚠️ Cookie GraphQL 触发速率限制 (429)：请求过于频繁，请稍后重试。"
                )
            elif response.status_code >= 400:
                raise ValueError(
                    f"❌ Cookie GraphQL 请求失败 ({response.status_code})："
                    f"{response.text[:200]}"
                )
            gql_data = response.json() if response.text else {}
            # 检查 GraphQL 层面的不可访问状态
            tweet_result = (
                gql_data.get("data", {}).get("tweetResult", {}).get("result", {})
            )
            if tweet_result.get("__typename") == "TweetUnavailable":
                raise FileNotFoundError(f"推文 {tweet_id} 不可访问或已删除")
            adapted = self._adapt_graphql_tweet_response(gql_data)
            return {"data": adapted, "headers": dict(response.headers)}
        except httpx.TimeoutException:
            raise ValueError("❌ Cookie GraphQL 请求超时，请检查网络或代理配置。")
        except httpx.ConnectError as e:
            raise ValueError(f"❌ Cookie GraphQL 连接失败：{str(e)}")
        except (ValueError, FileNotFoundError):
            raise
        except Exception as e:
            raise ValueError(
                f"❌ Cookie GraphQL 请求异常：{type(e).__name__} - {str(e)}"
            )

    async def _make_v1_cookie_request(
        self,
        method: str,
        v2_url: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        使用 Cookie 认证向 Twitter v1.1 内部 API 发起请求，并将结果适配为 v2 格式。

        Twitter v2 API 不接受 Cookie 认证；此方法将 v2 请求路由至对应的 v1.1
        端点，附加必需的 x-csrf-token / Cookie 头，再将 v1.1 响应转换回 v2
        兼容格式供上层 Pydantic 模型解析。
        """
        if not self.cookie_available:
            raise ValueError(
                "❌ Cookie 降级认证失败：未配置 auth_token 或 ct0。\n"
                "请在 WebUI 中填写「Cookie 降级认证 - auth_token」"
                "和「Cookie 降级认证 - ct0 (CSRF Token)」。"
            )
        await self._ensure_client()
        headers = self._build_cookie_auth_headers()
        v1_url, v1_params, endpoint_type = self._translate_v2_to_v1_url(
            v2_url, params or {}
        )
        # 单条推文查询升级至 GraphQL TweetResultByRestId
        # Twitter v1.1 statuses/show.json 对 Cookie 鉴权已失效（官方 Web 客户端已全面迁移至 GraphQL）
        if endpoint_type == "tweet":
            tweet_id = v1_params.get("id", "")
            return await self._make_graphql_tweet_request(tweet_id, headers)
        logger.info(
            f"Cookie 降级认证 → v1.1 API | 端点类型: {endpoint_type} | URL: {v1_url}"
        )
        try:
            response = await self.client.request(
                method=method, url=v1_url, headers=headers, params=v1_params
            )
            if response.status_code >= 400:
                # 记录完整响应体供诊断，区分"端点失效"与"推文不存在"
                logger.warning(
                    f"Cookie v1.1 错误响应 [{response.status_code}] | "
                    f"URL: {v1_url} | Body: {response.text[:500]}"
                )
            if response.status_code == 401:
                raise ValueError(
                    "❌ Cookie 降级认证失败 (401)：auth_token 或 ct0 无效或已过期。\n"
                    "请重新从浏览器开发者工具获取最新 auth_token 和 ct0 后更新配置。"
                )
            elif response.status_code == 403:
                raise ValueError(
                    "❌ Cookie 降级认证失败 (403)：ct0 (CSRF Token) 与 auth_token "
                    "不匹配，或账户存在访问限制。"
                )
            elif response.status_code == 429:
                raise ValueError(
                    "⚠️ Cookie 降级认证触发速率限制 (429)：请求过于频繁，请稍后重试。"
                )
            elif response.status_code >= 400:
                raise ValueError(
                    f"❌ Cookie 降级认证请求失败 ({response.status_code})："
                    f"{response.text[:200]}"
                )
            v1_data = response.json() if response.text else {}
            adapted = self._adapt_v1_response(v1_data, endpoint_type)
            return {"data": adapted, "headers": dict(response.headers)}
        except httpx.TimeoutException:
            raise ValueError("❌ Cookie 降级请求超时，请检查网络或代理配置。")
        except httpx.ConnectError as e:
            raise ValueError(f"❌ Cookie 降级连接失败：{str(e)}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(
                f"❌ Cookie 降级请求异常：{type(e).__name__} - {str(e)}"
            )

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        use_cookie_fallback: bool = False,
        use_bearer_token: bool = False
    ) -> Dict[str, Any]:
        """
        执行 HTTP 请求的通用方法，包含三级认证降级与错误处理
        
        认证优先级：
        1. OAuth 1.0a 用户上下文（HMAC-SHA1 签名）—— 完整权限
        2. OAuth 2.0 Bearer Token —— 只读公开数据
        3. Cookie 降级 —— 最终降级方案
        
        Args:
            method (str): HTTP 方法 (GET, POST 等)
            url (str): API 端点 URL
            headers (dict, optional): 自定义请求头
            params (dict, optional): 查询参数
            json_data (dict, optional): JSON 请求体
            use_cookie_fallback (bool): 是否强制使用 Cookie 降级认证
            use_bearer_token (bool): 是否优先使用 Bearer Token 认证（部分端点如 trends 要求）
            
        Returns:
            dict: 包含 'data' 和 'headers' 的响应字典
            
        Raises:
            ValueError: API 错误或网络错误
            
        规范文档参考：
        - 身份验证矩阵与降级访问策略 (Line 81-94)
        - 全局网络代理与流量调度架构 (Line 252-280)
        """
        await self._ensure_client()
        
        # 构建请求头
        if headers is None:
            headers = {}
        
        # 根据认证方案设置请求头（三级降级策略）
        if use_cookie_fallback and self.cookie_available:
            # Cookie 降级：使用 v1.1 内部 API（v2 不接受 Cookie 认证）
            return await self._make_v1_cookie_request(
                method=method, v2_url=url, params=params, json_data=json_data
            )
        elif use_bearer_token and self.bearer_token:
            # 指定端点优先 Bearer Token（如 trends 端点，官方文档要求 Bearer Token 认证）
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.oauth1_available:
            # 第一级: OAuth 1.0a 用户上下文（HMAC-SHA1 签名）
            # 生成包含所有 oauth_* 参数的签名头部
            headers["Authorization"] = self._generate_oauth1_header(
                method=method,
                url=url,
                params=params
            )
        elif self.bearer_token:
            # 第二级: Bearer Token (App-Only)
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.cookie_available:
            # Cookie 降级（主认证模式）：路由到 v1.1 内部 API 通道
            return await self._make_v1_cookie_request(
                method=method, v2_url=url, params=params, json_data=json_data
            )
        else:
            raise ValueError(
                "❌ 认证失败：未配置任何有效凭据。\n"
                "请在 WebUI 中配置以下任一认证方式：\n"
                "1. OAuth 1.0a: Consumer Key/Secret + Access Token/Secret（推荐）\n"
                "2. Bearer Token（仅限公开数据只读）\n"
                "3. Cookie 降级认证（auth_token + ct0）"
            )
        
        headers["User-Agent"] = headers.get("User-Agent", "AstrBot-XAPI-Client/1.0")
        
        try:
            # 执行请求
            response = await self.client.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_data
            )
            
            # 提取响应数据和头部信息
            response_data = response.json() if response.text else {}
            response_headers = dict(response.headers)
            
            # 错误码处理
            if response.status_code == 401:
                auth_hint = (
                    "OAuth 1.0a 凭据（Consumer Key/Secret + Access Token/Secret）"
                    if self.oauth1_available else "Bearer Token"
                )
                raise ValueError(
                    f"❌ 认证失败：X API 返回 401 Unauthorized。\n"
                    f"当前认证方式: {auth_hint}\n"
                    f"请检查 WebUI 中的凭据配置是否正确。"
                )
            
            elif response.status_code == 403:
                # 403 Forbidden - 权限不足或访问受限
                # 降级逻辑: OAuth 1.0a -> Bearer Token -> Cookie
                if not use_cookie_fallback:
                    if self.oauth1_available and "oauth_consumer_key" in headers.get("Authorization", ""):
                        # 当前为 OAuth 1.0a，先尝试 Bearer Token
                        if self.bearer_token:
                            logger.warning("⚠️ OAuth 1.0a 返回 403，尝试降级至 Bearer Token...")
                            fallback_headers = {k: v for k, v in headers.items() if k != "Authorization"}
                            fallback_headers["Authorization"] = f"Bearer {self.bearer_token}"
                            try:
                                bt_response = await self.client.request(
                                    method=method,
                                    url=url,
                                    headers=fallback_headers,
                                    params=params,
                                    json=json_data
                                )
                                if bt_response.status_code < 400:
                                    return {
                                        "data": bt_response.json() if bt_response.text else {},
                                        "headers": dict(bt_response.headers)
                                    }
                            except Exception as e:
                                logger.debug(f"Bearer Token 降级尝试失败: {e}")
                    # 最终尝试 Cookie 降级（v1.1 内部 API 通道）
                    if self.cookie_available:
                        logger.warning("⚠️ API 返回 403 Forbidden，尝试 Cookie 降级认证（v1.1）...")
                        return await self._make_v1_cookie_request(
                            method=method,
                            v2_url=url,
                            params=params,
                            json_data=json_data,
                        )
                
                raise ValueError(
                    "❌ 权限被拒：API 返回 403 Forbidden。可能原因：\n"
                    "1. API Key 权限不足（建议升级到 Basic 或 Pro）\n"
                    "2. OAuth 1.0a 凭据无效或权限不足\n"
                    "3. 内容受限或账户限制\n"
                    "4. 需要提供有效的 Cookie 进行降级认证"
                )
            
            elif response.status_code == 429:
                # 429 Too Many Requests - 速率限制已触发
                reset_at = int(response_headers.get('x-rate-limit-reset', 0))
                if reset_at > 0:
                    reset_dt = datetime.fromtimestamp(reset_at).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    reset_dt = "未知（请稍后重试）"
                raise ValueError(
                    f"⚠️ 速率限制触发：已达 X API 15 分钟窗口限额。\n"
                    f"限额重置时间: {reset_dt}\n"
                    f"请稍后再试。"
                )
            
            elif response.status_code == 402:
                # 402 Payment Required - API 账户额度耗尽，尝试降级至 Cookie
                error_msg = response_data.get('detail', response_data.get('message', '未知错误'))
                if not use_cookie_fallback and self.cookie_available:
                    logger.warning("⚠️ API 返回 402（账户额度耗尽），尝试 Cookie 降级认证（v1.1）...")
                    return await self._make_v1_cookie_request(
                        method=method,
                        v2_url=url,
                        params=params,
                        json_data=json_data,
                    )
                raise ValueError(
                    f"❌ API 额度耗尽 (402)：{error_msg}\n"
                    f"当前账户无可用请求额度，且未配置 Cookie 降级认证。\n"
                    f"请在 WebUI 中配置 auth_token 和 ct0 以启用 Cookie 降级模式。"
                )

            elif 400 <= response.status_code < 500:
                # 其他 4xx 客户端错误
                error_msg = response_data.get('detail', response_data.get('message', '未知错误'))
                raise ValueError(f"❌ 客户端错误 ({response.status_code})：{error_msg}")
            
            elif response.status_code >= 500:
                # 5xx 服务器错误
                raise ValueError(
                    f"❌ 服务器错误 ({response.status_code})：X API 服务暂时不可用。请稍后重试。"
                )
            
            # 成功响应
            return {
                "data": response_data,
                "headers": response_headers
            }
        
        except httpx.TimeoutException:
            raise ValueError(
                "❌ 请求超时：连接到 X API 超过 10 秒。\n"
                "可能原因：\n"
                "1. 网络延迟过高\n"
                "2. 代理节点无响应（检查 http://127.0.0.1:7890）\n"
                "3. X API 服务响应缓慢"
            )
        
        except httpx.ConnectError as e:
            raise ValueError(
                f"❌ 连接错误：无法连接到 X API 或代理节点。\n"
                f"代理地址: {self.proxy_url if self.enable_proxy else '未配置'}\n"
                f"详细错误: {str(e)}"
            )
        
        except Exception as e:
            logger.error(f"HTTP 请求异常: {str(e)}", exc_info=True)
            raise ValueError(f"❌ 网络请求失败：{type(e).__name__} - {str(e)}")
    
    # ========================================================================
    # API 获取方法：搜索推文
    # ========================================================================
    
    async def search_recent(
        self,
        query: str,
        max_results: int = 100,
        pagination_token: Optional[str] = None,
        sort_order: str = "relevancy",
        expansions: str = "author_id,attachments.media_keys",
        tweet_fields: str = "created_at,public_metrics",
        media_fields: str = "url,variants,type",
        user_fields: str = "name,username,profile_image_url"
    ) -> SearchResponse:
        """
        搜索最新推文
        
        API 端点: GET /2/tweets/search/recent
        文档: https://docs.x.com/x-api/tweets/search/integrate/build-a-query
        
        规范文档参考：最新新闻与内容搜索 (Line 93-97)
        
        Args:
            query (str): 搜索查询（支持 Boolean 操作符）
            max_results (int): 返回结果数（默认 100，最大 100）
            sort_order (str): 排序方式，relevancy=按相关度/热度，recency=按时间倒序
            expansions (str): 扩展字段（接收 includes 容器中的关联数据）
            tweet_fields (str): 推文字段列表
            media_fields (str): 媒体字段列表
            user_fields (str): 用户字段列表
            
        Returns:
            SearchResponse: 包含推文列表、用户/媒体数据水合、分页元数据
            
        Raises:
            ValueError: API 错误或网络错误
        """
        url = "https://api.x.com/2/tweets/search/recent"
        params = {
            "query": query,
            "max_results": max(10, min(max_results, 100)),  # X API v2 要求 10-100
            "sort_order": sort_order if sort_order in ("relevancy", "recency") else "relevancy",
            "expansions": expansions,
            "tweet.fields": tweet_fields,
            "media.fields": media_fields,
            "user.fields": user_fields
        }
        if pagination_token:
            params["next_token"] = pagination_token
        
        response = await self._make_request("GET", url, params=params)
        
        # Pydantic 自动验证与反序列化
        search_response = SearchResponse(
            data=response["data"].get("data"),
            includes=response["data"].get("includes"),
            meta=response["data"].get("meta"),
            headers=response["headers"]
        )
        
        logger.info(f"搜索成功: {query} | 结果数: {len(search_response.data or [])}")
        return search_response
    
    # ========================================================================
    # API 获取方法：获取单条推文
    # ========================================================================
    
    async def get_tweet(
        self,
        tweet_id: str,
        expansions: str = "author_id,attachments.media_keys",
        tweet_fields: str = "created_at,public_metrics,entities",
        media_fields: str = "url,variants,type",
        user_fields: str = "name,username,profile_image_url"
    ) -> TweetResponse:
        """
        获取单条推文详情
        
        API 端点: GET /2/tweets/:id
        文档: https://docs.x.com/x-api/tweets/lookup/integrate/get-a-tweet
        
        规范文档参考：推文链接深度解析 (Line 99-102)
        
        Args:
            tweet_id (str): 推文 ID
            expansions (str): 扩展字段
            tweet_fields (str): 推文字段列表
            media_fields (str): 媒体字段列表
            user_fields (str): 用户字段列表
            
        Returns:
            TweetResponse: 包含推文对象、用户/媒体数据水合
            
        Raises:
            ValueError: API 错误或网络错误
            FileNotFoundError: 推文不存在（404）
        """
        url = f"https://api.x.com/2/tweets/{tweet_id}"
        params = {
            "expansions": expansions,
            "tweet.fields": tweet_fields,
            "media.fields": media_fields,
            "user.fields": user_fields
        }
        
        try:
            response = await self._make_request("GET", url, params=params)
        except ValueError as e:
            if "404" in str(e):
                raise FileNotFoundError(f"推文 {tweet_id} 不存在或已删除")
            raise
        
        tweet_response = TweetResponse(
            data=response["data"].get("data"),
            includes=response["data"].get("includes"),
            headers=response["headers"]
        )
        
        logger.info(f"获取推文成功: {tweet_id}")
        return tweet_response
    
    # ========================================================================
    # API 获取方法：获取用户ID
    # ========================================================================
    
    async def get_user_id_by_username(self, username: str) -> str:
        """
        通过用户名查询用户 ID
        
        API 端点: GET /2/users/by/username/:username
        文档: https://docs.x.com/x-api/users/lookup/integrate/get-a-user-by-username
        
        Args:
            username (str): 推特用户名（不含 @ 符号）
            
        Returns:
            str: 用户 ID
            
        Raises:
            ValueError: API 错误或网络错误
            FileNotFoundError: 用户不存在（404）
        """
        # 移除 @ 前缀（如果有）
        username = username.lstrip("@")
        
        # 验证 Twitter 用户名格式（仅允许字母、数字、下划线，长度 1-15）
        if not re.match(r'^[a-zA-Z0-9_]{1,15}$', username):
            raise ValueError(
                f"无效的推特用户名 '{username}'："
                f"Twitter 用户名仅允许字母、数字和下划线，长度 1-15 个字符。"
            )
        
        url = f"https://api.x.com/2/users/by/username/{username}"
        params = {
            "user.fields": "id,name,username"
        }
        
        try:
            response = await self._make_request("GET", url, params=params)
        except ValueError as e:
            if "404" in str(e):
                raise FileNotFoundError(f"用户 @{username} 不存在")
            raise
        
        user_response = UserLookupResponse(
            data=response["data"].get("data"),
            headers=response["headers"]
        )
        
        if not user_response.data or not user_response.data.id:
            raise FileNotFoundError(f"无法获取用户 @{username} 的 ID")
        
        logger.info(f"用户名查询成功: @{username} -> {user_response.data.id}")
        return user_response.data.id
    
    # ========================================================================
    # API 获取方法：获取用户时间线
    # ========================================================================
    
    async def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 100,
        pagination_token: Optional[str] = None,
        expansions: str = "attachments.media_keys",
        tweet_fields: str = "created_at,public_metrics",
        media_fields: str = "url,variants,type",
        user_fields: str = "name,username,profile_image_url"
    ) -> UserTimelineResponse:
        """
        获取用户推文时间线
        
        API 端点: GET /2/users/:id/tweets
        文档: https://docs.x.com/x-api/tweets/timelines/integrate/user-timeline-by-id
        
        规范文档参考：个人主页与时间线 (Line 104-107)
        
        Args:
            user_id (str): 用户 ID
            max_results (int): 返回结果数（默认 100，最大 100）
            expansions (str): 扩展字段
            tweet_fields (str): 推文字段列表
            media_fields (str): 媒体字段列表
            user_fields (str): 用户字段列表
            
        Returns:
            UserTimelineResponse: 包含推文列表、媒体数据水合、分页元数据
            
        Raises:
            ValueError: API 错误或网络错误
        """
        url = f"https://api.x.com/2/users/{user_id}/tweets"
        params = {
            "max_results": max(5, min(max_results, 100)),
            "expansions": expansions,
            "tweet.fields": tweet_fields,
            "media.fields": media_fields,
            "user.fields": user_fields
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        
        response = await self._make_request("GET", url, params=params)
        
        timeline_response = UserTimelineResponse(
            data=response["data"].get("data"),
            includes=response["data"].get("includes"),
            meta=response["data"].get("meta"),
            headers=response["headers"]
        )
        
        logger.info(f"时间线获取成功: 用户 {user_id} | 推文数: {len(timeline_response.data or [])}")
        return timeline_response
    
    # ========================================================================
    # API 获取方法：获取认证用户信息
    # ========================================================================
    
    async def get_authenticated_user_id(self) -> str:
        """
        获取当前 OAuth 1.0a 认证用户的 ID
        
        API 端点: GET /2/users/me
        文档: https://docs.x.com/x-api/users/lookup/api-reference/get-users-me
        
        Returns:
            str: 认证用户的 ID
            
        Raises:
            ValueError: API 错误或网络错误（需要 OAuth 1.0a 用户上下文）
        """
        url = "https://api.x.com/2/users/me"
        params = {"user.fields": "id,name,username"}
        
        response = await self._make_request("GET", url, params=params)
        
        user_data = response["data"].get("data")
        if not user_data or not user_data.get("id"):
            raise ValueError("无法获取当前认证用户信息，请检查 OAuth 1.0a 凭证配置")
        
        logger.info(f"认证用户查询成功: @{user_data.get('username', '?')} -> {user_data['id']}")
        return user_data["id"]
    
    # ========================================================================
    # API 获取方法：获取主页时间线（Following）
    # ========================================================================
    
    async def get_home_timeline(
        self,
        user_id: str,
        max_results: int = 100,
        pagination_token: Optional[str] = None,
        expansions: str = "author_id,attachments.media_keys",
        tweet_fields: str = "created_at,public_metrics",
        media_fields: str = "url,variants,type,preview_image_url",
        user_fields: str = "name,username,profile_image_url"
    ) -> UserTimelineResponse:
        """
        获取认证用户的主页时间线（Following / 关注者推文）
        
        API 端点: GET /2/users/:id/timelines/reverse_chronological
        文档: https://docs.x.com/x-api/tweets/timelines/api-reference/get-users-id-reverse-chronological-timeline
        
        认证方式：该端点要求 OAuth 1.0a 用户上下文认证
        
        Args:
            user_id (str): 认证用户的 ID
            max_results (int): 返回结果数（默认 100，最大 100）
            pagination_token (str): 分页令牌
            expansions (str): 扩展字段
            tweet_fields (str): 推文字段列表
            media_fields (str): 媒体字段列表
            user_fields (str): 用户字段列表
            
        Returns:
            UserTimelineResponse: 包含推文列表、媒体数据水合、分页元数据
            
        Raises:
            ValueError: API 错误或网络错误
        """
        url = f"https://api.x.com/2/users/{user_id}/timelines/reverse_chronological"
        params = {
            "max_results": max(1, min(max_results, 100)),
            "expansions": expansions,
            "tweet.fields": tweet_fields,
            "media.fields": media_fields,
            "user.fields": user_fields
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        
        response = await self._make_request("GET", url, params=params)
        
        timeline_response = UserTimelineResponse(
            data=response["data"].get("data"),
            includes=response["data"].get("includes"),
            meta=response["data"].get("meta"),
            headers=response["headers"]
        )
        
        logger.info(f"主页时间线获取成功: 用户 {user_id} | 推文数: {len(timeline_response.data or [])}")
        return timeline_response
    
    # ========================================================================
    # API 获取方法：获取趋势话题
    # ========================================================================
    
    async def get_trends(self, woeid: int = 1) -> TrendsResponse:
        """
        获取指定地理位置的热点趋势
        
        API 端点: GET /2/trends/by/woeid/:woeid
        文档: https://docs.x.com/x-api/trends/trends-by-woeid/introduction
        
        规范文档参考：热点趋势获取 (Line 109-112)
        
        认证方式：该端点要求 Bearer Token 认证（官方文档明确指定）
        
        Args:
            woeid (int): Yahoo Where On Earth ID（仅限预设地区）
                - 1: 全球
                - 23424856: 日本
                - 23424937: 香港
                - 23424971: 台湾
                - 23424977: 美国
                - 23424975: 英国
                - 23424781: 中国
                - 23424868: 韩国
                
        Returns:
            TrendsResponse: 包含趋势数据对象
            
        Raises:
            ValueError: API 错误或网络错误
            
        注意：该端点使用 Bearer Token 认证，如遇 403 则自动尝试 Cookie 降级
        规范文档参考：身份验证矩阵与降级访问策略 (Line 81-94)
        """
        url = f"https://api.x.com/2/trends/by/woeid/{woeid}"
        
        try:
            # 趋势端点官方文档要求 Bearer Token 认证
            response = await self._make_request("GET", url, use_bearer_token=True)
        except ValueError as e:
            if "403" in str(e):
                # Bearer Token 权限不足，尝试 Cookie 降级
                logger.warning(f"⚠️ 趋势端点返回 403，尝试 Cookie 降级...")
                try:
                    response = await self._make_request("GET", url, use_cookie_fallback=True)
                except Exception as e:
                    logger.debug(f"Cookie 降级认证失败: {e}")
                    raise ValueError(
                        f"❌ 趋势获取失败（API 返回 403）：\n"
                        f"可能原因：\n"
                        f"1. API Key 权限不足（趋势端点可能需要更高访问层级）\n"
                        f"2. 未配置有效的 Bearer Token\n"
                        f"3. 未配置 Cookie 降级认证"
                    ) from e
            else:
                raise
        
        trends_response = TrendsResponse(
            data=response["data"].get("data"),
            headers=response["headers"]
        )
        
        logger.info(f"趋势获取成功: WOEID {woeid} | 结果数: {len(trends_response.data or [])}")
        return trends_response
    
    # ========================================================================
    # 资源清理
    # ========================================================================
    
    async def close(self):
        """
        关闭异步 HTTP 客户端连接并释放资源
        
        规范要求：在插件卸载时必须调用此方法以正确关闭连接池
        规范文档参考：生命周期管理 (Line 558-569)
        """
        if self.client is not None:
            await self.client.aclose()
            self.client = None
            self.session_started = False
            logger.info("XApiClient 已关闭，连接池已释放")
