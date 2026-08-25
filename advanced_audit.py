# -*- coding: utf-8 -*-
"""高级审核能力（v2.13.0）：外链邀请撤回 / 链接安全检测 / GIF 帧级拆分审核 / 语音消息审核。

四项功能均默认关闭（配置开关控制），全部为增量能力，不影响既有审核链路：

1. 外链邀请撤回（invite_link_recall_enabled）：
   检测消息中的外部群邀请链接（QQ qm.qq.com/jq.qq.com/qun.qq.com/pd.qq.com、
   Telegram t.me、Discord discord.gg/discord.com/invite），命中即撤回并记录。
   高置信度（链接特征明确），不依赖 LLM。

2. 链接安全检测（url_safety_enabled）：
   提取消息中全部 URL → 解析域名 → 与「内置短链域名 + 用户自定义风险域名 +
   自定义风险正则」比对，命中即撤回并记录。用于拦截赌博/诈骗/引流等风险链接。

3. GIF 帧级拆分审核（gif_frame_audit_enabled）：
   对 GIF 动图下载后用 OpenCV 逐帧拆解，对关键帧做本地 OCR（复用多识别引擎），
   把每帧识别文字并入审核正文，避免中间帧违规漏检。失败自动降级为整体图审核。

4. 语音消息审核（voice_audit_enabled）：
   收集语音消息段 → 下载音频 → 调通用 HTTP ASR 接口（voice_asr_url）转文字 →
   并入审核正文。ASR 为外部服务（如自建 whisper、云 ASR），默认关闭。

本模块全部采用 try/except 兜底，任何一环失败都静默降级，绝不影响正常消息。
"""
import asyncio
import re
from urllib.parse import urlparse

from astrbot.api import logger

# 消息中的 URL 提取
_URL_RE = re.compile(r"https?://[^\s，。；;\"'<>()（）]+", re.I)

# 外部群邀请链接：域名级匹配（QQ / Telegram / Discord）
_INVITE_HOST_RES = [
    re.compile(r"qm\.qq\.com", re.I),
    re.compile(r"jq\.qq\.com", re.I),
    re.compile(r"qun\.qq\.com", re.I),
    re.compile(r"pd\.qq\.com", re.I),
    re.compile(r"t\.me", re.I),
    re.compile(r"discord\.gg", re.I),
    re.compile(r"discord\.com/invite", re.I),
]
# v2.29.0：QQ 群邀请链接（域名级，无条件拦截）——QQ 群内发 qm.qq.com 等
# 群邀请链接是明确引流特征，不依赖 invite_link_recall_enabled 开关。
_QQ_GROUP_HOST_RES = _INVITE_HOST_RES[:4]
# 邀请链接兜底：纯文本"群号+数字"特征（防短链/去链化）
_INVITE_TEXT_RES = [
    re.compile(r"群号\s*[:：]?\s*\d{5,12}", re.I),
]

# 内置常见短链/跳转域名（常用于隐藏恶意目标）；用户可用 url_risk_domains 追加
DEFAULT_RISK_DOMAINS = (
    "t.cn", "dwz.cn", "url.cn", "bit.ly", "goo.gl", "is.gd", "tinyurl.com",
    "sourl.cn", "dian.run", "suo.im", "u6.gg", "0x9.me", "6tu.cc",
)

URL_AUDIT_HTTP_TIMEOUT = 15.0


class AdvancedAuditMixin:
    """外链邀请 / 链接安全 / GIF 帧级 / 语音审核。依赖 OneBotMixin 通用工具。"""

    # ------------------------------------------------------------------
    # URL 提取工具
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_urls(text: str) -> list:
        """从文本提取全部 URL（去重保序）。"""
        if not text:
            return []
        seen, out = set(), []
        for m in _URL_RE.finditer(text):
            url = m.group(0)
            if url not in seen:
                seen.add(url)
                out.append(url)
        return out

    @staticmethod
    def _url_domain(url: str) -> str:
        """提取 URL 的主机名（小写，去 www.）。失败返回空串。"""
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            return ""
        return host[4:] if host.startswith("www.") else host

    # ------------------------------------------------------------------
    # 1. 外链邀请撤回（默认关闭）
    # ------------------------------------------------------------------
    def _find_invite_link(self, text: str) -> str:
        """检测文本中的外部群邀请链接，命中返回链接/特征串，否则返回空。"""
        if not text:
            return ""
        for url in self._extract_urls(text):
            if any(rx.search(url) for rx in _INVITE_HOST_RES):
                return url
        # 兜底：QQ 群号明文特征
        for m in _INVITE_TEXT_RES.finditer(text):
            return m.group(0)
        return ""

    def _find_qq_group_link(self, text: str) -> str:
        """v2.29.0：检测文本中的 QQ 群邀请链接（域名级，无需完整 URL 前缀）。

        QQ 群内发送 qm.qq.com / jq.qq.com / qun.qq.com / pd.qq.com 群邀请
        链接是明确引流特征，无条件拦截；去链化（无 https:// 前缀）也能命中。
        """
        if not text:
            return ""
        for url in self._extract_urls(text):
            if any(rx.search(url) for rx in _QQ_GROUP_HOST_RES):
                return url
        for rx in _QQ_GROUP_HOST_RES:
            m = rx.search(str(text))
            if m:
                return m.group(0)
        return ""

    def _invite_link_hit(self, group_id: str) -> bool:
        """外链邀请检测是否开启（可按群覆盖）。"""
        try:
            return self._cfg("invite_link_recall_enabled", False, group_id=group_id)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 2. 链接安全检测（默认关闭）
    # ------------------------------------------------------------------
    def _get_risk_domains(self, group_id: str = "") -> set:
        """合并内置短链域名 + 用户自定义域名（去重小写）。"""
        custom = self._cfg_str("url_risk_domains", "", group_id=group_id)
        domains = set(DEFAULT_RISK_DOMAINS)
        for d in str(custom or "").replace("，", ",").split(","):
            d = d.strip().lower()
            if d and "." in d:
                domains.add(d)
        return domains

    def _find_risk_url(self, text: str, group_id: str = "") -> str:
        """检测消息中的风险 URL，命中返回原 URL，否则返回空。"""
        if not text:
            return ""
        domains = self._get_risk_domains(group_id)
        custom_pats = self._cfg_str("url_risk_patterns", "", group_id=group_id)
        patterns = []
        for p in str(custom_pats or "").split("\n"):
            p = p.strip()
            if p and not p.startswith("#"):
                try:
                    patterns.append(re.compile(p, re.I))
                except re.error:
                    logger.debug(f"[GroupMgr] 无效的风险链接正则: {p}")
        for url in self._extract_urls(text):
            host = self._url_domain(url)
            if host and any(host == d or host.endswith("." + d) for d in domains):
                return url
            if any(rx.search(url) for rx in patterns):
                return url
        return ""

    # ------------------------------------------------------------------
    # 3. GIF 帧级拆分审核（默认关闭）
    # ------------------------------------------------------------------
    def _collect_gif_components(self, event) -> list:
        """从事件消息段收集 GIF 动图 URL。返回 url 列表。"""
        gif_urls = []
        try:
            for comp in (event.get_messages() or []):
                t = str(getattr(comp, "type", "") or "").lower()
                if t != "image":
                    continue
                url = self._pick_media_url(comp)
                if url and self._is_gif_url(url):
                    gif_urls.append(url)
        except Exception as e:
            logger.debug(f"[GroupMgr] 收集 GIF 段失败: {e}")
        return gif_urls[:5]

    def _pick_media_url(self, comp) -> str:
        """从消息组件提取可用 url（兼容 url / file 字段，file 优先 http 的）。"""
        url = str(getattr(comp, "url", "") or "").strip()
        if url:
            return url
        file_val = str(getattr(comp, "file", "") or "").strip()
        if file_val and (file_val.startswith("http://") or file_val.startswith("https://")):
            return file_val
        return ""

    def _gif_frame_hit(self, group_id: str) -> bool:
        """GIF 帧级拆分审核是否开启（可按群覆盖）。"""
        try:
            return self._cfg("gif_frame_audit_enabled", False, group_id=group_id)
        except Exception:
            return False

    async def _apply_gif_frame_audit(self, text: str, gif_urls: list, event, group_id: str) -> str:
        """GIF 逐帧拆分审核：下载 → OpenCV 逐帧 → 本地 OCR 识别每帧文字，并入正文。"""
        if not gif_urls:
            return text
        max_frames = max(1, min(self._cfg_int("gif_max_frames", 15, group_id=group_id), 60))
        collected = []
        for url in gif_urls[:2]:
            try:
                data = await self._download_bytes(url)
                if not data:
                    continue
                frames = await asyncio.to_thread(
                    self._decode_gif_frames, data, max_frames
                )
                for frame_idx, frame_bytes in enumerate(frames):
                    frame_text = await self._detect_media_text(frame_bytes, group_id)
                    if frame_text and frame_text.strip():
                        collected.append(f"[GIF第{frame_idx + 1}帧] {frame_text.strip()[:200]}")
                    if len(collected) >= 8:  # 限制合并条数，防止正文爆炸
                        break
            except Exception as e:
                logger.debug(f"[GroupMgr] GIF 帧级审核失败({url}): {e}")
        if collected:
            text = (text + "\n" if text else "") + "\n".join(collected[:8])
        return text

    @staticmethod
    def _decode_gif_frames(data: bytes, max_frames: int) -> list:
        """把 GIF 字节拆成最多 max_frames 帧的 JPEG 字节列表。失败返回 []。"""
        try:
            import cv2
            import numpy as np
            import tempfile
            import os

            if cv2 is None:
                return []
            with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                frames = []
                cap = cv2.VideoCapture(tmp_path)
                while True:
                    ok, frame = cap.read()
                    if not ok or len(frames) >= max_frames:
                        break
                    if frame is None:
                        continue
                    ok2, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                    if ok2:
                        frames.append(np.array(buf).tobytes())
                cap.release()
                return frames
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 4. 语音消息审核（默认关闭）
    # ------------------------------------------------------------------
    def _collect_voice_components(self, event) -> list:
        """从事件消息段收集语音（Record）组件。"""
        voices = []
        try:
            for comp in (event.get_messages() or []):
                t = str(getattr(comp, "type", "") or "").lower()
                if t in ("record", "voice", "audio"):
                    url = self._pick_media_url(comp)
                    if url:
                        voices.append(url)
        except Exception as e:
            logger.debug(f"[GroupMgr] 收集语音段失败: {e}")
        return voices[:3]

    def _voice_hit(self, group_id: str) -> bool:
        """语音审核是否开启（可按群覆盖）。"""
        try:
            return self._cfg("voice_audit_enabled", False, group_id=group_id)
        except Exception:
            return False

    async def _apply_voice_audit(self, text: str, voice_urls: list, event, group_id: str) -> str:
        """语音 ASR 审核：下载音频 → 调通用 HTTP ASR 接口转文字，并入正文。"""
        if not voice_urls:
            return text
        asr_url = self._cfg_str("voice_asr_url", "").strip().rstrip("/")
        if not asr_url:
            logger.debug("[GroupMgr] 语音审核开启但未配置 voice_asr_url，跳过")
            return text
        lines = []
        for url in voice_urls[:2]:
            try:
                data = await self._download_bytes(url)
                if not data:
                    continue
                transcript = await self._voice_asr_call(data, asr_url)
                if transcript and transcript.strip():
                    lines.append(f"[语音] {transcript.strip()[:300]}")
            except Exception as e:
                logger.debug(f"[GroupMgr] 语音 ASR 审核失败({url}): {e}")
        if lines:
            text = (text + "\n" if text else "") + "\n".join(lines[:2])
        return text

    async def _voice_asr_call(self, data: bytes, asr_url: str) -> str:
        """调用通用 HTTP ASR 接口：POST {audio} (multipart) → JSON {text}。失败返回空。"""
        try:
            import aiohttp

            form = aiohttp.FormData()
            form.add_field("audio", data, filename="voice.mp3", content_type="application/octet-stream")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{asr_url}/api/asr",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=URL_AUDIT_HTTP_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"[GroupMgr] 语音 ASR 返回状态 {resp.status}")
                        return ""
                    result = await resp.json()
            return str(result.get("text", "") or "")
        except Exception as exc:
            logger.warning(f"[GroupMgr] 语音 ASR 调用失败: {exc}")
            return ""


    # ------------------------------------------------------------------
    # 链接类违规的统一处置（外链邀请 / 风险链接，高置信度直接撤回）
    # ------------------------------------------------------------------
    async def _detect_link_violation(self, text: str, group_id: str = ""):
        """检测高置信链接违规（外链邀请 / 风险链接）。命中返回 (label, reason)，否则 None。"""
        if not text:
            return None
        # v2.29.0：QQ 群邀请链接（qm.qq.com 等）为明确引流特征，无条件拦截，
        # 不依赖 invite_link_recall_enabled 开关；命中即撤回+记录。
        try:
            qq_invite = self._find_qq_group_link(text)
            if qq_invite:
                return ("外链邀请", f"QQ 群邀请链接: {qq_invite}")
        except Exception as e:
            logger.debug(f"[GroupMgr] QQ 群链接检测异常: {e}")
        try:
            if self._invite_link_hit(group_id):
                invite = self._find_invite_link(text)
                if invite:
                    return ("外链邀请", f"外部群邀请链接: {invite}")
        except Exception as e:
            logger.debug(f"[GroupMgr] 外链邀请检测异常: {e}")
        try:
            if self._url_safety_hit(group_id):
                risk = self._find_risk_url(text, group_id)
                if risk:
                    return ("风险链接", f"命中风险域名/特征: {risk}")
        except Exception as e:
            logger.debug(f"[GroupMgr] 风险链接检测异常: {e}")
        return None

    async def _handle_link_violation(self, event, group_id: str, user_id: str,
                                     user_name: str, text: str, violation: tuple):
        """处置链接类违规：撤回 + 记录 + 群内提示（可选）+ 终止后续处理。"""
        label, reason = violation
        msg_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if msg_id:
            try:
                await self._recall_msg(event, msg_id)
            except Exception as e:
                logger.debug(f"[GroupMgr] 撤回链接违规消息失败: {e}")
        try:
            self._log_moderation(group_id, user_id, user_name, text[:200], "撤回", reason)
        except Exception as e:
            logger.debug(f"[GroupMgr] 记录链接违规失败: {e}")
        if self._cfg("auto_moderate_notice", True, group_id=group_id):
            yield event.plain_result(f"检测到{label}，已撤回")
        try:
            event.stop_event()
        except Exception:
            pass

    def _url_safety_hit(self, group_id: str) -> bool:
        """链接安全检测是否开启（可按群覆盖）。"""
        try:
            return self._cfg("url_safety_enabled", False, group_id=group_id)
        except Exception:
            return False
