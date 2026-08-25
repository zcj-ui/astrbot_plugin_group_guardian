# -*- coding: utf-8 -*-
"""广告识别引擎：本地 RapidOCR / Umi-OCR / 第三方云 API，统一供图片与视频帧识别。

- **local（默认）**：RapidOCR（ONNX），模型不随插件打包——开启 local 引擎时按需
  自动安装 `rapidocr_onnxruntime`（含模型约 30MB）；关闭后**不卸载**，模型保留本地；
- **umi**：Umi-OCR（Rapid 引擎版）HTTP 服务（默认 http://127.0.0.1:1224），需用户
  自行安装运行 Umi-OCR，插件通过 HTTP 调用，专门为视频/图片广告服务；
- **cloud**：第三方云广告检测 API（如阿里云内容安全，通用 JSON 协议），返回是否含广告；
- **llm**（可选，不再默认）：云端视觉模型（智谱 GLM-4V 等），保留兼容；
- **auto**：本地/云优先，识别不到再回退云端视觉。

同步调用放入线程池，不阻塞事件循环；引擎单例懒加载、全局复用。
"""

import asyncio
import base64
import sys
import time

from astrbot.api import logger

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:  # pragma: no cover
    RapidOCR = None


class LocalOCRMixin:
    """广告识别引擎能力，由 ``ModerationMixin`` 组合使用。"""

    def _init_local_ocr(self) -> None:
        """初始化识别引擎：本地引擎槽位、并发锁、可用性标记。"""
        self._local_ocr_engine = None
        self._local_ocr_available = RapidOCR is not None
        self._local_ocr_lock = asyncio.Lock()
        self._ocr_repair_lock = asyncio.Lock()
        self._last_ocr_repair_ts = 0.0
        self._ocr_repair_cooldown = 600  # 自我修复冷却（秒），避免频繁重装

    def _ad_engine(self, group_id: str = None) -> str:
        """当前广告识别引擎（local/umi/cloud/llm/auto），非法值回落 local。"""
        try:
            engine = self._cfg_str("ocr_engine", "local", group_id=group_id)
        except Exception:
            engine = "local"
        engine = str(engine or "").strip().lower()
        return engine if engine in ("local", "umi", "cloud", "llm", "auto") else "local"

    # ============================================================
    # 模型按需安装（zip 不带模型，开启 local 引擎时才下载，关闭不卸载）
    # ============================================================

    # ============================================================
    async def _ensure_local_ocr(self) -> bool:
        """确保本地 RapidOCR 可用：未安装时自动安装（含详细日志），失败自动重试。"""
        global RapidOCR
        if RapidOCR is not None:
            return True
        auto = True
        try:
            auto = bool(self.config.get("local_ocr_auto_install", True))
        except Exception:
            auto = True
        if not auto:
            logger.warning("[GroupMgr] 本地OCR未安装且未开启自动安装(local_ocr_auto_install)，本地识别将跳过")
            return False
        async with self._ocr_repair_lock:
            logger.info("[GroupMgr] [安装日志] 本地OCR引擎不可用，开始自动安装 rapidocr_onnxruntime（含模型约30MB，请耐心等待）...")
            for attempt in (1, 2):
                try:
                    logger.info(f"[GroupMgr] [安装日志] 第{attempt}次执行: pip install rapidocr_onnxruntime")
                    mirror = ""
                    try:
                        mirror = str(self.config.get(
                            "local_ocr_pip_mirror",
                            "https://pypi.tuna.tsinghua.edu.cn/simple",
                        ) or "").strip()
                    except Exception:
                        mirror = ""
                    install_cmd = [sys.executable, "-m", "pip", "install", "rapidocr_onnxruntime"]
                    if mirror:
                        install_cmd += ["-i", mirror]
                    proc = await asyncio.create_subprocess_exec(
                        *install_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )

                    async def _pump_output():
                        assert proc.stdout is not None
                        while True:
                            raw = await proc.stdout.readline()
                            if not raw:
                                break
                            text = raw.decode("utf-8", "ignore").strip()
                            if text:
                                logger.info(f"[GroupMgr] [pip] {text[:200]}")

                    pump = asyncio.create_task(_pump_output())
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=300)
                    finally:
                        pump.cancel()
                        try:
                            await pump
                        except Exception:
                            pass
                    if proc.returncode != 0:
                        logger.warning(f"[GroupMgr] [安装日志] pip 安装退出码={proc.returncode}，尝试重试")
                        continue
                    try:
                        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
                    except ImportError:
                        logger.warning("[GroupMgr] [安装日志] 安装完成但 import 失败，尝试重试")
                        continue
                    self._local_ocr_available = RapidOCR is not None
                    logger.info("[GroupMgr] [安装日志] 本地OCR安装成功（模型已下载；关闭引擎不会卸载模型）")
                    return True
                except Exception as exc:
                    logger.warning(f"[GroupMgr] [安装日志] 安装异常: {exc}")
            logger.warning("[GroupMgr] [安装日志] 本地OCR安装失败，请手动执行: pip install rapidocr_onnxruntime")
            return False


    # 本地 RapidOCR
    # ============================================================

    async def _local_ocr_text(self, data: bytes) -> str:
        """对图片字节执行本地 RapidOCR，返回识别文字；失败/不可用返回空串。"""
        if not await self._ensure_local_ocr():
            return ""
        if not data:
            return ""
        if self._local_ocr_engine is None:
            async with self._local_ocr_lock:
                if self._local_ocr_engine is None:
                    try:
                        self._local_ocr_engine = RapidOCR(
                            det_use_cuda=False,
                            rec_use_cuda=False,
                            use_cls=False,
                            show_log=False,
                        )
                    except Exception as exc:
                        logger.warning(f"[GroupMgr] 初始化 RapidOCR 失败: {exc}")
                        self._local_ocr_engine = False
        if not self._local_ocr_engine:
            return ""
        try:
            result, _ = await asyncio.to_thread(self._local_ocr_engine, data)
            if not result:
                return ""
            texts = []
            for line in result:
                if len(line) > 1 and line[1]:
                    texts.append(str(line[1]))
            return " ".join(texts).strip()
        except Exception as exc:
            logger.debug(f"[GroupMgr] 本地 OCR 识别失败: {exc}")
            return ""

    # ============================================================
    # Umi-OCR（Rapid 引擎版，本地 HTTP 服务）
    # ============================================================

    async def _umi_ocr_text(self, data: bytes) -> str:
        """调用 Umi-OCR 的 HTTP API 识别图片文字；失败自动修复重试一次。"""
        if not data:
            return ""
        url = self._cfg_str("umi_ocr_url", "http://127.0.0.1:1224").strip().rstrip("/")
        if not url:
            return ""
        text = await self._umi_ocr_call(data, url)
        if text:
            return text
        # 自我修复：Umi-OCR 连不上时检查/提示，冷却内重试一次
        if await self._repair_ad_engine("umi"):
            text = await self._umi_ocr_call(data, url)
        return text

    async def _umi_ocr_call(self, data: bytes, url: str) -> str:
        """实际调用 Umi-OCR /api/ocr。"""
        try:
            import aiohttp

            form = aiohttp.FormData()
            form.add_field("image", data, filename="image.jpg", content_type="image/jpeg")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{url}/api/ocr",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[GroupMgr] Umi-OCR 返回状态 {resp.status}")
                        return ""
                    result = await resp.json()
            data_obj = result.get("data") or {}
            texts = data_obj.get("texts") or []
            lines = []
            for item in texts:
                if isinstance(item, dict):
                    t = item.get("text")
                    if t:
                        lines.append(str(t))
                elif isinstance(item, str):
                    lines.append(item)
            return " ".join(lines).strip()
        except Exception as exc:
            logger.warning(f"[GroupMgr] Umi-OCR 调用失败: {exc}")
            return ""


    # ============================================================
    # 第三方云广告检测 API（如阿里云内容安全）
    # ============================================================

    async def _cloud_audit_image(self, data: bytes) -> tuple:
        """调用第三方云广告检测 API（通用 JSON 协议）。返回 (is_ad, reason)。

        对接格式（需第三方服务支持）：
        POST {cloud_audit_url}  请求头 Authorization: Bearer {cloud_audit_api_key}
        {"image_base64": "<base64>"}  →  {"is_ad": bool, "score": float, "reason": str}
        """
        if not data:
            return False, ""
        url = self._cfg_str("cloud_audit_url", "").strip()
        if not url:
            logger.debug("[GroupMgr] 未配置 cloud_audit_url，云广告检测不可用")
            return False, ""
        api_key = self._cfg_str("cloud_audit_api_key", "").strip()
        threshold = 0.8
        try:
            threshold = float(self.config.get("cloud_audit_threshold", 0.8) or 0.8)
        except (TypeError, ValueError):
            threshold = 0.8
        try:
            import aiohttp

            payload = {"image_base64": base64.b64encode(data).decode("ascii")}
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        return False, ""
                    result = await resp.json()
            is_ad = bool(result.get("is_ad", False))
            score = float(result.get("score", 0.0) or 0.0)
            reason = str(result.get("reason", "") or "")
            if is_ad or score >= threshold:
                return True, reason or f"云API广告检测(score={score:.2f})"
            return False, ""
        except Exception as exc:
            logger.debug(f"[GroupMgr] 云广告检测调用失败: {exc}")
            return False, ""

    # ============================================================
    # 统一识别入口（非 LLM 引擎）
    # ============================================================

    async def _detect_media_text(self, data: bytes, group_id: str = None) -> str:
        """按当前引擎识别图片/视频帧中的文字（或云 API 广告判定）。

        返回识别文本；云 API 命中广告时返回含 [云API] 标记的文本。
        """
        engine = self._ad_engine(group_id)
        if engine == "umi":
            return await self._umi_ocr_text(data)
        if engine == "cloud":
            is_ad, reason = await self._cloud_audit_image(data)
            if is_ad:
                return f"[云API] 广告：{reason}"
            return ""
        # local 或 auto 的本地部分
        return await self._local_ocr_text(data)

    def _engine_prefer_llm(self, group_id: str = None) -> bool:
        """当前引擎是否需要 LLM 视觉（llm / auto 的回退路径）。"""
        return self._ad_engine(group_id) in ("llm", "auto")

    def _engine_cloud_only(self, group_id: str = None) -> bool:
        """当前引擎是否纯云 API（无本地 OCR 文字）。"""
        return self._ad_engine(group_id) == "cloud"

    # ============================================================
    # 自我修复（模型链接不上时自动检测并尝试恢复，带冷却）
    # ============================================================

    async def _check_umi_ocr(self, url: str) -> bool:
        """测试 Umi-OCR HTTP 服务是否可达。"""
        if not url:
            return False
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{url}/", timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    return resp.status < 500
        except Exception:
            return False

    async def _repair_ad_engine(self, engine: str = None, group_id: str = None) -> bool:
        """自我修复：检测并尝试恢复指定识别引擎（带冷却，默认 10 分钟）。

        - local：RapidOCR 缺失/加载失败 → 自动重装（含详细日志）；
        - umi：Umi-OCR 连不上 → 探测服务并给出修复提示；
        - cloud：外部服务，检查配置并提示；
        返回 True 表示引擎已恢复可用。
        """
        engine = (engine or self._ad_engine(group_id)).strip().lower()
        now = time.time()
        if now - self._last_ocr_repair_ts < self._ocr_repair_cooldown:
            logger.debug("[GroupMgr] [自我修复] 仍在冷却期，跳过本次修复")
            return False
        self._last_ocr_repair_ts = now
        if engine == "local":
            ok = await self._ensure_local_ocr()
            if ok:
                logger.info("[GroupMgr] [自我修复] 本地OCR已恢复可用")
            return ok
        if engine == "umi":
            url = self._cfg_str("umi_ocr_url", "http://127.0.0.1:1224").strip().rstrip("/")
            ok = await self._check_umi_ocr(url)
            if ok:
                logger.info("[GroupMgr] [自我修复] Umi-OCR 服务已可达")
            else:
                logger.warning(
                    f"[GroupMgr] [自我修复] Umi-OCR 连接失败({url})。"
                    "请确认已启动 Umi-OCR 并开启「HTTP服务」（默认端口1224）"
                )
            return ok
        if engine == "cloud":
            url = self._cfg_str("cloud_audit_url", "").strip()
            if not url:
                logger.warning("[GroupMgr] [自我修复] 未配置 cloud_audit_url，云广告检测不可用，请检查配置")
            else:
                logger.info(
                    f"[GroupMgr] [自我修复] 云API地址已配置({url})，"
                    "若持续失败请检查 API Key 与服务器网络"
                )
            return bool(url)
        return False
