# -*- coding: utf-8 -*-
import os
import time
from datetime import datetime

from astrbot.api.event import AstrMessageEvent, filter


class LlmToolsMixin:
    @filter.llm_tool(name="ban_group_member")
    async def ban_group_member_tool(self, event: AstrMessageEvent, user_id: str, duration_minutes: int = 10):
        '''禁言群成员。当用户要求禁言某人时使用此工具。

        Args:
            user_id(string): 要禁言的用户QQ号
            duration_minutes(number): 禁言时长（分钟），默认10分钟
        '''
        ok, err = await self._check_admin_cfg_access(event, "ban_enabled", "禁言")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            duration_seconds = (min(max(duration_minutes, 1), 30 * 24 * 60) * 60)
            duration_seconds = (duration_seconds // 60) * 60
            ok, err = await self._call_group_api(client, 'set_group_ban', "禁言", group_id=gid, user_id=uid, duration=duration_seconds)
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            yield event.plain_result(f"已禁言 {user_id} {duration_minutes}分钟")
        except Exception as e:
            yield event.plain_result(f"禁言失败: {e}")

    @filter.llm_tool(name="unban_group_member")
    async def unban_group_member_tool(self, event: AstrMessageEvent, user_id: str):
        '''解除群成员禁言。当用户要求解除某人禁言时使用此工具。

        Args:
            user_id(string): 要解除禁言的用户QQ号
        '''
        ok, err = await self._check_admin_cfg_access(event, "unban_enabled", "解除禁言")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_ban', "解除禁言", group_id=gid, user_id=uid, duration=0)
            if not ok:
                yield event.plain_result(f"解除禁言失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解除禁言失败: {e}")

    @filter.llm_tool(name="kick_group_member")
    async def kick_group_member_tool(self, event: AstrMessageEvent, user_id: str):
        '''踢出群成员。当用户要求将某人踢出群时使用此工具。

        Args:
            user_id(string): 要踢出的用户QQ号
        '''
        ok, err = await self._check_admin_cfg_access(event, "kick_enabled", "踢人")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_kick', "踢人", group_id=gid, user_id=uid, reject_add_request=False)
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 踢出群聊")
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

    @filter.llm_tool(name="set_whole_group_ban")
    async def set_whole_group_ban_tool(self, event: AstrMessageEvent, enable: bool = True):
        '''开启或关闭全体禁言。

        Args:
            enable(boolean): true开启全体禁言，false关闭全体禁言
        '''
        ok, err = await self._check_admin_cfg_access(event, "whole_ban_enabled", "全体禁言")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_whole_ban', "全体禁言", group_id=gid, enable=enable)
            if not ok:
                yield event.plain_result(f"设置全体禁言失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"设置全体禁言失败: {e}")

    @filter.llm_tool(name="set_member_card")
    async def set_member_card_tool(self, event: AstrMessageEvent, user_id: str, card: str):
        '''设置群成员群名片。

        Args:
            user_id(string): 目标用户QQ号
            card(string): 新的群名片
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_card_enabled", "修改群名片")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_card', "设置群名片", group_id=gid, user_id=uid, card=card)
            if not ok:
                yield event.plain_result(f"设置群名片失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的群名片设为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置群名片失败: {e}")

    @filter.llm_tool(name="send_group_announcement")
    async def send_group_announcement_tool(self, event: AstrMessageEvent, content: str):
        '''发送群公告。

        Args:
            content(string): 公告内容
        '''
        ok, err = await self._check_admin_cfg_access(event, "send_announcement_enabled", "发送群公告")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, '_send_group_notice', "发送群公告", group_id=gid, content=content)
            if not ok:
                yield event.plain_result(f"发布公告失败: {err}")
                return
            yield event.plain_result("群公告已发布")
        except Exception as e:
            yield event.plain_result(f"发布公告失败: {e}")

    @filter.llm_tool(name="get_group_member_list")
    async def get_group_member_list_tool(self, event: AstrMessageEvent):
        '''获取群成员列表。'''
        ok, err = await self._check_admin_cfg_access(event, "member_list_enabled", "查看群成员列表")
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
            if not members:
                yield event.plain_result("群成员列表为空")
                return
            member_texts = []
            for m in members[:30]:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                name = card if card else nickname
                role = m.get("role", "member")
                role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                member_texts.append(f"- {name}({m.get('user_id')}) [{role_text}]")
            yield event.plain_result(self._truncate(f"群成员（共{len(members)}人）：\n" + "\n".join(member_texts)))
        except Exception as e:
            yield event.plain_result(f"获取成员列表失败: {e}")

    @filter.llm_tool(name="set_group_admin")
    async def set_group_admin_tool(self, event: AstrMessageEvent, user_id: str, enable: bool = True):
        '''设置或取消群管理员。

        Args:
            user_id(string): 目标用户QQ号
            enable(boolean): true设为管理员，false取消管理员
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_admin_enabled", "设置管理员")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_admin', "设置管理员", group_id=gid, user_id=uid, enable=enable)
            if not ok:
                yield event.plain_result(f"设置管理员失败: {err}")
                return
            yield event.plain_result(f"已{'设为' if enable else '取消'} {user_id} 的管理员")
        except Exception as e:
            yield event.plain_result(f"设置管理员失败: {e}")

    @filter.llm_tool(name="set_group_name")
    async def set_group_name_tool(self, event: AstrMessageEvent, group_name: str):
        '''修改群名称。

        Args:
            group_name(string): 新的群名称
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_group_name_enabled", "修改群名称")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'set_group_name', "修改群名称", group_id=gid, group_name=group_name)
            if not ok:
                yield event.plain_result(f"改群名失败: {err}")
                return
            yield event.plain_result(f"群名已改为: {group_name}")
        except Exception as e:
            yield event.plain_result(f"改群名失败: {e}")

    @filter.llm_tool(name="set_member_title")
    async def set_member_title_tool(self, event: AstrMessageEvent, user_id: str, title: str):
        '''设置群成员专属头衔。

        Args:
            user_id(string): 目标用户QQ号
            title(string): 专属头衔
        '''
        ok, err = await self._check_admin_cfg_access(event, "set_title_enabled", "设置专属头衔")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_group_special_title', "设置头衔", group_id=gid, user_id=uid, special_title=title)
            if not ok:
                yield event.plain_result(f"设置头衔失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的头衔设为: {title}")
        except Exception as e:
            yield event.plain_result(f"设置头衔失败: {e}")

    @filter.llm_tool(name="get_banned_members")
    async def get_banned_members_tool(self, event: AstrMessageEvent):
        '''获取群禁言列表。'''
        ok, err = await self._check_admin_cfg_access(event, "banned_list_enabled", "查看禁言列表")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            result = await client.call_action('get_group_shut_list', group_id=gid)
            shut_list = self._extract_list_result(result)
            if not shut_list:
                yield event.plain_result("当前没有禁言成员")
                return
            member_texts = []
            for m in shut_list[:15]:
                uid = m.get("user_id", "")
                nickname = m.get("nickname", "")
                shut_time = self._safe_int(m.get("shut_up_timestamp", 0))
                if shut_time:
                    remain = max(0, shut_time - int(time.time()))
                    remain_str = f"{remain // 60}分{remain % 60}秒"
                else:
                    remain_str = "未知"
                member_texts.append(f"- {nickname}({uid}) 剩余: {remain_str}")
            yield event.plain_result(f"禁言列表（共{len(shut_list)}人）：\n" + "\n".join(member_texts))
        except Exception as e:
            yield event.plain_result(f"获取禁言列表失败: {e}")

    @filter.llm_tool(name="set_group_join_verify")
    async def set_group_join_verify_tool(self, event: AstrMessageEvent, verify_type: str = "allow"):
        '''设置群加群验证方式。

        Args:
            verify_type(string): 验证类型: allow(允许加入), deny(拒绝加入), need_verify(需要审核), not_allow(不允许)
        '''
        ok, err = await self._check_admin_cfg_access(event, "join_verify_enabled", "设置加群方式")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            type_map = {"allow": 2, "deny": 1, "need_verify": 3, "not_allow": 4}
            add_type = type_map.get(verify_type.lower(), 2)
            ok, err = await self._call_group_api(client, 'set_group_add_option', "设置加群方式", group_id=gid, add_type=add_type)
            if not ok:
                yield event.plain_result(f"设置加群方式失败: {err}")
                return
            type_text = {"allow": "允许加入", "deny": "拒绝加入", "need_verify": "需审核", "not_allow": "不允许"}.get(verify_type.lower(), verify_type)
            yield event.plain_result(f"加群方式已设为: {type_text}")
        except Exception as e:
            yield event.plain_result(f"设置加群方式失败: {e}")

    @filter.llm_tool(name="recall_message")
    async def recall_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''撤回指定消息。

        Args:
            message_id(string): 要撤回的消息ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "recall_enabled", "撤回消息")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            ok, err = await self._call_group_api(client, 'delete_msg', "撤回消息", message_id=mid)
            if not ok:
                yield event.plain_result(f"撤回失败: {err}")
                return
            yield event.plain_result(f"已撤回消息 {message_id}")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    @filter.llm_tool(name="set_essence_message")
    async def set_essence_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''设置群精华消息。

        Args:
            message_id(string): 要设为精华的消息ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            ok, err = await self._call_group_api(client, 'set_essence_msg', "设精华", message_id=mid)
            if not ok:
                yield event.plain_result(f"设精华失败: {err}")
                return
            yield event.plain_result(f"已将 {message_id} 设为精华")
        except Exception as e:
            yield event.plain_result(f"设精华失败: {e}")

    @filter.llm_tool(name="delete_essence_message")
    async def delete_essence_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''取消群精华消息。

        Args:
            message_id(string): 要取消精华的消息ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "essence_enabled", "精华消息")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            ok, err = await self._call_group_api(client, 'delete_essence_msg', "取消精华", message_id=mid)
            if not ok:
                yield event.plain_result(f"取消精华失败: {err}")
                return
            yield event.plain_result(f"已取消 {message_id} 的精华")
        except Exception as e:
            yield event.plain_result(f"取消精华失败: {e}")

    @filter.llm_tool(name="delete_group_notice")
    async def delete_group_notice_tool(self, event: AstrMessageEvent, notice_id: str):
        '''删除群公告。

        Args:
            notice_id(string): 公告ID
        '''
        ok, err = await self._check_admin_cfg_access(event, "delete_announcement_enabled", "删除群公告")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, '_del_group_notice', "删除公告", group_id=gid, notice_id=notice_id)
            if not ok:
                yield event.plain_result(f"删除公告失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除公告失败: {e}")

    @filter.llm_tool(name="list_group_files")
    async def list_group_files_tool(self, event: AstrMessageEvent):
        '''查看群文件列表。'''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件")
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
            files = (result.get('files') or []) if isinstance(result, dict) else []
            folders = (result.get('folders') or []) if isinstance(result, dict) else []
            if not files and not folders:
                yield event.plain_result("根目录下没有文件或文件夹")
                return
            lines = [f"群 {gid} 根目录："]
            if folders:
                lines.append(f"  {len(folders)}个文件夹")
                for f in folders[:10]:
                    lines.append(f"    [{f.get('folder_id', '')}] {f.get('folder_name', '')}")
            if files:
                lines.append(f"  {len(files)}个文件")
                for f in files[:10]:
                    size_mb = self._safe_int(f.get('file_size', 0)) / (1024 * 1024)
                    lines.append(f"    [{f.get('file_id', '')}] {f.get('file_name', '')} ({size_mb:.1f}MB)")
            yield event.plain_result(self._truncate("\n".join(lines)))
        except Exception as e:
            yield event.plain_result(f"查文件失败: {e}")

    @filter.llm_tool(name="delete_group_file")
    async def delete_group_file_tool(self, event: AstrMessageEvent, file_id: str, busid: int = 102):
        '''删除群文件。

        Args:
            file_id(string): 文件ID
            busid(number): 文件类型ID，默认为102
        '''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            ok, err = await self._call_group_api(client, 'delete_group_file', "删除文件", group_id=gid, file_id=file_id, busid=busid)
            if not ok:
                yield event.plain_result(f"删文件失败: {err}")
                return
            yield event.plain_result(f"已删除 {file_id}")
        except Exception as e:
            yield event.plain_result(f"删文件失败: {e}")

    @filter.llm_tool(name="get_group_notice_list")
    async def get_group_notice_list_tool(self, event: AstrMessageEvent):
        '''获取群公告列表。'''
        ok, err = await self._check_admin_cfg_access(event, "list_announcements_enabled", "查看公告列表")
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
                yield event.plain_result("暂无公告")
                return
            lines = [f"群公告（{len(notices)}条）"]
            for n in notices[:10]:
                notice_id = n.get('notice_id', '')
                sender_id = n.get('sender_id', '')
                _msg = n.get('msg')
                content = ((_msg.get('text', '') if isinstance(_msg, dict) else '') or n.get('content', ''))[:60]
                ts = n.get('publish_time', 0)
                t = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '未知'
                lines.append(f"  [{notice_id}] {content}... ({sender_id}, {t})")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取公告失败: {e}")

    @filter.llm_tool(name="upload_group_file")
    async def upload_group_file_tool(self, event: AstrMessageEvent, file_path: str, file_name: str = ""):
        '''上传文件到群文件。

        Args:
            file_path(string): 文件路径
            file_name(string): 上传后的文件名，可选
        '''
        ok, err = await self._check_admin_cfg_access(event, "group_files_enabled", "群文件")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            if not file_path or not os.path.exists(file_path):
                yield event.plain_result(f"文件不存在: {file_path}")
                return
            name = file_name or os.path.basename(file_path)
            result = await client.call_action('upload_group_file', group_id=gid, file=file_path, name=name)
            fid = result.get('file_id', '未知') if isinstance(result, dict) else '未知'
            yield event.plain_result(f"已上传，file_id: {fid}")
        except Exception as e:
            yield event.plain_result(f"上传失败: {e}")
