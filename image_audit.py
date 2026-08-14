# -*- coding: utf-8 -*-
"""图片 OCR、二维码解码及受控下载。"""

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


LLM_CALL_TIMEOUT = 60.0
OCR_QUEUE_TIMEOUT = 300.0
IMAGE_QUEUE_TIMEOUT = 120.0
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT = 10.0
IMAGE_DNS_TIMEOUT = 5.0
IMAGE_QR_DECODE_TIMEOUT = 10.0
IMAGE_AUDIT_TOTAL_TIMEOUT = 600.0
IMAGE_MAX_REDIRECTS = 3
IMAGE_EVIDENCE_CACHE_TTL_SECONDS = 900
IMAGE_EVIDENCE_CACHE_MAX_ENTRIES = 4096
IMAGE_EVIDENCE_MAX_CHARS = 1000
IMAGE_BRANCH_EVIDENCE_MAX_CHARS = 20_000
IMAGE_COMBINED_AUDIT_MAX_CHARS = 92_000
IMAGE_WORKER_CONCURRENCY = 4

_QR_DECODER = None      # "cv2" | "pyzbar" | None
_QR_PROBED = False


def _probe_qr_decoder():
    """探测可用的二维码解码库，结果缓存。优先 OpenCV，其次 pyzbar。"""
    global _QR_DECODER, _QR_PROBED
    if _QR_PROBED:
        return _QR_DECODER
    _QR_PROBED = True
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        _QR_DECODER = "cv2"
        return _QR_DECODER
    except Exception:
        pass
    try:
        from PIL import Image  # noqa: F401
        from pyzbar import pyzbar  # noqa: F401
        _QR_DECODER = "pyzbar"
        return _QR_DECODER
    except Exception:
        pass
    _QR_DECODER = None
    return None


def _decode_qr_from_bytes(data: bytes, decoder: str) -> list:
    """从图片字节解码二维码。该函数在线程池中执行。"""
    try:
        if decoder == "cv2":
            import cv2
            import numpy as np

            array = np.frombuffer(data, np.uint8)
            image = cv2.imdecode(array, cv2.IMREAD_COLOR)
            if image is None:
                return []
            detector = cv2.QRCodeDetector()
            try:
                ok, decoded, _points, _ = detector.detectAndDecodeMulti(image)
                if ok and decoded:
                    return [item for item in decoded if item]
            except Exception:
                pass
            decoded, _points, _ = detector.detectAndDecode(image)
            return [decoded] if decoded else []

        import io

        from PIL import Image
        from pyzbar import pyzbar

        image = Image.open(io.BytesIO(data))
        return [
            item.data.decode("utf-8", "ignore")
            for item in pyzbar.decode(image)
            if item.data
        ]
    except Exception:
        return []


class ImageAuditMixin:
    """图片审核能力，由 ``ModerationMixin`` 组合使用。"""

    _OCR_PROMPT_TEMPLATES = {
        "default": {
            "system": "你是一个图片内容识别助手。请仔细观察图片，用文字详细描述图片中的所有内容。如果图片中有文字，请完整转录所有文字内容。如果图片中包含二维码，请务必明确指出「图片包含二维码」并描述二维码周围的文字（如扫码进群、扫码加好友等引导语）。如果图片是广告、推广、违规内容，请特别说明。只输出图片内容描述，不要输出其他内容。",
            "prompt": "请识别并描述这张图片的内容，特别注意图片中的文字和是否包含二维码。",
        },
        "strict": {
            "system": "你是一个严格的内容审核图片识别助手。你的任务是识别图片中是否存在违规内容。请仔细检查：1.图片中是否有广告推广信息（联系方式、二维码、引流链接）——发现二维码时必须明确输出「图片包含二维码」并说明用途 2.是否有色情或低俗内容 3.是否有政治敏感内容 4.是否有暴恐或违法信息 5.是否有赌博或诈骗信息。如果图片中有文字，请完整转录。最后给出明确结论：该图片是否包含违规内容。",
            "prompt": "请严格审核这张图片，识别并描述所有可能违规的内容（尤其注意二维码），完整转录图片中的文字。",
        },
        "text_only": {
            "system": "你是一个OCR文字识别助手。请将图片中的所有文字完整转录出来，保持原始格式和排版。如果图片中没有文字，请回复「图片中无文字」。只输出识别到的文字内容，不要添加任何分析或评论。",
            "prompt": "请将这张图片中的所有文字完整转录出来。",
        },
    }

    def _init_image_audit_resources(self, llm_concurrency: int) -> None:
        """初始化插件实例级并发闸门和可复用 HTTP 会话槽位。"""
        concurrency = max(1, int(llm_concurrency))
        self._ocr_semaphore = asyncio.Semaphore(min(4, concurrency))
        self._image_io_semaphore = asyncio.Semaphore(min(8, max(2, concurrency)))
        self._qr_decode_semaphore = asyncio.Semaphore(min(4, concurrency))
        self._image_http_session = None
        self._image_http_session_lock = asyncio.Lock()
        self._image_audit_closing = False
        self._image_audit_tasks = set()
        self._image_evidence_cache = {}

    async def _close_image_audit_resources(self) -> None:
        """取消在途图片分支并关闭 HTTP 会话；重复调用是安全的。"""
        lock = getattr(self, "_image_http_session_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._image_http_session_lock = lock
        async with lock:
            self._image_audit_closing = True
            current_task = asyncio.current_task()
            tasks = [
                task for task in getattr(self, "_image_audit_tasks", set())
                if task is not current_task and not task.done()
            ]
            session = getattr(self, "_image_http_session", None)
            self._image_http_session = None
            cache = getattr(self, "_image_evidence_cache", None)
            if isinstance(cache, dict):
                cache.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if session is None or getattr(session, "closed", False):
            return
        try:
            await session.close()
        except Exception as exc:
            logger.debug(f"[GroupMgr] 关闭图片下载会话失败: {exc}")

    @staticmethod
    def _is_gif_url(url: str) -> bool:
        if not url:
            return False
        lower = url.lower()
        return lower.endswith(".gif") or ".gif?" in lower or ".gif;" in lower

    @staticmethod
    def _is_sticker_image(url: str) -> bool:
        if not url:
            return False
        lower = url.lower()
        markers = ("sticker", "emoji", "marketface", "emoticon")
        return (
            any(marker in lower for marker in markers)
            or "/face/" in lower
            or "/face?" in lower
            or "&face=" in lower
            or "?face=" in lower
        )

    @staticmethod
    def _select_image_urls(image_urls: list, limit: int = None) -> list:
        """稳定去重；可选 limit 仅供显式受限调用方使用。"""
        selected = []
        seen = set()
        for url in image_urls or []:
            if not url or url in seen:
                continue
            seen.add(url)
            selected.append(url)
            if limit is not None and len(selected) >= max(0, int(limit)):
                break
        return selected

    @staticmethod
    async def _map_image_work(items: list, worker, concurrency: int = 4) -> list:
        """Process arbitrary image counts with a fixed number of worker tasks."""
        items = list(items or [])
        if not items:
            return []
        results = [None] * len(items)
        next_index = 0

        async def run_worker() -> None:
            nonlocal next_index
            while next_index < len(items):
                index = next_index
                next_index += 1
                results[index] = await worker(items[index])

        worker_count = min(len(items), max(1, int(concurrency)))
        await asyncio.gather(*(run_worker() for _ in range(worker_count)))
        return results

    def _create_image_audit_task(self, coroutine):
        """Track branch tasks so plugin unload can cancel queued image work."""
        task = asyncio.create_task(coroutine)
        tasks = getattr(self, "_image_audit_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return task

    def _format_image_evidence(self, results: list, kind: str) -> str:
        """Give every successful image a fair share of the prompt budget."""
        entries = []
        for index, result in enumerate(results or [], start=1):
            result = str(result or "").strip()
            if result:
                entries.append((f"[图片{index}{kind}] ", result))
        if not entries:
            return ""

        overhead = sum(len(label) for label, _ in entries) + len(entries) - 1
        text_budget = max(0, IMAGE_BRANCH_EVIDENCE_MAX_CHARS - overhead)
        per_item, remainder = divmod(text_budget, len(entries))
        lines = []
        for index, (label, result) in enumerate(entries):
            item_budget = per_item + (1 if index < remainder else 0)
            lines.append(label + self._bounded_audit_text(result, item_budget))
        return "\n".join(lines)[:IMAGE_BRANCH_EVIDENCE_MAX_CHARS]

    def _cache_image_evidence(self, url: str, kind: str, text: str) -> None:
        """Cache bounded OCR/QR evidence for later group-history prompts."""
        url = str(url or "").strip()
        text = str(text or "").strip()
        if not url or not text or kind not in {"ocr", "qr"}:
            return
        cache = getattr(self, "_image_evidence_cache", None)
        if cache is None:
            cache = {}
            self._image_evidence_cache = cache
        now = time.monotonic()
        for key in [
            key for key, value in cache.items()
            if float(value.get("expires_at", 0.0) or 0.0) <= now
        ]:
            cache.pop(key, None)
        entry = cache.pop(url, None) or {}
        entry[kind] = self._bounded_audit_text(
            text, IMAGE_EVIDENCE_MAX_CHARS
        )
        entry["expires_at"] = now + IMAGE_EVIDENCE_CACHE_TTL_SECONDS
        cache[url] = entry
        while len(cache) > IMAGE_EVIDENCE_CACHE_MAX_ENTRIES:
            cache.pop(next(iter(cache)))

    def _cached_image_evidence(self, image_urls: list) -> str:
        """Return ordered cached evidence without repeating visual model calls."""
        cache = getattr(self, "_image_evidence_cache", None) or {}
        now = time.monotonic()
        lines = []
        for index, url in enumerate(self._select_image_urls(image_urls), start=1):
            entry = cache.get(url)
            if not entry:
                continue
            if float(entry.get("expires_at", 0.0) or 0.0) <= now:
                cache.pop(url, None)
                continue
            if entry.get("qr"):
                lines.append(f"[历史图片{index}二维码] {entry['qr']}")
            if entry.get("ocr"):
                lines.append(f"[历史图片{index}OCR] {entry['ocr']}")
        return "\n".join(lines)

    async def _ocr_images(
        self,
        event: AiocqhttpMessageEvent,
        image_urls: list,
        group_id: str = "",
    ) -> str:
        """识别全部图片；全局许可限制实际并发并保持输入顺序。"""
        selected_urls = self._select_image_urls(image_urls)
        if not selected_urls:
            return ""

        async def recognize(image_url: str) -> str:
            try:
                data = None
                # 感知哈希广告黑名单快速命中（可选）：命中直接标记并跳过视觉 API 调用，
                # 并缓存哈希供广告确认后学习。
                if self._cfg("ad_hash_blacklist_enabled", False, group_id=group_id):
                    data = await self._download_bytes(image_url)
                    if data:
                        phash = self._phash_from_data(data)
                        if phash:
                            self._recent_media_hashes[image_url] = phash
                            distance = self._cfg_int(
                                "ad_hash_distance", 10, group_id=group_id
                            )
                            best = self._check_hash_blacklist(phash, distance)
                            if best <= distance:
                                result = (
                                    f"[已知广告图] 命中广告黑名单"
                                    f"(相似度{64 - best}/64)"
                                )
                                self._cache_image_evidence(image_url, "ocr", result)
                                return result
                # 非 LLM 识别引擎（local/umi/cloud），或 auto 的本地优先
                engine = self._ad_engine(group_id)
                if engine in ("local", "umi", "cloud", "auto"):
                    if data is None:
                        data = await self._download_bytes(image_url)
                    media_text = await self._detect_media_text(data, group_id)
                    if media_text:
                        result = media_text
                        if engine == "cloud":
                            result = "[云API] " + media_text
                        self._cache_image_evidence(image_url, "ocr", result)
                        return result
                    if engine in ("local", "umi", "cloud"):
                        return ""
                # llm 引擎，或 auto 本地无结果时回退 LLM 视觉
                is_gif = self._is_gif_url(image_url)
                is_sticker = self._is_sticker_image(image_url)
                ocr_text = await self._call_llm_ocr(
                    image_url,
                    is_gif=is_gif,
                    is_sticker=is_sticker,
                    group_id=group_id,
                )
                if ocr_text and ocr_text.strip():
                    prefix = "[GIF动图] " if is_gif else "[表情包] " if is_sticker else ""
                    result = prefix + ocr_text.strip()
                    self._cache_image_evidence(image_url, "ocr", result)
                    return result
            except Exception as exc:
                logger.debug(f"[GroupMgr] OCR识别失败: {exc}")
            return ""

        results = await self._map_image_work(
            selected_urls, recognize, IMAGE_WORKER_CONCURRENCY
        )
        return self._format_image_evidence(results, "OCR")

    async def _call_llm_ocr(
        self,
        image_url: str,
        is_gif: bool = False,
        is_sticker: bool = False,
        group_id: str = "",
    ) -> str:
        """限制 OCR 实际并发；峰值排队等待，长期过载有明确边界。"""
        semaphore = getattr(self, "_ocr_semaphore", None)
        acquired = False
        try:
            if semaphore is not None:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=OCR_QUEUE_TIMEOUT
                )
                acquired = True
            return await self._run_llm_with_limits(
                lambda: self._call_llm_ocr_impl(
                    image_url,
                    is_gif=is_gif,
                    is_sticker=is_sticker,
                    group_id=group_id,
                ),
                timeout=LLM_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("[GroupMgr] OCR LLM调用或排队超时")
            return ""
        except Exception as exc:
            logger.debug(f"[GroupMgr] OCR LLM调用失败: {exc}")
            return ""
        finally:
            if acquired:
                semaphore.release()

    async def _call_llm_ocr_impl(
        self,
        image_url: str,
        is_gif: bool = False,
        is_sticker: bool = False,
        group_id: str = "",
    ) -> str:
        configured_id = str(
            self.config.get("ocr_provider_id", "")
            or self.config.get("moderation_llm_provider_id", "")
            or ""
        ).strip()
        template_key = self._cfg_str(
            "ocr_prompt_template", "default", group_id=group_id
        ).strip()
        custom_system = self._cfg_str(
            "ocr_custom_system_prompt", "", group_id=group_id
        ).strip()
        custom_user = self._cfg_str(
            "ocr_custom_user_prompt", "", group_id=group_id
        ).strip()

        if custom_system and custom_user:
            system_prompt = custom_system
            prompt = custom_user
        else:
            template = self._OCR_PROMPT_TEMPLATES.get(
                template_key, self._OCR_PROMPT_TEMPLATES["default"]
            )
            system_prompt = template["system"]
            prompt = template["prompt"]

        if is_gif:
            prompt += "\n注意：这是一张GIF动图，可能包含多帧内容。请仔细观察每一帧，描述所有帧中出现的内容和文字，特别关注是否有违规内容在动画帧中出现。"
        elif is_sticker:
            prompt += "\n注意：这是一个表情包/贴纸图片。表情包中常包含文字，请完整转录表情包中的所有文字，并判断文字内容是否违规（如侮辱性脏话、广告推广等）。"

        try:
            if hasattr(self.context, "llm_generate"):
                kwargs = {
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    "image_urls": [image_url],
                }
                if configured_id:
                    kwargs["chat_provider_id"] = configured_id
                try:
                    response = await self.context.llm_generate(**kwargs)
                    if response:
                        return self._extract_llm_text(response)
                except TypeError:
                    pass

            if configured_id and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(configured_id)
                if provider and hasattr(provider, "text_chat"):
                    try:
                        response = await provider.text_chat(
                            system_prompt=system_prompt,
                            prompt=prompt,
                            image_urls=[image_url],
                        )
                        if response:
                            return self._extract_llm_text(response)
                    except TypeError:
                        pass
                    try:
                        response = await provider.text_chat(
                            system_prompt
                            + "\n\n图片URL: "
                            + image_url
                            + "\n\n"
                            + prompt
                        )
                        if response:
                            return self._extract_llm_text(response)
                    except Exception as exc:
                        logger.debug(f"[GroupMgr] OCR LLM单次调用失败: {exc}")
            return ""
        except Exception as exc:
            logger.debug(f"[GroupMgr] OCR LLM调用失败: {exc}")
            return ""

    async def _apply_ocr(
        self,
        text: str,
        image_urls: list,
        event: AiocqhttpMessageEvent,
        group_id: str,
    ) -> str:
        """并行组装二维码和视觉识别文本，供统一审核流程使用。"""
        full_image_audit = (
            self._cfg("llm_moderation_enabled", True, group_id=group_id)
            and self._cfg("llm_moderation_always", False, group_id=group_id)
        )
        qr_call = None
        if image_urls and (
            full_image_audit
            or self._cfg("qrcode_decode_enabled", False, group_id=group_id)
        ):
            qr_call = self._decode_qrcodes(image_urls)

        ocr_call = None
        if image_urls and (
            full_image_audit or self._cfg("ocr_enabled", False, group_id=group_id)
        ):
            ocr_urls = image_urls
            if not full_image_audit and not self._cfg(
                "scan_sticker_enabled", True, group_id=group_id
            ):
                ocr_urls = [url for url in image_urls if not self._is_sticker_image(url)]
            if ocr_urls:
                ocr_call = self._ocr_images(event, ocr_urls, group_id=group_id)

        branches = {}
        if qr_call is not None:
            branches["qr"] = self._create_image_audit_task(qr_call)
        if ocr_call is not None:
            branches["ocr"] = self._create_image_audit_task(ocr_call)
        qr_text = ocr_text = ""
        if branches:
            tasks = set(branches.values())
            try:
                done, pending = await asyncio.wait(
                    tasks, timeout=IMAGE_AUDIT_TOTAL_TIMEOUT
                )
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            if pending:
                logger.warning(
                    f"[GroupMgr] 单条消息图片审核总时长超限: group={group_id}, "
                    f"pending={len(pending)}"
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            for name, task in branches.items():
                if task not in done or task.cancelled():
                    continue
                try:
                    result = task.result()
                except Exception as exc:
                    logger.debug(f"[GroupMgr] 图片{name}分支失败: {exc}")
                    continue
                if name == "qr":
                    qr_text = result
                else:
                    ocr_text = result
        if getattr(self, "_image_audit_closing", False):
            raise asyncio.CancelledError

        if qr_text:
            text = (
                text + "\n[二维码内容]\n" + qr_text
                if text
                else "[二维码内容]\n" + qr_text
            )
        if ocr_text:
            text = (
                text + "\n[OCR识图内容]\n" + ocr_text
                if text
                else "[OCR识图内容]\n" + ocr_text
            )
        elif full_image_audit and image_urls:
            marker = "[图片消息：视觉模型未返回可用识别结果]"
            text = text + "\n" + marker if text else marker
            logger.warning(
                f"[GroupMgr] 全量审核未取得图片识别结果: group={group_id}, "
                f"images={len(self._select_image_urls(image_urls))}"
            )
        if not text:
            return ""
        return self._bounded_audit_text(text, IMAGE_COMBINED_AUDIT_MAX_CHARS)

    async def _decode_qrcodes(self, image_urls: list) -> str:
        """受控并发下载并解码全部图片，结果保持图片原顺序。"""
        decoder = _probe_qr_decoder()
        if not decoder:
            if not getattr(self, "_qr_warned", False):
                self._qr_warned = True
                logger.warning(
                    "[GroupMgr] 已开启二维码解码但解码库(opencv-python-headless)不可用，功能不生效。"
                    "正常情况下随插件依赖已自动安装；若手动删除过可重装: "
                    "pip install opencv-python-headless numpy"
                )
            return ""

        async def decode(url: str) -> list:
            try:
                data = await self._download_bytes(url)
                if not data:
                    return []
                values = await self._run_qr_decoder(data, decoder)
                values = [
                    value.strip() for value in values if value and value.strip()
                ]
                if values:
                    self._cache_image_evidence(url, "qr", "\n".join(values))
                return values
            except Exception as exc:
                logger.debug(f"[GroupMgr] 二维码解码失败: {exc}")
                return []

        selected_urls = self._select_image_urls(image_urls)
        decoded = await self._map_image_work(
            selected_urls, decode, IMAGE_WORKER_CONCURRENCY
        )
        hit_count = sum(len(values or []) for values in decoded)
        if hit_count:
            logger.info(f"[GroupMgr] 二维码解码命中 {hit_count} 条")
        joined = ["\n".join(values or []) for values in decoded]
        return self._format_image_evidence(joined, "二维码")

    async def _run_qr_decoder(self, data: bytes, decoder: str) -> list:
        """以插件实例级许可限制实际运行中的二维码线程数。"""
        semaphore = getattr(self, "_qr_decode_semaphore", None)
        acquired = False
        future = None
        try:
            if semaphore is not None:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=IMAGE_QUEUE_TIMEOUT
                )
                acquired = True
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, _decode_qr_from_bytes, data, decoder)
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=IMAGE_QR_DECODE_TIMEOUT
            )
        except asyncio.TimeoutError:
            if acquired and future is not None:
                future.add_done_callback(
                    lambda _future, sem=semaphore: sem.release()
                )
                acquired = False
            logger.debug("[GroupMgr] 二维码解码或排队超时")
            return []
        except asyncio.CancelledError:
            if acquired and future is not None:
                future.add_done_callback(
                    lambda _future, sem=semaphore: sem.release()
                )
                acquired = False
            raise
        finally:
            if acquired:
                semaphore.release()

    @staticmethod
    def _is_private_host(host: str) -> bool:
        """判定主机是否指向内网、本机或保留地址。"""
        if not host:
            return True
        try:
            address = ipaddress.ip_address(host)
            return (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
            )
        except ValueError:
            pass
        try:
            for info in socket.getaddrinfo(host, None):
                address = ipaddress.ip_address(info[4][0])
                if (
                    address.is_private
                    or address.is_loopback
                    or address.is_link_local
                    or address.is_reserved
                ):
                    return True
            return False
        except Exception:
            return True

    async def _is_safe_image_url(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        loop = asyncio.get_running_loop()
        try:
            is_private = await asyncio.wait_for(
                loop.run_in_executor(None, self._is_private_host, parsed.hostname),
                timeout=IMAGE_DNS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.debug(f"[GroupMgr] 图片地址 DNS 解析超时: {parsed.hostname}")
            return False
        if is_private:
            logger.debug(
                f"[GroupMgr] 拒绝下载内网/不可解析地址图片: {parsed.hostname}"
            )
        return not is_private

    async def _get_image_http_session(self):
        session = getattr(self, "_image_http_session", None)
        if session is not None and not getattr(session, "closed", False):
            return session
        lock = getattr(self, "_image_http_session_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._image_http_session_lock = lock
        async with lock:
            if getattr(self, "_image_audit_closing", False):
                return None
            session = getattr(self, "_image_http_session", None)
            if session is not None and not getattr(session, "closed", False):
                return session
            try:
                import aiohttp
            except Exception:
                return None
            session = aiohttp.ClientSession()
            self._image_http_session = session
            return session

    async def _download_bytes(
        self,
        url: str,
        max_bytes: int = IMAGE_MAX_BYTES,
        timeout: float = IMAGE_DOWNLOAD_TIMEOUT,
    ):
        """在共享 I/O 限额内下载图片，并逐跳校验重定向地址。"""
        semaphore = getattr(self, "_image_io_semaphore", None)
        acquired = False
        try:
            if semaphore is not None:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=IMAGE_QUEUE_TIMEOUT
                )
                acquired = True
            session = await self._get_image_http_session()
            if session is None:
                return None
            import aiohttp

            current_url = url
            for redirect_count in range(IMAGE_MAX_REDIRECTS + 1):
                if not await self._is_safe_image_url(current_url):
                    return None
                async with session.get(
                    current_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "")
                        if not location or redirect_count >= IMAGE_MAX_REDIRECTS:
                            return None
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status != 200:
                        return None
                    chunks = []
                    total = 0
                    while total <= max_bytes:
                        chunk = await response.content.read(
                            min(64 * 1024, max_bytes + 1 - total)
                        )
                        if not chunk:
                            return b"".join(chunks)
                        chunks.append(chunk)
                        total += len(chunk)
                    return None
            return None
        except asyncio.TimeoutError:
            logger.debug(f"[GroupMgr] 图片下载排队或请求超时({url[:60]})")
            return None
        except Exception as exc:
            logger.debug(f"[GroupMgr] 下载图片失败({url[:60]}): {exc}")
            return None
        finally:
            if acquired:
                semaphore.release()
