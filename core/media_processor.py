"""
媒体处理算法实现 - media_processor.py

规范文档参考：
- 媒体解析、异步并发与大文件拦截机制
- 多维度的变体解析与最优码率择优
- 大文件拦截与动态压缩流水线
- 异步网络基建与事件循环保护

参考实现：
- astrbot_plugin_pixiv_reborn: PIL 二分搜索压缩算法、asyncio.to_thread 线程池隔离
- astrbot_plugin_parser: 流式下载、双层体积守卫、HEAD 预检

核心职责：
1. 变体筛选 - 从视频/GIF 多变体中择优出最优 MP4 变体（排除 HLS 流媒体）
2. 大文件侦测熔断 - HEAD 请求预检体积，超限则熔断并生成降级文本
3. 流式下载 - httpx async 流式下载带双层体积守卫
4. PIL 压缩算法 - 二分搜索目标体积压缩，CPU 密集型任务委派线程池
5. 媒体信息提取 - 精简摘要用于 LLM 上下文防污染压缩
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import httpx
from PIL import Image as PILImage

from astrbot.api import logger
from ..models.x_response_models import (
    Media,
    MediaVariant,
)


class MediaProcessor:
    """
    媒体处理管线

    由 main.py 初始化并注入配置参数，承担推文媒体的全生命周期处理：
    变体筛选 → 大文件预检 → 流式下载 → 图片压缩 → 信息提取。

    所有网络 I/O 基于 httpx.AsyncClient 实现全异步，
    所有 PIL CPU 密集型任务通过 asyncio.to_thread() 委派线程池，
    确保 AstrBot 事件循环不被阻塞。
    """

    def __init__(
        self,
        forward_threshold_mb: int = 25,
        pil_compress_target_kb: int = 2048,
        display_media_details: bool = True,
        enable_proxy: bool = True,
        proxy_url: str | None = "http://127.0.0.1:7890",
    ):
        """
        初始化媒体处理器。

        Args:
            forward_threshold_mb: 转发阈值（MB），超过此体积的文件将被熔断拦截
            pil_compress_target_kb: PIL 压缩目标体积（KB），0 表示不限制
            display_media_details: 是否在返回信息中附带媒体详情（类型/大小/链接）
            enable_proxy: 是否启用代理（用于媒体下载）
            proxy_url: 代理地址（如 http://127.0.0.1:7890）
        """
        self.forward_threshold_mb = forward_threshold_mb
        self.forward_threshold_bytes = forward_threshold_mb * 1024 * 1024
        self.pil_compress_target_kb = pil_compress_target_kb
        self.pil_compress_target_bytes = pil_compress_target_kb * 1024
        self.display_media_details = display_media_details
        self.enable_proxy = enable_proxy
        self.proxy_url = proxy_url

        # HTTP 客户端延迟初始化（规范要求严禁在 __init__ 执行网络操作）
        self._client: httpx.AsyncClient | None = None

        logger.info(
            f"MediaProcessor 初始化 | "
            f"转发阈值: {forward_threshold_mb}MB | "
            f"PIL 压缩目标: {pil_compress_target_kb}KB | "
            f"媒体详情: {'开启' if display_media_details else '关闭'}"
        )

    # ========================================================================
    # 生命周期管理
    # ========================================================================

    async def _ensure_client(self) -> httpx.AsyncClient:
        """
        延迟初始化 httpx 异步客户端。

        规范要求：严禁在 __init__ 中执行网络操作，
        必须延后至事件循环启动后初始化。
        """
        if self._client is None:
            proxy = self.proxy_url if self.enable_proxy and self.proxy_url else None
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                proxy=proxy,
                verify=True,
                http2=True,
                follow_redirects=True,
            )
        return self._client

    async def close(self):
        """关闭 HTTP 客户端，释放连接池资源。"""
        if self._client:
            await self._client.aclose()
            self._client = None

    # ========================================================================
    # 1. 变体筛选（多维度的变体解析与最优码率择优）
    # ========================================================================

    def select_best_variant(
        self,
        variants: list[MediaVariant],
    ) -> dict[str, Any] | None:
        """
        从推特视频/GIF 的多变体列表中择优选出最佳 MP4 变体。

        规范参考：多维度的变体解析与最优码率择优
        - 首先过滤出 content_type == video/mp4 的变体（排除 HLS application/x-mpegURL）
        - 按 bit_rate 降序排列
        - 选择比特率最高且 URL 可用的变体

        Args:
            variants: MediaVariant 对象列表

        Returns:
            包含 url、bit_rate、content_type 的字典，或 None
        """
        if not variants:
            return None

        # Step 1: 过滤 MP4 变体（排除 HLS 流媒体格式）
        mp4_variants = [
            v for v in variants
            if v.content_type
            and v.content_type.lower() == "video/mp4"
            and v.url
        ]

        if not mp4_variants:
            # 无 MP4 变体，尝试回退到任意有 URL 的变体
            for v in variants:
                if v.url:
                    return {
                        "url": v.url,
                        "bit_rate": v.bit_rate,
                        "content_type": v.content_type,
                    }
            return None

        # Step 2: 按比特率降序排列（None 视为 0）
        mp4_variants.sort(key=lambda v: v.bit_rate or 0, reverse=True)

        # Step 3: 返回比特率最高的 MP4 变体
        best = mp4_variants[0]
        return {
            "url": best.url,
            "bit_rate": best.bit_rate,
            "content_type": best.content_type,
        }

    # ========================================================================
    # 2. 大文件侦测熔断（HEAD 请求预检）
    # ========================================================================

    async def check_file_size(self, url: str) -> int | None:
        """
        通过异步 HEAD 请求获取目标文件的 Content-Length。

        规范参考：大文件拦截与动态压缩流水线
        - 在发起完整的媒体下载前，httpx 发起一次异步 HEAD 请求获取 Content-Length
        - 如果文件体积超过异常大小阈值，下载将被立即熔断

        Args:
            url: 媒体直链地址

        Returns:
            文件体积（字节），无法获取时返回 None
        """
        try:
            client = await self._ensure_client()
            response = await client.head(url)
            content_length = response.headers.get("content-length")
            if content_length:
                return int(content_length)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(f"HEAD 请求失败: {url} - {type(e).__name__}")
        except Exception as e:
            logger.error(f"HEAD 请求异常: {url} - {e}")
        return None

    async def should_intercept(self, url: str) -> tuple[bool, int | None]:
        """
        判断媒体文件是否应被熔断拦截。

        Returns:
            (should_intercept, file_size_bytes)
            - True: 文件超过阈值，应拦截
            - False: 文件在阈值范围内，可继续下载
        """
        file_size = await self.check_file_size(url)
        if file_size is not None and file_size > self.forward_threshold_bytes:
            logger.warning(
                f"大文件熔断: {file_size / 1024 / 1024:.2f}MB 超过阈值 "
                f"{self.forward_threshold_mb}MB | URL: {url}"
            )
            return True, file_size
        return False, file_size

    def build_fallback_text(
        self, media_type: str, url: str, file_size: int | None = None
    ) -> str:
        """
        当大文件被拦截时，生成降级展示文本（附直链地址）。

        规范参考：此时插件不会让智能体回复失败，而是动态生成一段提示文本，
        附带媒体的原始直链地址发送至群聊，实现优雅的降级展示。

        Args:
            media_type: 媒体类型 (photo/video/animated_gif)
            url: 媒体直链地址
            file_size: 文件体积（字节）
        """
        size_str = f"{file_size / 1024 / 1024:.1f}MB" if file_size else "未知大小"
        type_label = {
            "photo": "图片", "video": "视频", "animated_gif": "GIF 动图"
        }.get(media_type, media_type)

        return (
            f"⚠️ {type_label}文件过大（{size_str}），"
            f"超过转发阈值 {self.forward_threshold_mb}MB，已跳过下载。\n"
            f"🔗 原始直链: {url}"
        )

    # ========================================================================
    # 3. 异步流式下载（带双层体积守卫）
    # ========================================================================

    async def download_media(self, url: str) -> bytes | None:
        """
        异步流式下载媒体内容，带双层体积守卫。

        参考 astrbot_plugin_parser Downloader.streamd():
        - 第一层: 预检 Content-Length（GET 响应头），超限立即终止
        - 第二层: 流式下载中实时累计检查，防止 Content-Length 缺失导致内存溢出

        Args:
            url: 媒体直链地址

        Returns:
            下载的字节数据，失败或超限返回 None
        """
        try:
            client = await self._ensure_client()

            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    logger.warning(
                        f"下载失败: HTTP {response.status_code} | {url}"
                    )
                    return None

                # 第一层预检: 从 GET 响应头获取 Content-Length
                content_length_str = response.headers.get("content-length")
                if content_length_str:
                    try:
                        content_length = int(content_length_str)
                    except ValueError:
                        logger.warning(f"Content-Length 解析失败（非数字值: {content_length_str!r}），跳过预检: {url}")
                        content_length = None
                if content_length_str and content_length is not None:
                    if content_length == 0:
                        logger.warning(f"下载内容为空（Content-Length=0）: {url}")
                        return None
                    if content_length > self.forward_threshold_bytes:
                        logger.warning(
                            f"下载熔断（响应头预检）: "
                            f"{content_length / 1024 / 1024:.2f}MB > "
                            f"{self.forward_threshold_mb}MB"
                        )
                        return None

                # 第二层: 流式下载 + 实时累计体积检查
                chunks = []
                downloaded = 0
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > self.forward_threshold_bytes:
                        logger.warning(
                            f"下载熔断（实时累计）: "
                            f"{downloaded / 1024 / 1024:.2f}MB > "
                            f"{self.forward_threshold_mb}MB"
                        )
                        return None
                    chunks.append(chunk)

                if downloaded == 0:
                    logger.warning(f"下载内容为空: {url}")
                    return None

                data = b"".join(chunks)
                logger.info(f"媒体下载完成: {downloaded / 1024:.1f}KB | {url}")
                return data

        except httpx.TimeoutException:
            logger.error(f"下载超时: {url}")
            return None
        except httpx.ConnectError as e:
            logger.error(f"下载连接失败（检查代理配置）: {url} - {e}")
            return None
        except Exception as e:
            logger.error(f"下载异常: {url} - {type(e).__name__}: {e}")
            return None

    # ========================================================================
    # 4. PIL 压缩算法（大文件压缩流水线）
    # ========================================================================

    @staticmethod
    def _jpeg_ready_image(img: PILImage.Image) -> PILImage.Image:
        """
        将图片转换为 JPEG 兼容模式。
        处理 RGBA/LA 透明通道 → RGB 白色背景，以及调色板模式转换。
        参考 astrbot_plugin_pixiv_reborn _jpeg_ready_image()。
        """
        if img.mode in ("RGBA", "LA"):
            background = PILImage.new("RGB", img.size, (255, 255, 255))
            alpha = img.split()[-1]
            background.paste(img.convert("RGBA"), mask=alpha)
            return background
        if img.mode == "P":
            return img.convert("RGB")
        if img.mode != "RGB":
            return img.convert("RGB")
        return img

    @staticmethod
    def _save_with_quality(
        img: PILImage.Image, fmt: str, quality: int
    ) -> bytes:
        """
        按指定质量保存图片到内存字节。

        参考 astrbot_plugin_pixiv_reborn _save_with_quality():
        - JPEG: 使用 quality + optimize + progressive
        - WEBP: 使用 quality + method=6
        - PNG: 使用调色板量化（colors）近似质量控制

        Args:
            img: PIL 图片对象
            fmt: 格式字符串 (JPEG/PNG/WEBP)
            quality: 质量参数 (1-100)

        Returns:
            压缩后的图片字节
        """
        quality = max(1, min(100, int(quality)))
        fmt = (fmt or "").upper()

        with io.BytesIO() as buf:
            if fmt in ("JPEG", "JPG"):
                jpeg_img = MediaProcessor._jpeg_ready_image(img)
                jpeg_img.save(
                    buf,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
            elif fmt == "WEBP":
                img.save(buf, format="WEBP", quality=quality, method=6)
            elif fmt == "PNG":
                if quality < 100:
                    colors = max(16, int(256 * quality / 100))
                    png_img = img
                    if png_img.mode not in ("RGB", "RGBA", "P", "L"):
                        png_img = png_img.convert("RGBA")
                    png_img = png_img.convert("RGBA").quantize(colors=colors)
                    png_img.save(buf, format="PNG", optimize=True)
                else:
                    img.save(buf, format="PNG", optimize=True, compress_level=9)
            else:
                # 未知格式回退到 JPEG
                jpeg_img = MediaProcessor._jpeg_ready_image(img)
                jpeg_img.save(
                    buf,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
            return buf.getvalue()

    @staticmethod
    def _compress_image_sync(
        img_data: bytes,
        target_kb: int = 0,
        quality: int = 85,
    ) -> bytes:
        """
        同步压缩图片字节（设计为在线程池中执行）。

        压缩策略（参考 astrbot_plugin_pixiv_reborn _compress_image_with_pil_sync）：
        - 优先级 1: target_kb > 0 时，使用二分搜索找到满足目标体积的最高质量
        - 优先级 2: 按固定 quality 百分比压缩
        - GIF 动图直接跳过（保持完整帧序列）

        Args:
            img_data: 原始图片字节
            target_kb: 目标体积（KB），0 表示仅按 quality 压缩
            quality: 压缩质量上限（1-100），默认 85

        Returns:
            压缩后的字节数据（若压缩后更大则返回原数据）
        """
        try:
            with io.BytesIO(img_data) as input_buf:
                with PILImage.open(input_buf) as img:
                    src_fmt = (img.format or "").upper()

                    # GIF 动图不压缩（保持帧序列完整性）
                    if src_fmt == "GIF":
                        return img_data

                    quality = max(1, min(100, int(quality)))
                    target_kb = max(0, int(target_kb))

                    # === 优先级 1: 按目标体积二分搜索压缩 ===
                    if target_kb > 0:
                        target_bytes = target_kb * 1024

                        # 已满足目标体积则直接跳过
                        if len(img_data) <= target_bytes:
                            return img_data

                        if src_fmt in ("JPEG", "JPG", "WEBP", ""):
                            # 二分搜索: 在 [10, quality] 区间找满足目标的最高质量
                            low, high = 10, quality
                            best = None
                            while low <= high:
                                mid = (low + high) // 2
                                candidate = MediaProcessor._save_with_quality(
                                    img, src_fmt or "JPEG", mid
                                )
                                if len(candidate) <= target_bytes:
                                    best = candidate
                                    low = mid + 1  # 尝试更高质量
                                else:
                                    high = mid - 1  # 降低质量
                            if best:
                                return best

                            # 最低质量兜底
                            fallback = MediaProcessor._save_with_quality(
                                img, src_fmt or "JPEG", 10
                            )
                            return (
                                fallback
                                if len(fallback) < len(img_data)
                                else img_data
                            )

                        # PNG 等格式: 逐步降低质量近似值
                        for q in [100, 90, 80, 70, 60, 50, 40, 30, 20]:
                            q = min(q, quality)
                            candidate = MediaProcessor._save_with_quality(
                                img, src_fmt, q
                            )
                            if len(candidate) <= target_bytes:
                                return candidate

                        fallback = MediaProcessor._save_with_quality(
                            img, src_fmt, max(20, quality // 2)
                        )
                        return (
                            fallback
                            if len(fallback) < len(img_data)
                            else img_data
                        )

                    # === 优先级 2: 按固定质量压缩 ===
                    if quality >= 100:
                        return img_data

                    candidate = MediaProcessor._save_with_quality(
                        img, src_fmt or "JPEG", quality
                    )
                    return (
                        candidate if len(candidate) < len(img_data) else img_data
                    )
        except Exception as e:
            logger.warning(f"PIL 压缩失败，返回原图: {e}")
            return img_data

    async def compress_image(self, img_data: bytes) -> bytes:
        """
        异步图片压缩入口。

        规范要求：所有涉及 PIL 的 CPU 密集型任务，必须通过 asyncio.to_thread()
        委派给独立的线程池执行，保证事件循环不被阻塞。
        参考 astrbot_plugin_pixiv_reborn _maybe_compress_image_with_pil()。

        Args:
            img_data: 原始图片字节

        Returns:
            压缩后的图片字节（失败时回退原图）
        """
        if not img_data:
            return img_data

        try:
            compressed = await asyncio.to_thread(
                self._compress_image_sync,
                img_data,
                target_kb=self.pil_compress_target_kb,
            )

            if len(compressed) < len(img_data):
                saved_pct = (1 - len(compressed) / len(img_data)) * 100
                logger.info(
                    f"PIL 压缩生效: "
                    f"{len(img_data) // 1024}KB → {len(compressed) // 1024}KB "
                    f"(节省 {saved_pct:.1f}%)"
                )

            return compressed
        except Exception as e:
            logger.warning(f"PIL 压缩异常，返回原图: {e}")
            return img_data

    # ========================================================================
    # 5. 缩略图生成（搜索列表预览用）
    # ========================================================================

    @staticmethod
    def _generate_thumbnail_sync(
        img_data: bytes,
        max_width: int = 200,
        quality: int = 40,
    ) -> bytes:
        """
        同步生成低分辨率缩略图（设计为在线程池中执行）。

        按 max_width 等比缩放后以低质量 JPEG 导出，
        用于搜索列表中的媒体预览，体积通常 < 30KB。

        Args:
            img_data: 原始图片字节
            max_width: 缩略图最大宽度（像素），默认 200
            quality: JPEG 导出质量（1-100），默认 40

        Returns:
            缩略图 JPEG 字节
        """
        try:
            with io.BytesIO(img_data) as buf:
                with PILImage.open(buf) as img:
                    # 等比缩放：仅在原图宽度超过 max_width 时缩放
                    if img.width > max_width:
                        ratio = max_width / img.width
                        new_size = (max_width, int(img.height * ratio))
                        img = img.resize(new_size, PILImage.Resampling.LANCZOS)

                    # 转为 JPEG 兼容模式并导出
                    jpeg_img = MediaProcessor._jpeg_ready_image(img)
                    with io.BytesIO() as out:
                        jpeg_img.save(
                            out, format="JPEG", quality=quality, optimize=True
                        )
                        return out.getvalue()
        except Exception as e:
            logger.warning(f"缩略图生成失败，返回原图: {e}")
            return img_data

    async def generate_thumbnail(self, img_data: bytes) -> bytes:
        """
        异步缩略图生成入口。

        通过 asyncio.to_thread() 委派线程池，保护事件循环。

        Args:
            img_data: 原始图片字节

        Returns:
            缩略图 JPEG 字节
        """
        if not img_data:
            return img_data
        return await asyncio.to_thread(self._generate_thumbnail_sync, img_data)

    # ========================================================================
    # 6. 完整媒体处理管线
    # ========================================================================

    async def process_media_list(
        self, media_list: list[Media]
    ) -> list[dict[str, Any]]:
        """
        处理推文中的所有媒体项，返回处理结果列表。

        每个结果包含:
        - type: 媒体类型 (photo/video/animated_gif)
        - media_key: 媒体唯一标识
        - url: 最终可访问的直链
        - data: 下载的字节数据（如果进行了下载）
        - intercepted: 是否被大文件拦截
        - fallback_text: 拦截/失败时的降级文本
        - details: 媒体详情信息字符串

        Args:
            media_list: Media 对象列表

        Returns:
            处理结果字典列表
        """
        results = []
        for media in media_list:
            result = await self.process_single_media(media)
            results.append(result)
        return results

    async def process_single_media(self, media: Media) -> dict[str, Any]:
        """
        处理单个媒体项，按类型分发处理流程。

        处理流程:
        - photo: 变体/URL选择 → HEAD 预检 → 下载 → PIL 压缩 → 返回
        - video: 变体择优(MP4) → HEAD 预检 → 大文件熔断/下载 → 返回
        - animated_gif: 变体择优(MP4) → HEAD 预检 → 下载 → 返回（不压缩）

        Args:
            media: Media 对象

        Returns:
            处理结果字典
        """
        result: dict[str, Any] = {
            "type": media.type,
            "media_key": media.media_key,
            "url": media.url,
            "data": None,
            "intercepted": False,
            "fallback_text": None,
            "details": None,
        }

        try:
            if media.type == "photo":
                result = await self._process_photo(media, result)
            elif media.type == "video":
                result = await self._process_video(media, result)
            elif media.type == "animated_gif":
                result = await self._process_gif(media, result)
            else:
                logger.warning(f"未知媒体类型: {media.type}")
        except Exception as e:
            logger.error(f"媒体处理异常 [{media.type}]: {e}")
            result["fallback_text"] = f"⚠️ 媒体处理失败: {type(e).__name__}"

        # 附加详情信息
        if self.display_media_details:
            result["details"] = self._build_media_details(media, result)

        return result

    async def _process_photo(self, media: Media, result: dict) -> dict:
        """
        处理图片媒体。
        流程: URL 获取 → HEAD 预检 → 异步下载 → PIL 压缩。
        """
        url = media.url
        if not url:
            result["fallback_text"] = "⚠️ 图片 URL 不可用"
            return result

        # HEAD 预检体积
        intercepted, file_size = await self.should_intercept(url)
        if intercepted:
            result["intercepted"] = True
            result["fallback_text"] = self.build_fallback_text(
                media.type, url, file_size
            )
            return result

        # 异步流式下载
        img_data = await self.download_media(url)
        if not img_data:
            result["fallback_text"] = f"⚠️ 图片下载失败\n🔗 直链: {url}"
            return result

        # PIL 压缩（线程池隔离，不阻塞事件循环）
        if self.pil_compress_target_kb > 0:
            img_data = await self.compress_image(img_data)

        result["data"] = img_data
        result["url"] = url
        return result

    async def _process_video(self, media: Media, result: dict) -> dict:
        """
        处理视频媒体。
        流程: 多变体择优(MP4) → HEAD 预检 → 大文件熔断/下载。
        """
        url = media.url

        # 变体择优：选择比特率最高的 MP4 变体
        if media.variants:
            best = self.select_best_variant(media.variants)
            if best and best.get("url"):
                url = best["url"]
                result["url"] = url

        if not url:
            result["fallback_text"] = "⚠️ 视频 URL 不可用"
            return result

        # HEAD 预检体积
        intercepted, file_size = await self.should_intercept(url)
        if intercepted:
            result["intercepted"] = True
            result["fallback_text"] = self.build_fallback_text(
                media.type, url, file_size
            )
            return result

        # 异步流式下载
        video_data = await self.download_media(url)
        if not video_data:
            result["fallback_text"] = f"⚠️ 视频下载失败\n🔗 直链: {url}"
            return result

        result["data"] = video_data
        return result

    async def _process_gif(self, media: Media, result: dict) -> dict:
        """
        处理 GIF 动图媒体。
        流程: 变体择优(MP4) → HEAD 预检 → 下载（GIF 不执行 PIL 压缩）。
        """
        url = media.url

        # 变体择优
        if media.variants:
            best = self.select_best_variant(media.variants)
            if best and best.get("url"):
                url = best["url"]
                result["url"] = url

        if not url:
            result["fallback_text"] = "⚠️ GIF URL 不可用"
            return result

        # HEAD 预检体积
        intercepted, file_size = await self.should_intercept(url)
        if intercepted:
            result["intercepted"] = True
            result["fallback_text"] = self.build_fallback_text(
                media.type, url, file_size
            )
            return result

        # 异步流式下载（GIF 不压缩，保持帧序列完整性）
        gif_data = await self.download_media(url)
        if not gif_data:
            result["fallback_text"] = f"⚠️ GIF 下载失败\n🔗 直链: {url}"
            return result

        result["data"] = gif_data
        return result

    # ========================================================================
    # 6. 媒体信息提取（用于 LLM 上下文防污染压缩）
    # ========================================================================

    def _build_media_details(self, media: Media, result: dict) -> str:
        """
        构建单个媒体的详情信息字符串。

        Args:
            media: 原始 Media 对象
            result: 处理结果字典

        Returns:
            格式化的详情字符串
        """
        type_labels = {
            "photo": "图片",
            "video": "视频",
            "animated_gif": "GIF 动图",
        }
        parts = [f"📎 类型: {type_labels.get(media.type, media.type)}"]

        if result.get("data"):
            size_kb = len(result["data"]) / 1024
            if size_kb >= 1024:
                parts.append(f"📦 大小: {size_kb / 1024:.1f}MB")
            else:
                parts.append(f"📦 大小: {size_kb:.0f}KB")

        if result.get("url"):
            parts.append(f"🔗 链接: {result['url']}")

        if result.get("intercepted"):
            parts.append("🚫 已拦截（超过转发阈值）")

        return " | ".join(parts)

    def extract_media_summary(self, media_list: list[Media]) -> str:
        """
        提取媒体列表的精简摘要信息，用于 LLM 上下文防污染压缩。

        规范参考：执行上下文管理与防污染压缩
        - 仅提取智能体构建回复所需的硬核语义信息
        - 包含多媒体类型和直接访问链接

        Args:
            media_list: Media 对象列表

        Returns:
            多行摘要字符串，每行对应一个媒体项
        """
        if not media_list:
            return ""

        type_emojis = {
            "photo": "🖼️",
            "video": "🎬",
            "animated_gif": "🎞️",
        }

        summaries = []
        for media in media_list:
            emoji = type_emojis.get(media.type, "📎")
            url = media.url or ""

            # 对视频/GIF 进行变体择优提取最优 URL
            if media.type in ("video", "animated_gif") and media.variants:
                best = self.select_best_variant(media.variants)
                if best and best.get("url"):
                    url = best["url"]

            summary = f"{emoji} [{media.type}]"
            if url:
                summary += f" {url}"
            summaries.append(summary)

        return "\n".join(summaries)
