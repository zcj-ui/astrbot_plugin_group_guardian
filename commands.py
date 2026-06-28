# -*- coding: utf-8 -*-
import asyncio
import time
from typing import Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class CommandsMixin:
    # AstrBot 命令 handler 统一使用 async generator 模式：通过 yield event.plain_result() 发送回复。
    # 每个 handler 的第一步都是调用 _check_admin_cfg_access 或 _cfg_check 做功能开关 + 权限校验。
    # 需要调用 QQ API 时通过 _get_group_client 获取客户端，它在 main.py 初始化时注入。

    async def word_count(self, event: AstrMessageEvent):
        '''统计群内关键词出现次数'''
        # 拆分命令参数：/字数统计 <关键词> [天数] [类型]
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /字数统计 <关键词> [天数] [类型]\n类型: 脏话/广告/敏感词/黑名单\n示例: /字数统计 傻逼 7 脏话")
            return
        keyword = args[1]
        days = 7
        search_type = "all"
        # 将中文类型名称映射为内部枚举值，便于 _search_keyword_in_messages 做专项匹配
        type_map = {"脏话": "swear", "广告": "ad", "敏感词": "sensitive", "黑名单": "black"}
        # 第三个参数可能是天数也可能是类型名，用 int() 尝试解析来区分
        if len(args) >= 3:
            try:
                days = int(args[2])
            except ValueError:
                search_type = type_map.get(args[2], args[2].lower())
        if len(args) >= 4:
            search_type = type_map.get(args[3], args[3].lower())
        # 约束天数范围 1-90 天，防止过大的查询压力
        days = max(1, min(days, 90))
        try:
            # need_admin=False：字数统计允许普通成员使用
            ok, err, client, gid = await self._prepare_group_action(event, "word_count_enabled", "字数统计", need_admin=False)
            if not ok:
                yield event.plain_result(err)
                return
            count, sample_messages = await self._search_keyword_in_messages(event, str(gid), keyword, days, search_type)
            if count == 0:
                yield event.plain_result(f"最近 {days} 天内未找到包含「{keyword}」的消息")
            else:
                result = f"最近 {days} 天内「{keyword}」出现次数: {count}\n"
                # 附带最近几条匹配消息作为示例，帮助用户确认匹配质量
                if sample_messages:
                    result += "\n最近消息:\n"
                    for msg in sample_messages[:5]:
                        result += f"  {msg}\n"
                yield event.plain_result(result)
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    async def _search_keyword_in_messages(self, event: AstrMessageEvent, group_id: str, keyword: str, days: int, search_type: str = "all") -> Tuple[int, list]:
        # 内部辅助方法：从群历史消息中检索关键词，返回 (匹配次数, 示例消息列表)
        # 使用 _get_client（非 _get_group_client），因为它只需 client 而不需要群 ID
        client = await self._get_client(event)
        if not client:
            return 0, []
        try:
            # 调用 OneBot get_group_msg_history 拉取最近 100 条消息（大部分 OneBot 实现支持）
            # _safe_int 用于防止 group_id 字符串转换为 int 时抛出异常
            result = await client.call_action('get_group_msg_history', group_id=self._safe_int(group_id, 0), count=100)
            # _extract_data_result: 统一处理 OneBot API 的 data 字段嵌套（如 {status:ok, data:{...}}）
            result = self._extract_data_result(result)
            messages = result.get('messages', []) if isinstance(result, dict) else []
        except Exception as e:
            logger.warning(f"[GroupMgr] 获取历史消息失败: {e}")
            return 0, []
        now = int(time.time())
        cutoff = now - days * 24 * 3600
        count = 0
        sample_messages = []
        for msg in messages:
            try:
                msg_time = msg.get('time', 0)
                # 跳过超出时间范围的消息
                if msg_time < cutoff:
                    continue
                raw_message = msg.get('message', '')
                # _format_message_content: 将 OneBot 消息数组（CQ 码）转换为纯文本
                text = self._format_message_content(raw_message)
                # 大小写不敏感匹配关键词
                if keyword.lower() in text.lower():
                    # 如果指定了过滤类型，需要进一步按类型规则判断该消息是否真的属于该类别
                    if search_type != "all":
                        is_match = False
                        if search_type == "swear":
                            is_match = self._swear_matcher.is_match(text) if hasattr(self, '_swear_matcher') else False
                        elif search_type == "ad":
                            is_match = self._is_ad_pattern(text)
                        elif search_type == "sensitive":
                            # 用 AC 自动机扫描 political 分类词库
                            ac = self._compiled_lexicon.get("political")
                            is_match = ac.exists(text) if ac else False
                        elif search_type == "black":
                            # 黑名单类型：检查消息发送者的 QQ 号是否在 _user_black_set 中
                            sender = msg.get('sender') or {}
                            uid = str(sender.get('user_id', ''))
                            is_match = uid in self._user_black_set
                        if not is_match:
                            continue
                    count += 1
                    sender = msg.get('sender') or {}
                    nickname = sender.get('nickname', '未知')
                    # 每条示例消息截取前 50 字符，避免输出过长
                    sample_messages.append(f"{nickname}: {text[:50]}")
            except Exception:
                # 单条消息解析失败不影响整体统计
                continue
        return count, sample_messages

    async def group_stats(self, event: AstrMessageEvent):
        '''显示群内今日消息统计和活跃排行'''
        try:
            # need_admin=False：允许普通成员查看群统计
            ok, err, client, gid = await self._prepare_group_action(event, "group_stats_enabled", "群统计", need_admin=False)
            if not ok:
                yield event.plain_result(err)
                return
            # 调用 OneBot get_group_member_list 获取群成员列表，用于统计分析
            result = await client.call_action('get_group_member_list', group_id=gid)
            # _extract_list_result: 从 OneBot 响应中提取列表（兼容 data 嵌套和直接返回列表两种格式）
            members = self._extract_list_result(result)
            total = len(members)
            # 统计各类角色的数量：owner（群主）、admin（管理员）、剩余为普通成员
            admins = sum(1 for m in members if m.get('role') in ('admin', 'owner'))
            owners = sum(1 for m in members if m.get('role') == 'owner')
            regular = total - admins
            stats = (
                f"群 {gid} 统计:\n"
                f"  群主: {owners}人\n"
                f"  管理员: {admins - owners}人\n"
                f"  普通成员: {regular}人\n"
                f"  总计: {total}人"
            )
            yield event.plain_result(stats)
        except Exception as e:
            yield event.plain_result(f"获取统计失败: {e}")

    async def search_member(self, event: AstrMessageEvent):
        '''按昵称或QQ号搜索群成员'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /搜索成员 <关键词>")
            return
        keyword = args[1]
        try:
            ok, err, client, gid = await self._prepare_group_action(event, "member_list_enabled", "查看群成员")
            if not ok:
                yield event.plain_result(err)
                return
            # 获取全量群成员列表，在本地做模糊匹配（不依赖 OneBot 搜索 API，因为大部分实现不支持）
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = self._extract_list_result(result)
            matched = []
            for m in members:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                uid = str(m.get("user_id", ""))
                # 支持按群名片、昵称（不区分大小写）或 QQ 号精确匹配三种方式
                if keyword.lower() in card.lower() or keyword.lower() in nickname.lower() or keyword in uid:
                    matched.append(m)
            if not matched:
                yield event.plain_result(f"未找到匹配「{keyword}」的成员")
            else:
                result_text = f"找到 {len(matched)} 个匹配成员:\n"
                # 最多展示 20 个结果，避免刷屏
                for m in matched[:20]:
                    card = m.get("card", "")
                    nickname = m.get("nickname", "")
                    name = card if card else nickname
                    role = m.get("role", "member")
                    # 将角色英文值翻译为中文显示
                    role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                    result_text += f"  {name}({m.get('user_id')}) [{role_text}]\n"
                yield event.plain_result(result_text.strip())
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    async def recall_last(self, event: AstrMessageEvent):
        '''撤回群内最新一条或多条消息'''
        ok, err, client, gid = await self._prepare_group_action(event, "recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        count = self._safe_int(args[1], 1) if len(args) >= 2 else 1
        count = max(1, min(count, 10))
        try:
            # 拉取 count+1 条历史消息，确保即使最新消息是撤回目标也能获取到足够的历史
            result = await client.call_action('get_group_msg_history', group_id=gid, count=count + 1)
            result = self._extract_data_result(result)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            # 取最后 count 条消息（最新的消息在列表末尾）
            for msg in messages[-count:]:
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        # 调用 OneBot delete_msg 逐条撤回
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                        # 每条撤回之间休眠 0.5 秒，防止 API 频率限制
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.debug(f"[GroupMgr] 撤回消息{msg_id}失败: {e}")
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    async def cmd_ban(self, event: AstrMessageEvent):
        '''禁言指定群成员。用法: /禁言 <QQ号或@某人> <分钟>'''
        args = event.message_str.split()
        at_targets = self._extract_at_targets(event)
        if len(args) < 2 and not at_targets:
            yield event.plain_result("用法: /禁言 <QQ号或@某人> [时长(分钟)]\n示例: /禁言 123456 30 或 /禁言 @张三 30")
            return
        try:
            user_id = at_targets[0] if at_targets else str(args[1]).strip()
            raw_min = args[2] if len(args) > 2 else (args[1] if at_targets and len(args) > 1 else None)
            minutes = min(max(self._safe_int(raw_min, 10), 1), 43200)
            duration = minutes * 60
            ok, err, client, gid, uid = await self._prepare_group_member_action(event, "ban_enabled", "禁言", user_id, precheck_action="ban")
            if not ok:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_ban', "禁言", group_id=gid, user_id=uid, duration=duration)
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            self._schedule_unban(str(gid), user_id, duration)
            yield event.plain_result(f"已禁言 {user_id}，时长 {minutes} 分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {e}")

    async def cmd_unban(self, event: AstrMessageEvent):
        '''解除指定群成员禁言。用法: /解禁 <QQ号或@某人>'''
        args = event.message_str.split()
        at_targets = self._extract_at_targets(event)
        if len(args) < 2 and not at_targets:
            yield event.plain_result("用法: /解禁 <QQ号或@某人>\n示例: /解禁 123456 或 /解禁 @张三")
            return
        try:
            user_id = at_targets[0] if at_targets else str(args[1]).strip()
            ok, err, client, gid, uid = await self._prepare_group_member_action(event, "unban_enabled", "解禁", user_id)
            if not ok:
                yield event.plain_result(err)
                return
            # 解禁即 duration=0 的 set_group_ban，OneBot 协议中 duration=0 表示解除禁言
            ok, err = await self._call_group_api(client, 'set_group_ban', "解禁", group_id=gid, user_id=uid, duration=0)
            if not ok:
                yield event.plain_result(f"解禁失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解禁失败: {e}")

    async def cmd_kick(self, event: AstrMessageEvent):
        '''将成员移出群聊。用法: /踢人 <QQ号或@某人>'''
        args = event.message_str.split()
        at_targets = self._extract_at_targets(event)
        if len(args) < 2 and not at_targets:
            yield event.plain_result("用法: /踢人 <QQ号或@某人>\n示例: /踢人 123456 或 /踢人 @张三")
            return
        try:
            user_id = at_targets[0] if at_targets else str(args[1]).strip()
            ok, err, client, gid, uid = await self._prepare_group_member_action(event, "kick_enabled", "踢人", user_id, precheck_action="kick")
            if not ok:
                yield event.plain_result(err)
                return
            # set_group_kick: OneBot 踢人 API，调用前 _check_admin_cfg_access 已确保操作者有权限
            recalled = 0
            group_id = str(gid)
            if self._cfg("kick_recall_enabled", False, group_id=group_id):
                recall_count = min(max(self._cfg_int("kick_recall_count", 10, group_id=group_id), 1), 50)
                try:
                    result = await client.call_action('get_group_msg_history', group_id=gid, count=100)
                    result = self._extract_data_result(result)
                    msgs = result.get('messages', []) if isinstance(result, dict) else []
                    for msg in msgs:
                        if recalled >= recall_count:
                            break
                        sender = msg.get('sender') or {}
                        if str(sender.get('user_id', '')) == str(user_id):
                            mid = msg.get('message_id')
                            if mid:
                                try:
                                    await client.call_action('delete_msg', message_id=mid)
                                    recalled += 1
                                    await asyncio.sleep(0.3)
                                except Exception:
                                    pass
                except Exception as e:
                    logger.debug(f"[GroupMgr] 踢人撤回消息失败: {e}")
            ok, err = await self._call_group_api(client, 'set_group_kick', "踢人", group_id=gid, user_id=uid)
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            msg = f"已将 {user_id} 踢出群聊"
            if recalled > 0:
                msg += f"，已撤回 {recalled} 条消息"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

    async def cmd_whole_ban(self, event: AstrMessageEvent):
        '''开启或关闭全员禁言。用法: /全体禁言 开启/关闭'''
        args = event.message_str.split()
        enable = True
        if len(args) >= 2:
            action = args[1].strip()
            # 支持中英文和数字多种表示方式
            if action in ("关闭", "off", "0", "取消"):
                enable = False
        try:
            ok, err, client, gid = await self._prepare_group_action(event, "whole_ban_enabled", "全体禁言")
            if not ok:
                yield event.plain_result(err)
                return
            # set_group_whole_ban: OneBot 全体禁言 API，enable=True 开启、False 关闭
            ok, err = await self._call_group_api(client, 'set_group_whole_ban', "全体禁言", group_id=gid, enable=enable)
            if not ok:
                yield event.plain_result(f"操作失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")

    async def cmd_set_card(self, event: AstrMessageEvent):
        '''修改成员群名片。用法: /设置名片 <QQ号或@某人> <新名称>'''
        args = event.message_str.split()
        at_targets = self._extract_at_targets(event)
        if len(args) < 2 and not at_targets:
            yield event.plain_result("用法: /设置名片 <QQ号或@某人> <名片内容>\n示例: /设置名片 123456 管理员 或 /设置名片 @张三 管理员")
            return
        try:
            user_id = at_targets[0] if at_targets else str(args[1]).strip()
            card = ' '.join(args[2:]) if len(args) > 2 else (' '.join(args[1:]) if at_targets else '')
            ok, err, client, gid, uid = await self._prepare_group_member_action(event, "set_card_enabled", "设置名片", user_id)
            if not ok:
                yield event.plain_result(err)
                return
            # set_group_card: OneBot 设置群名片 API，card 为空字符串可清除名片
            ok, err = await self._call_group_api(client, 'set_group_card', "设置名片", group_id=gid, user_id=uid, card=card)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的群名片为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    async def cmd_send_notice(self, event: AstrMessageEvent):
        '''发布群公告。用法: /发公告 <内容>'''
        # 从消息文本中去除命令前缀，剩余部分作为公告内容
        content = event.message_str.replace("/发公告", "").strip()
        if not content:
            yield event.plain_result("用法: /发公告 <公告内容>")
            return
        try:
            ok, err, client, gid = await self._prepare_group_action(event, "send_announcement_enabled", "发公告")
            if not ok:
                yield event.plain_result(err)
                return
            # _send_group_notice: 下划线前缀表示 OneBot 扩展 API（非标准协议），不同实现可能有差异
            r = await client.call_action('_send_group_notice', group_id=gid, content=content)
            # _check_api_result: 解析 API 返回的 status/retcode 判断是否成功
            api_ok, err = self._check_api_result(r, "发公告")
            if not api_ok:
                yield event.plain_result(f"发送失败: {err}")
                return
            # 不同 OneBot 实现的返回字段名不同（notice_id 或 id），兼容处理
            notice_id = (r or {}).get("notice_id") or (r or {}).get("id") or ""
            yield event.plain_result(f"公告已发送{f'，ID: {notice_id}' if notice_id else ''}")
        except Exception as e:
            yield event.plain_result(f"发送失败: {e}")

    async def cmd_delete_notice(self, event: AstrMessageEvent):
        '''删除群公告。用法: /删公告 <公告ID>'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删公告 <公告ID>")
            return
        try:
            notice_id = str(args[1]).strip()
            ok, err, client, gid = await self._prepare_group_action(event, "delete_announcement_enabled", "删公告")
            if not ok:
                yield event.plain_result(err)
                return
            # _del_group_notice: OneBot 扩展 API，用于删除指定 ID 的群公告
            ok, err = await self._call_group_api(client, '_del_group_notice', "删公告", group_id=gid, notice_id=notice_id)
            if not ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    async def cmd_list_notices(self, event: AstrMessageEvent):
        '''查看群公告列表'''
        try:
            # need_admin=False：普通成员也可查看公告列表，但发布和删除需要管理员权限
            ok, err, client, gid = await self._prepare_group_action(event, "list_announcements_enabled", "公告列表", need_admin=False)
            if not ok:
                yield event.plain_result(err)
                return
            # _get_group_notice: OneBot 扩展 API，获取群公告列表
            result = await client.call_action('_get_group_notice', group_id=gid)
            notices = self._extract_list_result(result)
            if not notices:
                yield event.plain_result("暂无群公告")
                return
            lines = [f"📋 群公告列表 ({len(notices)}条):"]
            # 最多展示前 10 条，防止公告过多导致刷屏
            for n in notices[:10]:
                # 兼容不同 OneBot 实现的字段名差异
                nid = n.get("notice_id", n.get("id", ""))
                pub = n.get("publisher") or {}
                name = pub.get("nickname", "未知")
                title = n.get("title", n.get("content", ""))[:40]
                lines.append(f"  ID:{nid} | {name}: {title}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    async def cmd_list_files(self, event: AstrMessageEvent):
        '''查看群文件列表'''
        try:
            ok, err, client, gid = await self._prepare_group_action(event, "group_files_enabled", "群文件管理", need_admin=False)
            if not ok:
                yield event.plain_result(err)
                return
            # get_group_root_files: OneBot API，获取群根目录下的文件和文件夹列表
            result = await client.call_action('get_group_root_files', group_id=gid)
            result = self._extract_data_result(result)
            files = result.get("files", []) if isinstance(result, dict) else []
            folders = result.get("folders", []) if isinstance(result, dict) else []
            lines = [f"📁 群文件列表:"]
            for f in folders[:15]:
                lines.append(f"  📁 {f.get('folder_name', '?')}")
            for f in files[:15]:
                size = f.get('size', 0)
                # 将字节数转换为 KB/MB 等人类可读格式
                unit = "B"
                if size > 1024 * 1024:
                    size, unit = round(size / 1048576, 1), "MB"
                elif size > 1024:
                    size, unit = round(size / 1024, 1), "KB"
                lines.append(f"  📄 {f.get('file_name', '?')} ({size}{unit})")
            if not files and not folders:
                lines.append("  暂无文件")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    async def cmd_delete_file(self, event: AstrMessageEvent):
        '''删除群文件。用法: /删文件 <文件ID>'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删文件 <file_id>\n提示: 使用 /文件列表 查看 file_id")
            return
        try:
            file_id = str(args[1]).strip()
            ok, err, client, gid = await self._prepare_group_action(event, "group_files_enabled", "群文件管理")
            if not ok:
                yield event.plain_result(err)
                return
            # delete_group_file: OneBot API，busid=0 表示根目录下的文件
            ok, err = await self._call_group_api(client, 'delete_group_file', "删文件", group_id=gid, file_id=file_id, busid=0)
            if not ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除文件 {file_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    async def cmd_member_list(self, event: AstrMessageEvent):
        '''查看群成员列表'''
        try:
            ok, err, client, gid = await self._prepare_group_action(event, "member_list_enabled", "成员列表", need_admin=False)
            if not ok:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = self._extract_list_result(result)
            # 按角色分组计数：owner（群主）、admin（管理员）、member（普通成员）
            role_count = {"owner": 0, "admin": 0, "member": 0}
            for m in members:
                role = m.get("role", "member")
                role_count[role] = role_count.get(role, 0) + 1
            total = len(members)
            lines = [
                f"👥 群成员列表 ({total}人):",
                f"  👑 群主: {role_count['owner']}人",
                f"  ⭐ 管理员: {role_count['admin']}人",
                f"  👤 成员: {role_count['member']}人",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    async def cmd_banned_list(self, event: AstrMessageEvent):
        '''查看当前被禁言的成员'''
        try:
            ok, err, client, gid = await self._prepare_group_action(event, "banned_list_enabled", "禁言列表", need_admin=False)
            if not ok:
                yield event.plain_result(err)
                return
            # get_group_shut_list: OneBot API，获取当前被禁言的成员列表及其剩余时长
            result = await client.call_action('get_group_shut_list', group_id=gid)
            banned = self._extract_list_result(result)
            if not banned:
                yield event.plain_result("当前无人被禁言")
                return
            lines = [f"🚫 禁言列表 ({len(banned)}人):"]
            # 最多展示前 20 条，duration 单位是秒，除以 60 转为分钟显示
            for b in banned[:20]:
                uid = b.get("user_id", "?")
                dur = b.get("duration", 0)
                lines.append(f"  QQ: {uid}, 剩余: {dur // 60}分钟")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    async def cmd_set_name(self, event: AstrMessageEvent):
        '''修改群聊名称。用法: /群名 <新名称>'''
        name = event.message_str.replace("/群名", "").strip()
        if not name:
            yield event.plain_result("用法: /群名 <新群名>")
            return
        try:
            ok, err, client, gid = await self._prepare_group_action(event, "set_group_name_enabled", "修改群名")
            if not ok:
                yield event.plain_result(err)
                return
            # set_group_name: OneBot API，修改群名称
            ok, err = await self._call_group_api(client, 'set_group_name', "修改群名", group_id=gid, group_name=name)
            if not ok:
                yield event.plain_result(f"修改失败: {err}")
                return
            yield event.plain_result(f"群名已修改为: {name}")
        except Exception as e:
            yield event.plain_result(f"修改失败: {e}")

    async def cmd_set_title(self, event: AstrMessageEvent):
        '''设置成员专属头衔。用法: /头衔 <QQ号> <头衔名>'''
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("用法: /头衔 <QQ号> <头衔内容>\n示例: /头衔 123456 大佬")
            return
        try:
            user_id = str(args[1]).strip()
            title = ' '.join(args[2:])
            ok, err, client, gid, uid = await self._prepare_group_member_action(event, "set_title_enabled", "设置头衔", user_id)
            if not ok:
                yield event.plain_result(err)
                return
            # set_group_special_title: OneBot API，duration=-1 表示永久头衔
            ok, err = await self._call_group_api(client, 'set_group_special_title', "设置头衔", group_id=gid, user_id=uid, special_title=title, duration=-1)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的专属头衔: {title}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    async def cmd_set_essence(self, event: AstrMessageEvent):
        '''设置精华消息。用法: /设精华 <消息ID>'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /设精华 <message_id>\n回复消息或提供 message_id")
            return
        try:
            ok, err, client, msg_id = await self._prepare_message_action(event, "essence_enabled", "精华消息", args[1])
            if not ok:
                yield event.plain_result(err)
                return
            # set_essence_msg: OneBot API，将指定消息设为精华
            ok, err = await self._call_group_api(client, 'set_essence_msg', "设精华", message_id=msg_id)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设为精华消息 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    async def cmd_del_essence(self, event: AstrMessageEvent):
        '''取消精华消息。用法: /取消精华 <消息ID>'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /取消精华 <message_id>")
            return
        try:
            ok, err, client, msg_id = await self._prepare_message_action(event, "essence_enabled", "精华消息", args[1])
            if not ok:
                yield event.plain_result(err)
                return
            # delete_essence_msg: OneBot API，取消消息的精华状态
            ok, err = await self._call_group_api(client, 'delete_essence_msg', "取消精华", message_id=msg_id)
            if not ok:
                yield event.plain_result(f"取消失败: {err}")
                return
            yield event.plain_result(f"已取消精华 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"取消失败: {e}")

    async def cmd_set_admin(self, event: AstrMessageEvent):
        '''设置或取消群管理员。用法: /设置管理 @某人 或 <QQ号> [设置/取消]'''
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("请在群内使用此命令")
            return
        # 权限：仅"白名单群的群主"或"插件全局管理员"可设置/取消本群管理员。
        # 设置管理员属于高敏感操作，普通群管不应能借此扩张管理层。
        operator = self._try_get_sender_id(event)
        is_plugin_admin = await self._is_plugin_admin(event)
        if not is_plugin_admin:
            # 必须是白名单群（未设白名单时不开放群主自助设管理，避免任意群群主滥用）
            if not (self._group_white_set and group_id in self._group_white_set):
                yield event.plain_result("此功能仅对白名单群开放，请联系插件管理员将本群加入白名单")
                return
            role = await self._get_member_role(event, group_id, operator)
            if role != "owner":
                yield event.plain_result("仅本群群主或插件管理员可以设置/取消群管理员")
                return
        # 目标：优先取 @，否则取文本里的 QQ 号
        at_targets = self._extract_at_targets(event)
        args = event.message_str.split()
        enable = True
        # 解析"设置/取消"动作（可出现在任意位置）
        for tok in args[1:]:
            t = tok.strip().lower()
            if t in ("取消", "移除", "off", "0", "false", "down", "unset"):
                enable = False
            elif t in ("设置", "添加", "on", "1", "true", "set"):
                enable = True
        if at_targets:
            user_id = at_targets[0]
        else:
            # 从文本参数里找第一个纯数字 QQ 号
            user_id = ""
            for tok in args[1:]:
                tok = tok.strip()
                if tok.isdigit():
                    user_id = tok
                    break
            if not user_id:
                yield event.plain_result("用法: /设置管理 @某人 [设置/取消]\n或: /设置管理 <QQ号> [设置/取消]\n示例: /设置管理 @张三 设置")
                return
        try:
            ok, err, client, gid, uid = await self._prepare_group_member_action(event, "set_admin_enabled", "设置管理员", user_id)
            if not ok:
                yield event.plain_result(err)
                return
            # set_group_admin: OneBot API，enable=True 设为管理员，False 取消管理员
            ok, err = await self._call_group_api(client, 'set_group_admin', "设置管理员", group_id=gid, user_id=uid, enable=enable)
            if not ok:
                yield event.plain_result(f"{'设置' if enable else '取消'}失败: {err}")
                return
            yield event.plain_result(f"已{'将 ' + user_id + ' 设为群管理员' if enable else '取消 ' + user_id + ' 的群管理员'}")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")

    async def cmd_join_verify(self, event: AstrMessageEvent):
        '''修改入群验证方式。用法: /加群方式 <需要验证/允许/禁止>'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /加群方式 <方法>\n方法: 需要验证/允许/禁止\n示例: /加群方式 需要验证")
            return
        try:
            method_str = args[1].strip()
            method, method_text = self._parse_join_verify_method(method_str)
            if method == -1:
                yield event.plain_result("无效的方法，请选择: 需要验证/允许/禁止")
                return
            ok, err, client, gid = await self._prepare_group_action(event, "join_verify_enabled", "加群验证")
            if not ok:
                yield event.plain_result(err)
                return
            # set_group_add_option: OneBot API，修改群的加群验证方式
            ok, err = await self._call_group_api(client, 'set_group_add_option', "加群方式", group_id=gid, add_type=method)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"加群方式已设置为: {method_text}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    async def cmd_auto_moderate(self, event: AstrMessageEvent):
        '''开关智能审核功能。用法: /自动审核 开启/关闭/状态'''
        # 自动审核是插件全局运行开关，影响所有群，必须由插件全局管理员操作，
        # 群主/群管的群角色授权不足以更改全局开关。
        if not await self._is_plugin_admin(event):
            yield event.plain_result("仅插件管理员可以使用此功能")
            return
        args = event.message_str.split()
        if len(args) < 2:
            # 无参数时显示当前状态
            status = "开启" if self.auto_moderate_enabled else "关闭"
            yield event.plain_result(f"自动审核状态: {status}\n用法: /自动审核 开启|关闭")
            return
        action = args[1].strip()
        if action in ("开启", "on", "1"):
            self.auto_moderate_enabled = True
            self.config["auto_moderate_enabled"] = True
        elif action in ("关闭", "off", "0"):
            self.auto_moderate_enabled = False
            self.config["auto_moderate_enabled"] = False
        else:
            yield event.plain_result("参数错误，请使用: 开启 或 关闭")
            return
        # _save_config_safe: 将配置写回文件，带异常保护和备份机制
        self._save_config_safe()
        yield event.plain_result(f"自动审核已{action}")

    async def cmd_plugin_admin(self, event: AstrMessageEvent):
        '''管理插件管理员列表。用法: /设置管理插件 <QQ号> 添加/移除'''
        # 管理"全局插件管理员名单"是最高敏感操作（决定谁能跨群管理整个插件），
        # 必须仅限现有插件全局管理员，群主/群管的群角色授权严禁修改此名单（防提权）。
        if not await self._is_plugin_admin(event):
            yield event.plain_result("仅插件管理员可以使用此功能")
            return
        args = event.message_str.split()
        if len(args) < 2:
            admins = self._get_admin_list()
            yield event.plain_result(f"插件管理员 ({len(admins)}人): {', '.join(str(a) for a in admins) or '无'}\n用法: /设置管理插件 <QQ号> 添加/移除")
            return
        user_id = str(args[1]).strip()
        # 第三个参数可选，默认为"添加"（兼容只写 QQ 号的快捷用法）
        action = "添加" if len(args) < 3 else args[2].strip()
        admin_list = self._get_admin_list()
        if action == "移除":
            if user_id in admin_list:
                self._managed_list_remove("admin", user_id)
                self._admin_role_cache.clear()
                yield event.plain_result(f"已移除插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 不在管理员列表中")
        else:
            if user_id not in admin_list:
                self._managed_list_add("admin", user_id)
                self._admin_role_cache.clear()
                yield event.plain_result(f"已添加插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 已是插件管理员")

    async def recall_all(self, event: AstrMessageEvent):
        '''批量撤回最近消息。用法: /批量撤回 [条数] 或 /批量撤回 @用户 [条数]'''
        ok, err, client, gid = await self._prepare_group_action(event, "recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        target_user = None
        count = 20
        at_targets = self._extract_at_targets(event)
        if at_targets:
            target_user = at_targets[0]
        for arg in args[1:]:
            if arg.isdigit():
                count = max(1, min(int(arg), 100))
            elif not target_user and not arg.startswith('@'):
                target_user = arg
        try:
            result = await client.call_action('get_group_msg_history', group_id=gid, count=100)
            result = self._extract_data_result(result)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            for msg in messages:
                if recalled >= count:
                    break
                sender = msg.get('sender') or {}
                uid = str(sender.get('user_id', ''))
                # 如果指定了目标用户，跳过不匹配的消息
                if target_user and uid != target_user:
                    continue
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                        # 每条撤回间隔 0.5 秒，避免 OneBot 频率限制导致后续消息撤回失败
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.debug(f"[GroupMgr] 撤回消息{msg_id}失败: {e}")
            filter_desc = f"（用户{target_user}）" if target_user else ""
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息{filter_desc}")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    async def cmd_batch_ban(self, event: AstrMessageEvent):
        '''批量禁言多人。用法: /批量禁言 <QQ1> <QQ2> ... [时长分钟]'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /批量禁言 <QQ1> <QQ2> ... [时长分钟]\n示例: /批量禁言 111 222 333 30")
            return
        ok, err, client, gid = await self._prepare_group_action(event, "ban_enabled", "批量禁言")
        if not ok:
            yield event.plain_result(err)
            return
        tokens = args[1:]
        minutes = 10
        if len(tokens) >= 2 and tokens[-1].isdigit() and len(tokens[-1]) <= 6:
            minutes = self._clamp_int(tokens[-1], 10, 1, 43200)
            tokens = tokens[:-1]
        targets = [t.strip() for t in tokens if t.strip().isdigit()]
        targets = targets[:50]
        if not targets:
            yield event.plain_result("未解析到有效 QQ 号")
            return
        success, fail = 0, 0
        for uid in targets:
            uid_int = self._safe_int(uid, 0)
            ok_pre, _pre_msg = await self._precheck_member_action(client, gid, uid_int, "ban")
            if not ok_pre:
                fail += 1
                await asyncio.sleep(0.1)
                continue
            done, _e = await self._call_group_api(client, 'set_group_ban', "批量禁言",
                                                  group_id=gid, user_id=uid_int, duration=minutes * 60)
            if done:
                success += 1
                self._schedule_unban(str(gid), uid, minutes * 60)
            else:
                fail += 1
            await asyncio.sleep(0.3)
        yield event.plain_result(f"批量禁言完成：成功 {success} 人，失败 {fail} 人，时长 {minutes} 分钟")

    async def cmd_batch_kick(self, event: AstrMessageEvent):
        '''批量踢出多人。用法: /批量踢人 <QQ1> <QQ2> ...'''
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /批量踢人 <QQ1> <QQ2> ...\n示例: /批量踢人 111 222 333")
            return
        ok, err, client, gid = await self._prepare_group_action(event, "kick_enabled", "批量踢人")
        if not ok:
            yield event.plain_result(err)
            return
        targets = [t.strip() for t in args[1:] if t.strip().isdigit()][:50]
        if not targets:
            yield event.plain_result("未解析到有效 QQ 号")
            return
        success, fail = 0, 0
        for uid in targets:
            uid_int = self._safe_int(uid, 0)
            ok_pre, _pre_msg = await self._precheck_member_action(client, gid, uid_int, "kick")
            if not ok_pre:
                fail += 1
                await asyncio.sleep(0.1)
                continue
            done, _e = await self._call_group_api(client, 'set_group_kick', "批量踢人",
                                                  group_id=gid, user_id=uid_int)
            if done:
                success += 1
            else:
                fail += 1
            await asyncio.sleep(0.3)
        yield event.plain_result(f"批量踢人完成：成功 {success} 人，失败 {fail} 人")

    async def cmd_group_admin_grant(self, event: AstrMessageEvent):
        '''群管理员授权开关（F5）。用法: /群管理授权 开启/关闭/状态'''
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("请在群内使用此命令")
            return
        # 仅群主或插件全局管理员可改本群授权策略（与 /移除群管权限 一致）。
        # 不放给普通群管，避免被授权的群管反过来扩大或锁定授权范围。
        operator = self._try_get_sender_id(event)
        role = await self._get_member_role(event, group_id, operator)
        if role != "owner" and not await self._is_plugin_admin(event):
            yield event.plain_result("仅群主或插件管理员可以管理本群的群管理授权")
            return
        args = event.message_str.split()
        if len(args) < 2:
            grant = self._storage.get_group_admin_grant(group_id)
            if grant and grant.get("enabled"):
                who = []
                if grant.get("grant_owner"):
                    who.append("群主")
                if grant.get("grant_admin"):
                    who.append("管理员")
                yield event.plain_result(f"本群群管理员授权：开启（授权对象：{'、'.join(who) or '无'}）\n用法: /群管理授权 开启/关闭")
            else:
                yield event.plain_result("本群群管理员授权：关闭\n用法: /群管理授权 开启/关闭")
            return
        action = args[1].strip()
        if action in ("开启", "on", "1"):
            self._storage.save_group_admin_grant(group_id, grant_owner=True, grant_admin=True, enabled=True)
            self._admin_role_cache.clear()
            yield event.plain_result("已开启本群群管理员授权（群主+管理员在本群自动获得群管操作权限，被下管理后失效）")
        elif action in ("关闭", "off", "0"):
            self._storage.save_group_admin_grant(group_id, grant_owner=True, grant_admin=True, enabled=False)
            self._admin_role_cache.clear()
            yield event.plain_result("已关闭本群群管理员授权")
        else:
            yield event.plain_result("参数错误，请使用: 开启 或 关闭")

    async def cmd_revoke_admin_perm(self, event: AstrMessageEvent):
        '''群主移除本群某群管的bot管理权限。用法: /移除群管权限 <QQ号> 或 /恢复群管权限 <QQ号>'''
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("请在群内使用此命令")
            return
        operator = self._try_get_sender_id(event)
        role = await self._get_member_role(event, group_id, operator)
        if role != "owner" and not await self._is_plugin_admin(event):
            yield event.plain_result("仅群主或插件管理员可以管理本群的群管权限")
            return
        args = event.message_str.split()
        if len(args) < 2:
            blocked = self._storage.list_group_admin_blocks(group_id)
            ids = "、".join(b["user_id"] for b in blocked) or "无"
            yield event.plain_result(f"本群已被移除bot权限的群管：{ids}\n用法: /移除群管权限 <QQ号> 或 /恢复群管权限 <QQ号>")
            return
        target = str(args[1]).strip()
        if not target.isdigit():
            yield event.plain_result("请提供有效的 QQ 号")
            return
        # 指令名区分移除/恢复
        is_restore = "恢复" in event.message_str.split()[0]
        if is_restore:
            self._storage.remove_group_admin_block(group_id, target)
            self._admin_role_cache.clear()
            yield event.plain_result(f"已恢复 {target} 在本群的 bot 管理权限")
        else:
            self._storage.add_group_admin_block(group_id, target)
            self._admin_role_cache.clear()
            yield event.plain_result(f"已移除 {target} 在本群的 bot 管理权限（该用户将无法再使用本插件的群管功能）")
