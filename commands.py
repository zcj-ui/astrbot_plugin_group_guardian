# -*- coding: utf-8 -*-
import asyncio
import time
from typing import Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter


class CommandsMixin:
    @filter.command("字数统计")
    async def word_count(self, event: AstrMessageEvent):
        '''统计群内关键词出现次数'''
        ok, err = await self._check_admin_cfg_access(event, "word_count_enabled", "字数统计", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /字数统计 <关键词> [天数] [类型]\n类型: 脏话/广告/敏感词/黑名单\n示例: /字数统计 傻逼 7 脏话")
            return
        keyword = args[1]
        days = 7
        search_type = "all"
        type_map = {"脏话": "swear", "广告": "ad", "敏感词": "sensitive", "黑名单": "black"}
        if len(args) >= 3:
            try:
                days = int(args[2])
            except ValueError:
                search_type = type_map.get(args[2], args[2].lower())
        if len(args) >= 4:
            search_type = type_map.get(args[3], args[3].lower())
        days = max(1, min(days, 90))
        try:
            group_id, client, _, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            count, sample_messages = await self._search_keyword_in_messages(event, group_id, keyword, days, search_type)
            if count == 0:
                yield event.plain_result(f"最近 {days} 天内未找到包含「{keyword}」的消息")
            else:
                result = f"最近 {days} 天内「{keyword}」出现次数: {count}\n"
                if sample_messages:
                    result += "\n最近消息:\n"
                    for msg in sample_messages[:5]:
                        result += f"  {msg}\n"
                yield event.plain_result(result)
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    async def _search_keyword_in_messages(self, event: AstrMessageEvent, group_id: str, keyword: str, days: int, search_type: str = "all") -> Tuple[int, list]:
        client = await self._get_client(event)
        if not client:
            return 0, []
        try:
            result = await client.call_action('get_group_msg_history', group_id=self._safe_int(group_id, 0), count=100)
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
                if msg_time < cutoff:
                    continue
                raw_message = msg.get('message', '')
                text = self._format_message_content(raw_message)
                if keyword.lower() in text.lower():
                    if search_type != "all":
                        is_match = False
                        if search_type == "swear":
                            is_match = any(p.search(text) for p in self._compiled_swear)
                        elif search_type == "ad":
                            is_match = self._is_ad_pattern(text)
                        elif search_type == "sensitive":
                            is_match = any(p.search(text) for p in self._compiled_lexicon.get("political", []))
                        elif search_type == "black":
                            sender = msg.get('sender') or {}
                            uid = str(sender.get('user_id', ''))
                            is_match = uid in self._user_black_set
                        if not is_match:
                            continue
                    count += 1
                    sender = msg.get('sender') or {}
                    nickname = sender.get('nickname', '未知')
                    sample_messages.append(f"{nickname}: {text[:50]}")
            except Exception:
                continue
        return count, sample_messages

    @filter.command("群统计")
    async def group_stats(self, event: AstrMessageEvent):
        '''显示群内今日消息统计和活跃排行'''
        ok, err = await self._check_admin_cfg_access(event, "group_stats_enabled", "群统计", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = self._extract_list_result(result)
            total = len(members)
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

    @filter.command("搜索成员")
    async def search_member(self, event: AstrMessageEvent):
        '''按昵称或QQ号搜索群成员'''
        ok, err = await self._check_admin_cfg_access(event, "member_list_enabled", "查看群成员")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /搜索成员 <关键词>")
            return
        keyword = args[1]
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = self._extract_list_result(result)
            matched = []
            for m in members:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                uid = str(m.get("user_id", ""))
                if keyword.lower() in card.lower() or keyword.lower() in nickname.lower() or keyword in uid:
                    matched.append(m)
            if not matched:
                yield event.plain_result(f"未找到匹配「{keyword}」的成员")
            else:
                result_text = f"找到 {len(matched)} 个匹配成员:\n"
                for m in matched[:20]:
                    card = m.get("card", "")
                    nickname = m.get("nickname", "")
                    name = card if card else nickname
                    role = m.get("role", "member")
                    role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                    result_text += f"  {name}({m.get('user_id')}) [{role_text}]\n"
                yield event.plain_result(result_text.strip())
        except Exception as e:
            yield event.plain_result(f"搜索失败: {e}")

    @filter.command("撤回最新消息")
    async def recall_last(self, event: AstrMessageEvent):
        '''撤回群内最新一条或多条消息'''
        ok, msg = self._cfg_check("recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        args = event.message_str.split()
        count = 1
        if len(args) >= 2:
            count = self._safe_int(args[1], 1)
        count = max(1, min(count, 10))
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_msg_history', group_id=self._safe_int(group_id, 0), count=count + 1)
            result = self._extract_data_result(result)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            for msg in messages[-count:]:
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.debug(f"[GroupMgr] 撤回消息{msg_id}失败: {e}")
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    @filter.command("禁言")
    async def cmd_ban(self, event: AstrMessageEvent):
        '''禁言指定群成员。用法: /禁言 <QQ号> <分钟>'''
        ok, err = await self._check_admin_cfg_access(event, "ban_enabled", "禁言")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /禁言 <QQ号> [时长(分钟)]\n示例: /禁言 123456 30")
            return
        try:
            user_id = str(args[1]).strip()
            duration = min(max(self._safe_int(args[2], 10) if len(args) > 2 else 10, 1), 43200)
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_ban', "禁言", group_id=gid, user_id=uid, duration=duration * 60)
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            yield event.plain_result(f"已禁言 {user_id}，时长 {duration} 分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {e}")

    @filter.command("解禁")
    async def cmd_unban(self, event: AstrMessageEvent):
        '''解除指定群成员禁言。用法: /解禁 <QQ号>'''
        ok, err = await self._check_admin_cfg_access(event, "unban_enabled", "解禁")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /解禁 <QQ号>\n示例: /解禁 123456")
            return
        try:
            user_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_ban', "解禁", group_id=gid, user_id=uid, duration=0)
            if not ok:
                yield event.plain_result(f"解禁失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解禁失败: {e}")

    @filter.command("踢人")
    async def cmd_kick(self, event: AstrMessageEvent):
        '''将成员移出群聊。用法: /踢人 <QQ号>'''
        ok, err = await self._check_admin_cfg_access(event, "kick_enabled", "踢人")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /踢人 <QQ号>\n示例: /踢人 123456")
            return
        try:
            user_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_kick', "踢人", group_id=gid, user_id=uid)
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 踢出群聊")
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

    @filter.command("全体禁言")
    async def cmd_whole_ban(self, event: AstrMessageEvent):
        '''开启或关闭全员禁言。用法: /全体禁言 开启/关闭'''
        ok, err = await self._check_admin_cfg_access(event, "whole_ban_enabled", "全体禁言")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        enable = True
        if len(args) >= 2:
            action = args[1].strip()
            if action in ("关闭", "off", "0", "取消"):
                enable = False
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_whole_ban', "全体禁言", group_id=gid, enable=enable)
            if not ok:
                yield event.plain_result(f"操作失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")

    @filter.command("设置名片")
    async def cmd_set_card(self, event: AstrMessageEvent):
        '''修改成员群名片。用法: /设置名片 <QQ号> <新名称>'''
        ok, err = await self._check_admin_cfg_access(event, "set_card_enabled", "设置名片")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("用法: /设置名片 <QQ号> <名片内容>\n示例: /设置名片 123456 管理员")
            return
        try:
            user_id = str(args[1]).strip()
            card = ' '.join(args[2:])
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_card', "设置名片", group_id=gid, user_id=uid, card=card)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的群名片为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.command("发公告")
    async def cmd_send_notice(self, event: AstrMessageEvent):
        '''发布群公告。用法: /发公告 <内容>'''
        ok, err = await self._check_admin_cfg_access(event, "send_announcement_enabled", "发公告")
        if not ok:
            yield event.plain_result(err)
            return
        content = event.message_str.replace("/发公告", "").strip()
        if not content:
            yield event.plain_result("用法: /发公告 <公告内容>")
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            r = await client.call_action('_send_group_notice', group_id=gid, content=content)
            api_ok, err = self._check_api_result(r, "发公告")
            if not api_ok:
                yield event.plain_result(f"发送失败: {err}")
                return
            notice_id = (r or {}).get("notice_id") or (r or {}).get("id") or ""
            yield event.plain_result(f"公告已发送{f'，ID: {notice_id}' if notice_id else ''}")
        except Exception as e:
            yield event.plain_result(f"发送失败: {e}")

    @filter.command("删公告")
    async def cmd_delete_notice(self, event: AstrMessageEvent):
        '''删除群公告。用法: /删公告 <公告ID>'''
        ok, err = await self._check_admin_cfg_access(event, "delete_announcement_enabled", "删公告")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删公告 <公告ID>")
            return
        try:
            notice_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, '_del_group_notice', "删公告", group_id=gid, notice_id=notice_id)
            if not ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    @filter.command("公告列表")
    async def cmd_list_notices(self, event: AstrMessageEvent):
        '''查看群公告列表'''
        ok, err = await self._check_admin_cfg_access(event, "list_announcements_enabled", "公告列表", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('_get_group_notice', group_id=gid)
            notices = self._extract_list_result(result)
            if not notices:
                yield event.plain_result("暂无群公告")
                return
            lines = [f"📋 群公告列表 ({len(notices)}条):"]
            for n in notices[:10]:
                nid = n.get("notice_id", n.get("id", ""))
                pub = n.get("publisher") or {}
                name = pub.get("nickname", "未知")
                title = n.get("title", n.get("content", ""))[:40]
                lines.append(f"  ID:{nid} | {name}: {title}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.command("文件列表")
    async def cmd_list_files(self, event: AstrMessageEvent):
        '''查看群文件列表'''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件管理", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_root_files', group_id=gid)
            result = self._extract_data_result(result)
            files = result.get("files", []) if isinstance(result, dict) else []
            folders = result.get("folders", []) if isinstance(result, dict) else []
            lines = [f"📁 群文件列表:"]
            for f in folders[:15]:
                lines.append(f"  📁 {f.get('folder_name', '?')}")
            for f in files[:15]:
                size = f.get('size', 0)
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

    @filter.command("删文件")
    async def cmd_delete_file(self, event: AstrMessageEvent):
        '''删除群文件。用法: /删文件 <文件ID>'''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件管理")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /删文件 <file_id>\n提示: 使用 /文件列表 查看 file_id")
            return
        try:
            file_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'delete_group_file', "删文件", group_id=gid, file_id=file_id, busid=0)
            if not ok:
                yield event.plain_result(f"删除失败: {err}")
                return
            yield event.plain_result(f"已删除文件 {file_id}")
        except Exception as e:
            yield event.plain_result(f"删除失败: {e}")

    @filter.command("成员列表")
    async def cmd_member_list(self, event: AstrMessageEvent):
        '''查看群成员列表'''
        ok, err = await self._check_admin_cfg_access(event, "member_list_enabled", "成员列表", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = self._extract_list_result(result)
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

    @filter.command("禁言列表")
    async def cmd_banned_list(self, event: AstrMessageEvent):
        '''查看当前被禁言的成员'''
        ok, err = await self._check_admin_cfg_access(event, "banned_list_enabled", "禁言列表", need_admin=False)
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_shut_list', group_id=gid)
            banned = self._extract_list_result(result)
            if not banned:
                yield event.plain_result("当前无人被禁言")
                return
            lines = [f"🚫 禁言列表 ({len(banned)}人):"]
            for b in banned[:20]:
                uid = b.get("user_id", "?")
                dur = b.get("duration", 0)
                lines.append(f"  QQ: {uid}, 剩余: {dur // 60}分钟")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取失败: {e}")

    @filter.command("群名")
    async def cmd_set_name(self, event: AstrMessageEvent):
        '''修改群聊名称。用法: /群名 <新名称>'''
        ok, err = await self._check_admin_cfg_access(event, "set_group_name_enabled", "修改群名")
        if not ok:
            yield event.plain_result(err)
            return
        name = event.message_str.replace("/群名", "").strip()
        if not name:
            yield event.plain_result("用法: /群名 <新群名>")
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_name', "修改群名", group_id=gid, group_name=name)
            if not ok:
                yield event.plain_result(f"修改失败: {err}")
                return
            yield event.plain_result(f"群名已修改为: {name}")
        except Exception as e:
            yield event.plain_result(f"修改失败: {e}")

    @filter.command("头衔")
    async def cmd_set_title(self, event: AstrMessageEvent):
        '''设置成员专属头衔。用法: /头衔 <QQ号> <头衔名>'''
        ok, err = await self._check_admin_cfg_access(event, "set_title_enabled", "设置头衔")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 3:
            yield event.plain_result("用法: /头衔 <QQ号> <头衔内容>\n示例: /头衔 123456 大佬")
            return
        try:
            user_id = str(args[1]).strip()
            title = ' '.join(args[2:])
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_special_title', "设置头衔", group_id=gid, user_id=uid, special_title=title, duration=-1)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设置 {user_id} 的专属头衔: {title}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.command("设精华")
    async def cmd_set_essence(self, event: AstrMessageEvent):
        '''设置精华消息。用法: /设精华 <消息ID>'''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /设精华 <message_id>\n回复消息或提供 message_id")
            return
        try:
            msg_id = self._safe_int(args[1], 0)
            if not msg_id:
                yield event.plain_result("消息ID格式无效")
                return
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_essence_msg', "设精华", message_id=msg_id)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已设为精华消息 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.command("取消精华")
    async def cmd_del_essence(self, event: AstrMessageEvent):
        '''取消精华消息。用法: /取消精华 <消息ID>'''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /取消精华 <message_id>")
            return
        try:
            msg_id = self._safe_int(args[1], 0)
            if not msg_id:
                yield event.plain_result("消息ID格式无效")
                return
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'delete_essence_msg', "取消精华", message_id=msg_id)
            if not ok:
                yield event.plain_result(f"取消失败: {err}")
                return
            yield event.plain_result(f"已取消精华 (ID: {msg_id})")
        except Exception as e:
            yield event.plain_result(f"取消失败: {e}")

    @filter.command("设置管理")
    async def cmd_set_admin(self, event: AstrMessageEvent):
        '''设置或取消群管理员。用法: /设置管理 <QQ号>'''
        ok, err = await self._check_admin_cfg_access(event, "set_admin_enabled", "设置管理员")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法: /设置管理 <QQ号>\n示例: /设置管理 123456")
            return
        try:
            user_id = str(args[1]).strip()
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_admin', "设置管理员", group_id=gid, user_id=uid, enable=True)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 设为群管理员")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.command("加群方式")
    async def cmd_join_verify(self, event: AstrMessageEvent):
        '''修改入群验证方式。用法: /加群方式 <需要验证/允许/禁止>'''
        ok, err = await self._check_admin_cfg_access(event, "join_verify_enabled", "加群验证")
        if not ok:
            yield event.plain_result(err)
            return
        args = event.message_str.split()
        method_map = {"需要验证": 1, "允许": 0, "禁止": 2, "免审核": 0}
        if len(args) < 2:
            yield event.plain_result("用法: /加群方式 <方法>\n方法: 需要验证/允许/禁止\n示例: /加群方式 需要验证")
            return
        try:
            method_str = args[1].strip()
            method = method_map.get(method_str, -1)
            if method == -1:
                yield event.plain_result("无效的方法，请选择: 需要验证/允许/禁止")
                return
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_add_option', "加群方式", group_id=gid, add_type=method)
            if not ok:
                yield event.plain_result(f"设置失败: {err}")
                return
            yield event.plain_result(f"加群方式已设置为: {method_str}")
        except Exception as e:
            yield event.plain_result(f"设置失败: {e}")

    @filter.command("自动审核")
    async def cmd_auto_moderate(self, event: AstrMessageEvent):
        '''开关智能审核功能。用法: /自动审核 开启/关闭/状态'''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        args = event.message_str.split()
        if len(args) < 2:
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
        self._save_config_safe()
        yield event.plain_result(f"自动审核已{action}")

    @filter.command("设置管理插件")
    async def cmd_plugin_admin(self, event: AstrMessageEvent):
        '''管理插件管理员列表。用法: /设置管理插件 <QQ号> 添加/移除'''
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        args = event.message_str.split()
        if len(args) < 2:
            admins = self.config.get("admin_list", [])
            yield event.plain_result(f"插件管理员 ({len(admins)}人): {', '.join(str(a) for a in admins) or '无'}\n用法: /设置管理插件 <QQ号> 添加/移除")
            return
        user_id = str(args[1]).strip()
        action = "添加" if len(args) < 3 else args[2].strip()
        admin_list = self._get_admin_list()
        if action == "移除":
            if user_id in admin_list:
                self._safe_list_remove(admin_list, user_id)
                yield event.plain_result(f"已移除插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 不在管理员列表中")
        else:
            if user_id not in admin_list:
                admin_list.append(user_id)
                yield event.plain_result(f"已添加插件管理员: {user_id}")
            else:
                yield event.plain_result(f"{user_id} 已是插件管理员")
        self.config["admin_list"] = admin_list
        self._save_config_safe()

    @filter.command("批量撤回")
    async def recall_all(self, event: AstrMessageEvent):
        '''批量撤回最近消息。用法: /批量撤回 [条数] 或 /批量撤回 @用户 [条数]'''
        ok, msg = self._cfg_check("recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(msg)
            return
        allowed, reason = self._check_group_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return
        if not await self._is_admin(event):
            yield event.plain_result("仅管理员可以使用此功能")
            return
        args = event.message_str.split()
        target_user = None
        count = 20
        for arg in args[1:]:
            if arg.isdigit():
                count = max(1, min(int(arg), 100))
            elif arg.startswith('@'):
                target_user = arg[1:]
            else:
                target_user = arg
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("无法获取群号")
            return
        client = await self._get_client(event)
        if not client:
            yield event.plain_result("无法获取QQ客户端")
            return
        try:
            result = await client.call_action('get_group_msg_history', group_id=self._safe_int(group_id, 0), count=100)
            result = self._extract_data_result(result)
            messages = result.get('messages', []) if isinstance(result, dict) else []
            recalled = 0
            for msg in messages:
                if recalled >= count:
                    break
                sender = msg.get('sender') or {}
                uid = str(sender.get('user_id', ''))
                if target_user and uid != target_user:
                    continue
                msg_id = msg.get('message_id')
                if msg_id:
                    try:
                        await client.call_action('delete_msg', message_id=msg_id)
                        recalled += 1
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.debug(f"[GroupMgr] 撤回消息{msg_id}失败: {e}")
            filter_desc = f"（用户{target_user}）" if target_user else ""
            yield event.plain_result(f"已尝试撤回 {recalled} 条消息{filter_desc}")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")
