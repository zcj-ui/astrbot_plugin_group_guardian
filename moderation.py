# -*- coding: utf-8 -*-
import json
import re
import time
from typing import Dict, Tuple

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


class ModerationMixin:
    # 审核主流程由 _handle_message 驱动（注册在 main.py），按以下顺序执行：
    # 1. 黑白名单 / 功能开关 / 管理员豁免检查
    # 2. 消息文本提取（支持普通消息 + 合并转发 + JSON卡片 + QQ收藏）
    # 3. 正则初筛（脏话、广告、敏感词库）
    # 4. OCR 识图审核（可选，需配置视觉模型）
    # 5. LLM 二次判断（30条上下文 + 可疑类型标签）
    # 6. 违规处理（撤回 + 记录日志），非违规补充段记录便于后续排查

    # OCR_PROMPT_TEMPLATES：预置的 OCR 图片识别提示词模板集合。
    # 三个预设模板（default / strict / text_only）分别适用于通用描述、严格审核、纯文字转录场景。
    # 每个模板包含 system（系统角色指令）和 prompt（用户提问）两部分。
    # 管理员在 WebUI 下拉菜单中可任选其一，或选择自定义模板覆盖此处的值。
    # 新增模板只需在此 dict 中添加 key，无需修改任何 WebUI 代码。
    _OCR_PROMPT_TEMPLATES = {
        "default": {
            # 通用识别模板：要求 LLM 详细描述图片中所有内容和文字。
            # 如果发现广告/推广/违规内容需特别说明，但不会专门分类。
            "system": "你是一个图片内容识别助手。请仔细观察图片，用文字详细描述图片中的所有内容。如果图片中有文字，请完整转录所有文字内容。如果图片是广告、推广、违规内容，请特别说明。只输出图片内容描述，不要输出其他内容。",
            "prompt": "请识别并描述这张图片的内容，特别注意图片中的文字。"
        },
        "strict": {
            # 严格审核模板：要求 LLM 按五大违规类别逐一检查（广告/色情/政治/暴恐/赌博）。
            # 需要在描述的最后给出"是否包含违规内容"的明确结论，适用于高安全要求的群聊。
            "system": "你是一个严格的内容审核图片识别助手。你的任务是识别图片中是否存在违规内容。请仔细检查：1.图片中是否有广告推广信息（联系方式、二维码、引流链接）2.是否有色情或低俗内容 3.是否有政治敏感内容 4.是否有暴恐或违法信息 5.是否有赌博或诈骗信息。如果图片中有文字，请完整转录。最后给出明确结论：该图片是否包含违规内容。",
            "prompt": "请严格审核这张图片，识别并描述所有可能违规的内容，完整转录图片中的文字。"
        },
        "text_only": {
            # 纯文字转录模板：仅要求 OCR 提取文字，不进行分析或审核判断。
            # 适用于仅需获取图片中文字的场合（如验证码、截图等），
            # 若图片无文字则固定回复「图片中无文字」以便上层逻辑区分。
            "system": "你是一个OCR文字识别助手。请将图片中的所有文字完整转录出来，保持原始格式和排版。如果图片中没有文字，请回复「图片中无文字」。只输出识别到的文字内容，不要添加任何分析或评论。",
            "prompt": "请将这张图片中的所有文字完整转录出来。"
        }
    }

    async def _fetch_context_messages(self, group_id: str, current_msg_id: str, count: int = 30) -> list:
        # 从群聊消息历史中拉取当前消息之前的上下文消息（最多 count 条，默认 30 条）。
        # 30 条是一个经验值：太少无法形成有效语境（判断脏话/政治误报需要看前后对话），
        # 太多则容易超出 LLM 的 token 限制且携带无关信息干扰判断。
        if not self._client:
            return []
        client = self._client
        gid = self._safe_int(group_id, 0)
        if not gid:
            return []
        try:
            # 调用 OneBot (go-cqhttp) 的 get_group_msg_history API 获取历史消息。
            # message_seq=0 表示从最新消息开始往前拉，count=min(count+5,100) 多取 5 条作为缓冲，
            # 因为过滤掉当前消息后可能有损耗，且 API 本身有最大 100 条的限制。
            result = await client.call_action('get_group_msg_history',
                group_id=gid, message_seq=0, count=min(count + 5, 100))
            result = self._extract_data_result(result)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            # 排除当前正在审核的消息本身（避免 LLM 混淆），然后取最后 count 条。
            return [m for m in messages if str(m.get('message_id', '')) != str(current_msg_id)][-count:]
        except Exception as e:
            logger.debug(f"[GroupMgr] 获取上下文消息失败: {e}")
            return []

    def _extract_llm_text(self, response) -> str:
        # 从 LLM 返回的响应对象中提取文本字符串。
        # AstrBot 的 LLM 响应包装器通常有 .completion_text 属性，
        # 若没有则直接转 str 兜底。
        if hasattr(response, 'completion_text'):
            return response.completion_text
        return str(response)

    async def _call_llm_safe(self, system_prompt: str, prompt: str) -> str:
        # 多级 Provider 调用的安全封装，按以下优先级逐级尝试：
        # 1) configured_id —— 用户在配置中手动指定的 LLM Provider ID
        # 2) get_all_providers() —— 遍历所有已注册的 Provider，逐一尝试
        # 3) provider_manager.get_using_provider() —— 获取当前正在使用的 Provider
        # 若所有级别均失败，则抛出 RuntimeError 并汇总前 5 条错误信息。

        configured_id = str(self.config.get("moderation_llm_provider_id", "")).strip()
        errors = []
        error_set = set()

        def _add_error(err: str):
            # 去重记录错误信息，避免反复记录相同错误占用 error 列表空间。
            if err not in error_set:
                error_set.add(err)
                errors.append(err)

        async def _try_text_chat(prov, pid: str) -> str:
            # 尝试调用 provider.text_chat() 方法。
            # 尝试两种签名风格以兼容不同 Provider 实现：
            #   签名1: text_chat(system_prompt=..., prompt=...) —— 标准命名参数
            #   签名2: text_chat(system_prompt + "\n\n" + prompt) —— 直接传拼接后的字符串
            # 如果某签名抛出 TypeError/ValueError（参数不匹配），静默跳到下一种。
            if not hasattr(prov, 'text_chat'):
                return None
            signatures = [
                ((), {'system_prompt': system_prompt, 'prompt': prompt}),
                ((system_prompt + "\n\n" + prompt,), {}),
            ]
            for args, kwargs in signatures:
                try:
                    r = await prov.text_chat(*args, **kwargs)
                    if r:
                        return str(r)
                except (TypeError, ValueError):
                    # 参数签名不匹配（如不支持命名参数），尝试下一种。
                    continue
                except Exception as e:
                    _add_error(f"{pid}.text_chat: {str(e)[:120]}")
                    continue
            return None

        async def _try_provider(prov, pid: str) -> str:
            # 在单个 Provider 上尝试所有可能的调用方式。
            # 优先尝试 text_chat()，若失败则依次尝试 chat()、invoke()、complete() 等老式方法名。
            # 每种方法也尝试多种参数签名以兼容不同实现。
            result = await _try_text_chat(prov, pid)
            if result:
                return result
            for meth in ('chat', 'invoke', 'complete'):
                fn = getattr(prov, meth, None)
                if not fn:
                    continue
                signatures = [
                    ((system_prompt + "\n\n" + prompt,), {}),
                    ((), {'prompt': system_prompt + "\n\n" + prompt}),
                ]
                for args, kwargs in signatures:
                    try:
                        r = await fn(*args, **kwargs)
                        if r:
                            return str(r)
                    except (TypeError, ValueError):
                        continue
                    except Exception as e:
                        _add_error(f"{pid}.{meth}: {str(e)[:120]}")
                        continue
            return None

        async def _try_by_id(pid: str) -> str:
            # 通过 Provider ID 获取并调用 LLM。
            # 优先使用 context.llm_generate()（AstrBot 提供的高级 API，支持多种参数），
            # 若失败则回退到 context.get_provider_by_id() 获取 Provider 实例后手动调用。
            if hasattr(self.context, 'llm_generate'):
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=pid, prompt=prompt, system_prompt=system_prompt)
                    if resp:
                        return self._extract_llm_text(resp)
                except Exception as e:
                    _add_error(f"llm_generate({pid}): {str(e)[:120]}")
            prov = self.context.get_provider_by_id(pid) if hasattr(self.context, 'get_provider_by_id') else None
            if prov:
                result = await _try_provider(prov, pid)
                if result:
                    return result
            raise RuntimeError(f"Provider {pid} 不可用")

        # ---------- 第一级：用户配置的指定 Provider ----------
        # 如果用户在配置中手动指定了 moderation_llm_provider_id，优先使用。
        # 适用于用户希望审核流量走独立 LLM（与日常对话模型隔离）的场景。
        if configured_id:
            try:
                result = await _try_by_id(configured_id)
                logger.info(f"[GroupMgr] LLM审核使用指定provider: {configured_id}")
                return result
            except Exception as e:
                _add_error(f"指定{configured_id}: {str(e)[:120]}")

        # ---------- 第二级：遍历所有已注册的 Provider ----------
        # 若未指定或指定 Provider 不可用，则通过 get_all_providers()
        # 获取 AstrBot 中注册的所有 Provider，依次尝试调用。
        try:
            ps = (self.context.get_all_providers() if hasattr(self.context, 'get_all_providers') else []) or []
        except Exception as e:
            ps = []
            _add_error(f"get_all_providers: {str(e)[:120]}")

        for p in ps:
            try:
                meta = p.meta()
                pid = meta.id
                result = await _try_by_id(pid)
                logger.info(f"[GroupMgr] LLM审核使用provider: {pid}")
                return result
            except Exception as e:
                _add_error(str(e)[:80])
                continue

        # ---------- 第三级：provider_manager 的当前 Provider ----------
        # 最后的兜底方案：通过 provider_manager.get_using_provider()
        # 获取 AstrBot 当前正在使用的默认 Provider（通常是主对话模型）。
        try:
            pm = getattr(self.context, 'provider_manager', None)
            if pm and hasattr(pm, 'get_using_provider'):
                up = pm.get_using_provider()
                if up:
                    result = await _try_provider(up, str(getattr(up, 'provider_name', up)))
                    if result:
                        logger.info("[GroupMgr] LLM审核使用provider_manager")
                        return result
        except Exception as e:
            _add_error(f"provider_manager: {str(e)[:120]}")

        # ---------- 所有级别均失败 ----------
        # 汇总前 5 条错误信息抛出异常，提示用户检查 AstrBot 的 LLM Provider 配置。
        detail = '; '.join(errors[:5]) if errors else '无任何可用Provider'
        raise RuntimeError(f"LLM调用失败({detail})。请检查AstrBot是否已配置LLM Provider")

    async def _call_llm_for_moderation(self, event: AiocqhttpMessageEvent,
                                        text: str, hit_types: Dict[str, bool],
                                        group_id: str = "") -> dict:
        """LLM 二次审核：携带 30 条上下文和可疑类型标签，要求 LLM 返回 JSON。

        Returns:
            {"violation": bool, "reason": str}
        """
        if not group_id:
            group_id = self._get_group_id(event)
        msg_obj = getattr(event, 'message_obj', None)
        msg_id = str(getattr(msg_obj, 'message_id', '')) if msg_obj else ''
        user_name = event.get_sender_name()

        # ---------- 上下文消息准备 ----------
        # 拉取当前消息之前的 30 条对话记录作为 LLM 判断的语境。
        # 这对于误报率较高的类别（如政治敏感、脏话）尤为重要——同样的词
        # 在技术讨论、游戏对话、历史讨论中可能是完全合法的。
        context_msgs = []
        if group_id and msg_id:
            context_msgs = await self._fetch_context_messages(group_id, msg_id, 30)
        context_text = ""
        if context_msgs:
            lines = []
            for m in context_msgs:
                sender_obj = m.get('sender')
                sender = sender_obj.get('nickname', '未知') if isinstance(sender_obj, dict) else '未知'
                content = self._format_message_content(m.get('message', ''))
                # 每条上下文消息截断到 200 字符，防止单条长消息淹没有效信息。
                if len(content) > 200:
                    content = content[:200] + '...'
                lines.append(f"  {sender}: {content}")
            context_text = "\n".join(lines)
            # 所有上下文总长度限制 3000 字符，超长则截取尾部（最近的消息更重要）。
            if len(context_text) > 3000:
                context_text = context_text[-3000:]

        # ---------- 可疑类型标签 ----------
        # 将正则/词库初筛命中的类型组装为人类可读的标签传给 LLM，
        # 让 LLM 知道哪些方面需要重点审查，降低漏判概率。
        suspect_types = [k for k, v in hit_types.items() if v]
        suspect_tag = "+".join(suspect_types) if suspect_types else "无"
        type_desc = {
            "swear": "骂人/脏话",
            "ad": "广告/推广",
            "political": "政治敏感",
            "porn": "色情/淫秽",
            "violent_terror": "暴恐内容",
            "reactionary": "反动言论",
            "weapons": "涉枪涉爆",
            "corruption": "贪腐相关",
            "illegal_url": "违规网址",
            "other": "其他违规",
            "supplement": "补充违规",
            "livelihood": "民生敏感",
            "tencent_ban": "腾讯封禁",
        }
        suspect_desc = "+".join([type_desc.get(t, t) for t in suspect_types]) if suspect_types else "无"

        # ---------- Prompt 模板 ----------
        # 完整的 LLM 审核提示词包含以下几部分：
        # 【核心准则】—— 通用的审核原则（脏话、广告零容忍、政治需结合上下文等）
        # 【审核标准】—— 逐类详细说明什么算违规、什么不算违规
        # 【JSON格式要求】—— 要求 LLM 严格输出 {"violation":bool, "reason":str}
        # 【被标记消息】—— 待审核的消息原文
        # 【上下文消息】—— 最近的群聊历史记录
        prompt = (
            f"你是一名群聊内容审核员。你的任务是判断被标记消息是否需要撤回，需要结合上下文语境合理判断。\n\n"
            f"【核心准则】\n"
            f"- 侮辱性脏话（傻逼、废物、脑残、操你妈等）对任何对象使用都应撤回，包括对机器人\n"
            f"- 广告内容零容忍，一律撤回\n"
            f"- 政治敏感词库误报率高，需结合上下文判断，技术/游戏讨论不违规\n"
            f"- 色情/暴恐等需结合上下文判断\n"
            f"- 涉及查询、泄露他人隐私信息（身份证、住址、电话等）→ 违规\n\n"
            f"【审核标准】\n"
            f"1. 骂人/脏话类（swear）—— 严格处理侮辱性词汇：\n"
            f"     * 使用侮辱性脏话（傻逼、废物、蠢货、脑残、智障等）\n"
            f"     * 涉及家人死亡的诅咒（\"你妈死了\"、\"死全家\"、\"nmsl\"等）\n"
            f"     * 极端恶意人身攻击，明显带有仇恨和恶意\n"
            f"     * 对任何对象使用\"傻逼\"、\"操你妈\"、\"废物\"等侮辱性词汇\n"
            f"     * 对机器人/AI使用侮辱性脏话（\"傻逼机器人\"、\"废物机器人\"等）\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 轻微口头禅（\"卧槽\"、\"我靠\"、\"牛逼\"等不含侮辱性的语气词）\n"
            f"     * 自嘲、自黑（\"我太菜了\"、\"我真是个憨憨\"等）\n"
            f"     * 游戏中的轻度调侃（\"垃圾队友\"、\"这打得真烂\"等游戏场景）\n\n"
            f"2. 广告类（ad）—— 零容忍，一律违规：\n"
            f"   - 任何推广引流行为 → 违规（加微信、扫码、兼职、赚钱、收徒、挂圈等）\n"
            f"   - 色情引流（\"18+进xxx\"、\"看片加Q\"、\"福利群\"等）→ 违规\n"
            f"   - 金融诈骗（开户、跑分、洗钱、赌博等）→ 违规\n"
            f"   - 商品推销、代购、微商 → 违规\n"
            f"   - 任何包含联系方式（QQ号、微信号、手机号）的推广内容 → 违规\n"
            f"   - 只有纯粹的资源分享（如\"推荐一部电影\"）且无任何引流意图 → 不违规\n\n"
            f"3. 色情类（porn）—— 识别真正的色情内容：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 明确的色情内容、招嫖信息\n"
            f"     * 发送色情图片/视频/链接\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 暧昧玩笑、两性话题讨论（只要不过于露骨）\n"
            f"     * 恋爱话题、情感倾诉\n\n"
            f"4. 暴恐/涉枪涉爆/贪腐类：\n"
            f"   - 明确的违法内容 → 违规\n"
            f"   - 游戏/影视/新闻讨论 → 不违规\n\n"
            f"5. 政治敏感类（political）—— 注意：该词库误报率很高，需严格区分：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 明确的颠覆国家政权言论（\"推翻政府\"、\"颠覆政权\"等）\n"
            f"     * 直接侮辱国家领导人（不是讨论政策，而是人身攻击）\n"
            f"     * 明确煽动分裂国家的言论\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 正常政治讨论、新闻评论\n"
            f"     * 游戏、影视中的政治元素讨论\n"
            f"     * 历史人物/事件的正常讨论\n\n"
            f"6. 违规网址类（illegal_url）—— 注意：误报率高：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 赌博、色情、诈骗网站\n"
            f"     * 恶意软件下载链接\n"
            f"   - 以下情况**不违规**：\n"
            f"     * 正常游戏攻略、教程链接\n"
            f"     * 视频网站链接（B站、YouTube等）\n"
            f"     * 工具软件官网\n\n"
            f"7. 隐私泄露类：\n"
            f"   - 以下情况**违规**：\n"
            f"     * 泄露他人身份证号、住址、电话\n"
            f"     * 人肉搜索、开盒行为\n"
            f"     * 公开他人私人信息\n\n"
            f"请严格按照以下JSON格式返回，不要返回其他内容：\n"
            f'{{"violation": true/false, "reason": "判断原因"}}\n\n'
            f"【被标记消息】\n"
            f"发送者: {user_name}\n"
            f"内容: {text}\n"
            f"可疑类型: {suspect_desc} ({suspect_tag})\n\n"
            f"【上下文消息】\n"
            f"{context_text}\n"
        )
        # system_prompt 较短，核心约束是"严格返回 JSON 格式"。
        system_prompt = (
            "你是一名群聊内容审核员。你的任务是判断被标记消息是否需要撤回。"
            "请结合上下文语境合理判断。返回严格的JSON格式。"
        )

        try:
            # 使用信号量（_llm_semaphore）控制并发，避免同一时间大量 LLM 请求打爆 API。
            async with self._llm_semaphore:
                llm_response = await self._call_llm_safe(system_prompt, prompt)

            # ---------- JSON 响应解析 ----------
            # LLM 回复中可能包含额外的解释文字，需用正则提取 JSON 部分。
            # 优先匹配包含 "violation" 键的 JSON 对象，确保结构正确；
            # 若找不到则回退到匹配任意 {...}，增加容错性。
            json_match = re.search(r'\{[^{}]*"violation"[^{}]*\}', llm_response, re.DOTALL)
            if not json_match:
                json_match = re.search(r'\{.*\}', llm_response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                return result
            else:
                # LLM 完全没有返回 JSON 格式，可能是模型不兼容或提示词被忽略。
                logger.warning(f"[GroupMgr] LLM返回非JSON格式: {llm_response[:200]}")
                return {"violation": False, "reason": "LLM返回格式异常"}
        except json.JSONDecodeError as e:
            # 匹配到了类似 JSON 的文本但解析失败（如括号不配对、非法字符等）。
            logger.warning(f"[GroupMgr] LLM返回JSON解析失败: {e}")
            return {"violation": False, "reason": "JSON解析失败"}
        except Exception as e:
            # LLM 调用本身出错（Provider 不可用、网络错误、超时等）。
            # 默认不违规（宁可放过不可错杀）。
            logger.warning(f"[GroupMgr] LLM审核调用失败: {e}")
            return {"violation": False, "reason": f"LLM调用失败: {str(e)[:100]}"}

    def _is_ad_pattern(self, text: str) -> bool:
        # 使用预编译的广告正则列表（_compiled_ad）对文本进行快速初筛。
        # 正则来源在配置文件/字典中定义，覆盖微信号、QQ号、手机号、引流话术等常见广告模式。
        if not text:
            return False
        for p in self._compiled_ad:
            m = p.search(text)
            if m:
                logger.debug(f"[GroupMgr] 正则广告命中: {m.group()}")
                return True
        return False

    def _should_scan_message(self, event: AiocqhttpMessageEvent) -> bool:
        # 判断消息是否需要进行审核扫描。
        # 仅当消息包含以下至少一种 CQ 码段类型时返回 True：
        #   text(文本)、forward(合并转发)、image(图片)、market_face(商城表情)、
        #   json(JSON卡片)、app(应用消息)
        # 同时排除匿名消息（anonymous）和通知类消息（notice），这两类消息无审核意义。
        sub_type = ''
        raw = getattr(event, 'raw_event', None)
        if isinstance(raw, dict):
            sub_type = str(raw.get('sub_type', '')).lower()
        if sub_type in ('anonymous', 'notice'):
            return False
        chain = event.get_messages()
        for seg in (chain or []):
            # 兼容两种消息段格式：AstrBot 的 dict 格式和 go-cqhttp 的 MessageSegment 对象格式。
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                if seg_type == 'text' and seg.get('data', {}).get('text', '').strip():
                    return True
                if seg_type == 'forward':
                    return True
                if seg_type == 'image':
                    return True
                if seg_type == 'market_face':
                    return True
            else:
                seg_cls = type(seg).__name__
                if seg_cls == 'Plain' and getattr(seg, 'text', '').strip():
                    return True
                if seg_cls == 'Forward' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'forward'):
                    return True
                if seg_cls == 'Image' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'image'):
                    return True
                if seg_cls == 'MarketFace' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'market_face'):
                    return True
                if seg_cls == 'Json' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'json'):
                    return True
                if seg_cls == 'App' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'app'):
                    return True
        return False

    async def _resolve_forward_messages(self, event: AiocqhttpMessageEvent) -> Tuple[str, bool]:
        # 解析合并转发（Forward）消息中的每条单条消息内容，将所有文字拼合为一段文本。
        # 一次转发可能包含多条消息、多种 CQ 码类型（text/image/json/app 等），
        # 需要递归遍历并提取其中的文字部分。
        # 返回格式："[转发]发送者昵称: 消息内容"
        # 第二个返回值 is_qq_favorite 标记是否包含 QQ 收藏来源的内容。
        client = await self._get_client(event)
        if not client:
            return "", False
        chain = event.get_messages() or []
        forward_ids = []
        for seg in chain:
            # 从消息段中提取 forward 类型段的 id，兼容 dict 和对象两种格式。
            if isinstance(seg, dict) and seg.get('type') == 'forward':
                fid = seg.get('data', {}).get('id', '')
                if fid:
                    forward_ids.append(fid)
            else:
                seg_cls = type(seg).__name__
                if seg_cls == 'Forward' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'forward'):
                    fid = ''
                    if hasattr(seg, 'id'):
                        fid = getattr(seg, 'id', '')
                    elif hasattr(seg, 'data') and isinstance(getattr(seg, 'data', None), dict):
                        fid = getattr(seg, 'data', {}).get('id', '')
                    if fid:
                        forward_ids.append(fid)
        if not forward_ids:
            return "", False
        all_texts = []
        is_qq_favorite = False
        for fid in forward_ids:
            try:
                # 调用 go-cqhttp 的 get_forward_msg API 获取合并转发的详细内容。
                result = await client.call_action('get_forward_msg', message_id=fid)
                result = self._extract_data_result(result)
                if not isinstance(result, dict):
                    continue
                messages = result.get('messages', []) or result.get('message', [])
                if isinstance(messages, dict):
                    messages = messages.get('message', [])
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    sender = msg.get('sender') or {}
                    nickname = sender.get('nickname', '未知') if isinstance(sender, dict) else '未知'
                    card = sender.get('card', '') if isinstance(sender, dict) else ''
                    content = msg.get('message', '')
                    if isinstance(content, list):
                        # 逐条解析转发内的 CQ 码段：text 提取文本，image 标记为[图片]，
                        # forward 标记为[嵌套转发]（递归不支持，仅标记），
                        # json/app 额外检查是否包含 QQ 收藏特征。
                        parts = []
                        for c_seg in content:
                            if isinstance(c_seg, dict):
                                ct = c_seg.get('type', '')
                                cd = c_seg.get('data', {}) or {}
                                if ct == 'text':
                                    text_val = cd.get('text', '')
                                    parts.append(text_val)
                                    if self._is_qq_favorite_text(text_val):
                                        is_qq_favorite = True
                                elif ct == 'image':
                                    parts.append('[图片]')
                                elif ct == 'forward':
                                    parts.append('[嵌套转发]')
                                elif ct == 'json':
                                    json_data = cd.get('data', '')
                                    if self._is_qq_favorite_text(json_data if isinstance(json_data, str) else str(json_data)):
                                        is_qq_favorite = True
                                    parts.append(f'[{ct}]')
                                elif ct == 'app':
                                    app_content = cd.get('content', '')
                                    if self._is_qq_favorite_text(app_content if isinstance(app_content, str) else str(app_content)):
                                        is_qq_favorite = True
                                    parts.append(f'[{ct}]')
                                else:
                                    parts.append(f'[{ct}]')
                                if not is_qq_favorite and self._check_dict_seg_qq_favorite(c_seg):
                                    is_qq_favorite = True
                            else:
                                parts.append(str(c_seg))
                        content_text = ''.join(parts)
                    else:
                        content_text = str(content)
                        if self._is_qq_favorite_text(content_text):
                            is_qq_favorite = True
                    if content_text.strip():
                        all_texts.append(f"[转发]{nickname}: {content_text.strip()}")
            except Exception as e:
                logger.debug(f"[GroupMgr] 获取转发消息内容失败: {e}")
                all_texts.append("[转发消息获取失败]")
        return '\n'.join(all_texts), is_qq_favorite

    @staticmethod
    def _is_qq_favorite_text(text: str) -> bool:
        # 判断文本中是否包含 QQ 收藏相关的特征字符串。
        # QQ 收藏消息在转发和 JSON 卡片中通常包含 "QQ收藏"、".qq.com/share/" 等特征。
        if not isinstance(text, str):
            return False
        return 'QQ收藏' in text or 'qq收藏' in text.lower() or 'sharechain.qq.com' in text

    @staticmethod
    def _check_dict_seg_qq_favorite(seg: dict) -> bool:
        # 对单个 CQ 码段的 dict 表示，检查 json/app 类型中是否包含 QQ 收藏特征。
        if not isinstance(seg, dict):
            return False
        seg_type = seg.get('type', '')
        seg_data = seg.get('data', {}) or {}
        if seg_type == 'json':
            return ModerationMixin._is_qq_favorite_text(seg_data.get('data', ''))
        if seg_type == 'app':
            return ModerationMixin._is_qq_favorite_text(seg_data.get('content', ''))
        return False

    async def _check_qq_favorite_non_forward(self, event: AiocqhttpMessageEvent) -> bool:
        # 在非转发消息中检查是否包含 QQ 收藏特征。
        # 有些 QQ 收藏消息以独立的 json/app CQ 码段发送（而非包装在 forward 中），
        # 需要额外扫描 raw_event 的 message 原始列表和 chain 中的 Json/App 段。
        raw = getattr(event, 'raw_event', None)
        chain = event.get_messages() or []
        if isinstance(raw, dict):
            msg_list = raw.get('message', [])
            if isinstance(msg_list, list):
                for seg in msg_list:
                    if self._check_dict_seg_qq_favorite(seg):
                        return True
        for seg in chain:
            if isinstance(seg, dict):
                continue
            seg_cls = type(seg).__name__
            if seg_cls in ('Json',) or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'json'):
                json_data = getattr(seg, 'data', '') or ''
                if self._is_qq_favorite_text(json_data):
                    return True
                if isinstance(json_data, dict) and self._is_qq_favorite_text(str(json_data)):
                    return True
            elif seg_cls in ('App',) or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'app'):
                if self._is_qq_favorite_text(getattr(seg, 'content', '')):
                    return True
        return False

    @staticmethod
    def _is_gif_url(url: str) -> bool:
        # 判断图片 URL 是否为 GIF 动图。
        # 检测规则：URL 以 .gif 结尾，或包含 .gif? / .gif; 查询参数。
        if not url:
            return False
        lower = url.lower()
        if lower.endswith('.gif'):
            return True
        if '.gif?' in lower or '.gif;' in lower:
            return True
        return False

    @staticmethod
    def _is_sticker_image(url: str) -> bool:
        # 判断图片 URL 是否为表情包/贴纸图。
        # 检测特征：URL 中包含 sticker / emoji / marketface / emoticon 等关键词，
        # 或包含 /face/ 路径段/查询参数（go-cqhttp 的表情图片常见格式）。
        # 表情包通常不需要 OCR（内容多为表情而非文字），
        # 若 scan_sticker_enabled 关闭则跳过 OCR。
        if not url:
            return False
        lower = url.lower()
        sticker_markers = ['sticker', 'emoji', 'marketface', 'emoticon']
        if any(m in lower for m in sticker_markers):
            return True
        if '/face/' in lower or '/face?' in lower or '&face=' in lower or '?face=' in lower:
            return True
        return False

    async def _ocr_images(self, event: AiocqhttpMessageEvent, image_urls: list) -> str:
        # 对图片列表逐一执行 OCR 识别（上限 3 张，避免 token 消耗过大）。
        # 对每张图片调用 _call_llm_ocr，在其返回前附加 gif/表情包 的前缀标记，
        # 方便 LLM 审核心 `_call_llm_for_moderation` 中的 prompt 理解上下文。
        # 所有 OCR 结果用换行拼接后返回。
        if not image_urls:
            return ""
        all_ocr_texts = []
        for img_url in image_urls[:3]:
            try:
                is_gif = self._is_gif_url(img_url)
                is_sticker = self._is_sticker_image(img_url)
                ocr_text = await self._call_llm_ocr(img_url, is_gif=is_gif, is_sticker=is_sticker)
                if ocr_text and ocr_text.strip():
                    prefix = ""
                    if is_gif:
                        prefix = "[GIF动图] "
                    elif is_sticker:
                        prefix = "[表情包] "
                    all_ocr_texts.append(prefix + ocr_text.strip())
            except Exception as e:
                logger.debug(f"[GroupMgr] OCR识别失败: {e}")
        return '\n'.join(all_ocr_texts)

    async def _call_llm_ocr(self, image_url: str, is_gif: bool = False, is_sticker: bool = False) -> str:
        # 调用 LLM 的视觉能力对单张图片进行 OCR 识别。
        # 视觉识别需要 LLM Provider 支持多模态（如 GPT-4V、Qwen-VL 等），
        # 用户需要在配置中指定 ocr_provider_id 并确保该 Model/Provider 支持 image_urls 参数。
        # 流程：
        #   1) 从配置中读取 OCR 专用 Provider ID，若为空则直接返回（OCR 功能未启用）。
        #   2) 从配置中获取模板选择（default/strict/text_only）或自定义提示词。
        #   3) 若为 GIF/表情包，在 prompt 末尾追加特殊说明（动图多帧、表情包文字等）。
        #   4) 尝试两种调用方式：
        #        a) context.llm_generate() —— 传递 image_urls 参数的多模态生成 API。
        #        b) prov.text_chat() —— 先尝试带 image_urls 命名参数的版本，失败则尝试
        #           将图片 URL 拼接在 prompt 文本中的降级方案（兼容不支持 image_urls 参数的 Provider）。
        configured_id = str(self.config.get("ocr_provider_id", "")).strip()
        if not configured_id:
            return ""

        # 优先使用用户自定义的提示词（ocr_custom_system_prompt + ocr_custom_user_prompt），
        # 若未设置则从预置模板 _OCR_PROMPT_TEMPLATES 中根据 ocr_prompt_template 配置项选取。
        template_key = str(self.config.get("ocr_prompt_template", "default")).strip()
        custom_system = str(self.config.get("ocr_custom_system_prompt", "")).strip()
        custom_user = str(self.config.get("ocr_custom_user_prompt", "")).strip()

        if custom_system and custom_user:
            system_prompt = custom_system
            prompt = custom_user
        else:
            template = self._OCR_PROMPT_TEMPLATES.get(template_key, self._OCR_PROMPT_TEMPLATES["default"])
            system_prompt = template["system"]
            prompt = template["prompt"]

        if is_gif:
            prompt += "\n注意：这是一张GIF动图，可能包含多帧内容。请仔细观察每一帧，描述所有帧中出现的内容和文字，特别关注是否有违规内容在动画帧中出现。"
        elif is_sticker:
            prompt += "\n注意：这是一个表情包/贴纸图片。表情包中常包含文字，请完整转录表情包中的所有文字，并判断文字内容是否违规（如侮辱性脏话、广告推广等）。"

        try:
            # 方式 A：通过 context.llm_generate() 多模态接口调用。
            if hasattr(self.context, 'llm_generate'):
                kwargs = {
                    'prompt': prompt,
                    'system_prompt': system_prompt,
                    'image_urls': [image_url],
                    'chat_provider_id': configured_id,
                }
                try:
                    resp = await self.context.llm_generate(**kwargs)
                    if resp:
                        return self._extract_llm_text(resp)
                except TypeError:
                    pass

            # 方式 B：通过 provider.text_chat() 手动调用，兼容不支持多模态参数的 Provider。
            if hasattr(self.context, 'get_provider_by_id'):
                prov = self.context.get_provider_by_id(configured_id)
                if prov and hasattr(prov, 'text_chat'):
                    try:
                        r = await prov.text_chat(
                            system_prompt=system_prompt,
                            prompt=prompt,
                            image_urls=[image_url],
                        )
                        if r:
                            return str(r)
                    except TypeError:
                        pass
                    # 降级方案：将图片 URL 拼入 prompt 文本末尾，适合不支持 image_urls 命名参数的 Provider。
                    try:
                        r = await prov.text_chat(
                            system_prompt + "\n\n图片URL: " + image_url + "\n\n" + prompt,
                        )
                        if r:
                            return str(r)
                    except Exception as _e:
                        logger.debug(f"[GroupMgr] OCR LLM单次调用失败: {_e}")

            return ""
        except Exception as e:
            logger.debug(f"[GroupMgr] OCR LLM调用失败: {e}")
            return ""

    async def _handle_message(self, event: AiocqhttpMessageEvent):
        # ===== 审核管线的完整入口点 =====
        # 此方法由 main.py 注册为群消息事件处理器，每次群消息到来时调用。
        # 按以下流水线顺序执行：
        #
        # 步骤 A：群/用户级前置检查
        #   - 提取 group_id，跳过空 group_id
        #   - 黑名单群直接放行不处理
        #   - 白名单模式：不在白名单的群也放行
        #   - should_scan_message：跳过无有效内容的消息（纯系统消息、通知等）
        #   - enabled 配置开关 + disclaimer_agreed 免责声明确认
        #   - 管理员豁免：管理员不受审核
        #   - 用户黑名单：黑名单用户直接踢出+禁言
        #
        # 步骤 B：消息内容提取
        #   - 遍历 CQ 码段，提取文本（text）、图片 URL（image/market_face）、转发标记（forward）
        #   - 若有转发，调用 _resolve_forward_messages 提取转发内文字
        #   - 检查 QQ 收藏特征，若命中且 recall_qq_favorite_enabled 则直接撤回
        #
        # 步骤 C：OCR 图片识别（可选）
        #   - 仅当 ocr_enabled 且存在图片 URL 时执行
        #   - 若 scan_sticker_enabled=False 则跳过表情包类图片
        #   - OCR 结果拼入 text，格式为 "[OCR识图内容]\n<识别文本>"
        #
        # 步骤 D：正则/词库初筛
        #   - 脏话正则（_compiled_swear）
        #   - 广告正则（_is_ad_pattern）
        #   - 敏感词库_compiled_lexicon 的多分类匹配（政治/色情/暴恐等）
        #   - 若均未命中则直接跳过（无违规嫌疑）
        #
        # 步骤 E：LLM 二次审核（可选）
        #   - 仅当 llm_moderation_enabled 时调用
        #   - 携带 30 条上下文 + 初筛的命中类型标签
        #   - LLM 返回 JSON 含 violation bool + reason 字符串
        #
        # 步骤 F：违规处理
        #   - LLM 判定违规 → 撤回消息 + 可选禁言 + 发送通知 + 记录日志
        #   - LLM 判定不违规 → 写日志放行（便于后续排查误报）
        #   - LLM 不可用 → 直接按正则命中判定结果执行撤回+禁言

        group_id = self._get_group_id(event)
        if not group_id:
            return
        # 群黑名单：在黑名单中的群完全不处理（与白名单的"不处理白名单外的群"逻辑对应）。
        if self._group_black_set and group_id in self._group_black_set:
            return
        # 群白名单：如果启用了白名单模式，只有白名单中的群接受审核。
        if self._group_white_set and group_id not in self._group_white_set:
            return
        if not self._should_scan_message(event):
            return
        if not self._cfg("enabled"):
            return
        if not self.config.get("disclaimer_agreed", False):
            return
        # 管理员完全豁免审核（包括黑名单检查），防止误处理管理员。
        if await self._is_admin(event):
            return
        # 用户黑名单：直接踢出+禁言，不经过审核流程。
        if self._user_black_set:
            user_id = self._try_get_sender_id(event)
            if user_id and user_id in self._user_black_set:
                try:
                    await self._kick_member(event)
                    await self._mute_member(event, 60)
                    notice = self.config.get("ban_notice", "[群管] {name}({uid}) 已被踢出（黑名单）")
                    yield event.plain_result(notice.replace("{name}", event.get_sender_name()).replace("{uid}", user_id).replace("{group}", group_id))
                    event.stop_event()
                except Exception as e:
                    logger.warning(f"[GroupMgr] 黑名单执行出错: {e}")
                return

        # ===== 步骤 B：消息内容提取 =====
        chain = event.get_messages()
        raw_text_parts = []
        image_urls = []
        has_forward = False
        for seg in (chain or []):
            # 同时兼容 dict 格式和 MessageSegment 对象格式。
            if isinstance(seg, dict):
                seg_type = seg.get('type', '')
                seg_data = seg.get('data', {}) or {}
                if seg_type == 'text':
                    raw_text_parts.append(seg_data.get('text', ''))
                elif seg_type == 'forward':
                    has_forward = True
                elif seg_type == 'image':
                    img_url = seg_data.get('url', '') or seg_data.get('file', '')
                    if img_url:
                        image_urls.append(img_url)
                elif seg_type == 'market_face':
                    mf_url = seg_data.get('url', '') or ''
                    if mf_url:
                        image_urls.append(mf_url)
            else:
                seg_cls = type(seg).__name__
                if seg_cls == 'Plain' or hasattr(seg, 'text'):
                    raw_text_parts.append(getattr(seg, 'text', '') or '')
                elif seg_cls == 'Forward' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'forward'):
                    has_forward = True
                elif seg_cls == 'Image' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'image'):
                    img_url = getattr(seg, 'url', '') or getattr(seg, 'file', '') or ''
                    if not img_url and hasattr(seg, 'data'):
                        seg_data = getattr(seg, 'data', {})
                        if isinstance(seg_data, dict):
                            img_url = seg_data.get('url', '') or seg_data.get('file', '')
                    if img_url:
                        image_urls.append(img_url)
                    else:
                        logger.debug(f"[GroupMgr] Image段无URL: {seg}")
                elif seg_cls == 'MarketFace' or (hasattr(seg, 'type') and getattr(seg, 'type', '') == 'market_face'):
                    mf_url = getattr(seg, 'url', '') or ''
                    if not mf_url and hasattr(seg, 'data'):
                        seg_data = getattr(seg, 'data', {})
                        if isinstance(seg_data, dict):
                            mf_url = seg_data.get('url', '') or ''
                    if mf_url:
                        image_urls.append(mf_url)
        text = ''.join(raw_text_parts).strip()

        # 解析合并转发消息中的文字内容。
        forward_text = ""
        forward_is_qq_favorite = False
        if has_forward:
            forward_text, forward_is_qq_favorite = await self._resolve_forward_messages(event)
            scan_forward = self._cfg("scan_forward_msg", True)
            if forward_text and scan_forward:
                if text:
                    text = text + '\n' + forward_text
                else:
                    text = forward_text

        # QQ 收藏消息检测：如果消息内容包含 QQ 收藏特征，直接撤回（不经过正常审核）。
        if self._cfg("recall_qq_favorite_enabled", True):
            is_qq_fav = forward_is_qq_favorite or await self._check_qq_favorite_non_forward(event)
            if is_qq_fav:
                try:
                    msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
                    if msg_id:
                        await self._recall_msg(event, msg_id)
                        user_name = event.get_sender_name()
                        user_id = self._try_get_sender_id(event)
                        yield event.plain_result(f"[群管] 检测到QQ收藏内容，已自动撤回")
                        self._log_moderation(group_id, user_id, user_name, "[QQ收藏消息]", "撤回", "QQ收藏内容自动撤回", image_urls)
                        event.stop_event()
                except Exception as e:
                    logger.warning(f"[GroupMgr] QQ收藏撤回失败: {e}")
                return

        if not self.auto_moderate_enabled:
            return

        # ===== 步骤 C：OCR 图片识别 =====
        if image_urls and self._cfg("ocr_enabled", False):
            ocr_urls = image_urls
            if not self._cfg("scan_sticker_enabled", True):
                # 如果配置了不扫描表情包，过滤掉表情包类图片。
                ocr_urls = [u for u in image_urls if not self._is_sticker_image(u)]
            if ocr_urls:
                logger.info(f"[GroupMgr] OCR开始识别 {len(ocr_urls)} 张图片")
                ocr_text = await self._ocr_images(event, ocr_urls)
                if ocr_text:
                    if text:
                        text = text + '\n[OCR识图内容]\n' + ocr_text
                    else:
                        text = '[OCR识图内容]\n' + ocr_text
                    logger.info(f"[GroupMgr] OCR识别结果: {ocr_text[:100]}")
                else:
                    logger.debug(f"[GroupMgr] OCR识别返回空结果")

        # 纯图片无文字且 OCR 未产生结果 → 跳过后续审核。
        if not text:
            if image_urls:
                logger.debug(f"[GroupMgr] 图片消息无文字且OCR未生效，跳过审核")
            return
        if len(text) > 5000:
            text = text[:5000]
        user_id = self._try_get_sender_id(event)
        user_name = event.get_sender_name()

        # ===== 步骤 D：正则/词库初筛 =====
        hit_types = {
            "swear": False,
            "ad": False,
            "political": False,
            "porn": False,
            "violent_terror": False,
            "reactionary": False,
            "weapons": False,
            "corruption": False,
            "illegal_url": False,
            "other": False,
        }

        swear_hit = False
        if self._cfg("scan_swear", True):
            for p in self._compiled_swear:
                m = p.search(text)
                if m:
                    logger.info(f"[GroupMgr] 正则脏话命中: {m.group()}")
                    swear_hit = True
                    break
        hit_types["swear"] = swear_hit

        ad_hit = False
        if self._cfg("scan_ad", True):
            ad_hit = self._is_ad_pattern(text)
        hit_types["ad"] = ad_hit

        lexicon_result = self._check_lexicon(text)
        for cat, hit in lexicon_result.items():
            if cat in hit_types:
                hit_types[cat] = hit_types[cat] or hit

        should_check = any(hit_types.values())
        if not should_check:
            return

        # ===== 步骤 E：LLM 二次审核 =====
        llm_enabled = self._cfg("llm_moderation_enabled", True)

        if not llm_enabled:
            # LLM 审核关闭时：直接按正则命中执行撤回+禁言。
            reason = "触发规则: " + ", ".join(k for k, v in hit_types.items() if v)
            logger.info(f"[GroupMgr] {user_name}({user_id}) in {group_id} -> {reason}")
            try:
                msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
                await self._recall_msg(event, msg_id)
                await self._mute_member(event)
                notice = self.config.get("ban_notice", "[群管] {name}({uid}) 已被禁言（触发规则）")
                yield event.plain_result(notice.replace("{name}", user_name).replace("{uid}", user_id).replace("{group}", group_id))
                self._log_moderation(group_id, user_id, user_name, text, "撤回+禁言", reason, image_urls)
                event.stop_event()
            except Exception as e:
                logger.warning(f"[GroupMgr] 自动审核出错: {e}")
            return

        logger.debug(f"[GroupMgr] 开始调用LLM审核...")
        llm_result = await self._call_llm_for_moderation(event, text, hit_types, group_id=group_id)
        logger.debug(f"[GroupMgr] LLM返回结果: {llm_result}")
        is_violation = llm_result.get("violation", False)
        reason = llm_result.get("reason", "无理由")

        # ===== 步骤 F：违规处理 =====
        hit_summary = ', '.join(k for k, v in hit_types.items() if v)
        if not is_violation:
            # LLM 判定不违规 → 记录日志放行（有助于后续排查误报和调优正则）。
            logger.info(f"[GroupMgr] LLM审核通过: {user_name}({user_id}) in {group_id} | 命中类型={{{hit_summary}}} | 原因={reason}")
            self._log_moderation(group_id, user_id, user_name, text, "LLM放行", reason, image_urls)
            return

        # LLM 判定违规 → 执行撤回、禁言、通知、记录。
        logger.info(f"[GroupMgr] LLM审核拦截: {user_name}({user_id}) in {group_id} | 命中类型={{{hit_summary}}} | 原因={reason}")

        try:
            msg_id = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
            if msg_id:
                try:
                    await self._recall_msg(event, msg_id)
                except Exception as recall_err:
                    logger.warning(f"[GroupMgr] 撤回消息失败: {recall_err}")

            if self._cfg("llm_moderation_ban", True):
                try:
                    await self._mute_member(event)
                except Exception as ban_err:
                    logger.warning(f"[GroupMgr] 禁言失败: {ban_err}")

            if self._cfg("auto_moderate_notice", True):
                try:
                    notice = self.config.get("ban_notice", "[群管] {name}({uid}) 的消息已被撤回（违规内容）")
                    yield event.plain_result(notice.replace("{name}", user_name).replace("{uid}", user_id).replace("{group}", group_id))
                except Exception as notice_err:
                    logger.warning(f"[GroupMgr] 发送通知失败: {notice_err}")

            self._log_moderation(group_id, user_id, user_name, text, "LLM撤回", reason, image_urls)
            event.stop_event()
        except Exception as e:
            logger.warning(f"[GroupMgr] 自动审核出错: {e}")
            yield event.plain_result(f"[群管] 审核出错: {str(e)[:100]}")
