# -*- coding: utf-8 -*-
import os
import time
from datetime import datetime

from astrbot.api.event import AstrMessageEvent


class LlmToolsMixin:
    # LLM Tool 同样使用 async generator 模式。AstrBot 会根据函数签名的 event 后的参数自动生成 Tool 的 parameters schema。
    # docstring 中的 Args 格式很重要：参数名(类型): 描述，会被 AstrBot 解析为工具的输入参数定义。
    # _check_admin_cfg_access 同时检查功能开关和当前用户是否为插件/群管理员，保证工具不会被未授权用户调用。
    #
    # 通用模式（每个工具遵循以下步骤）：
    #   1. 权限与功能开关检查 — _check_admin_cfg_access(event, "feature_flag", "中文名")
    #      返回 (ok, err)；不通过则 yield 错误信息并 return。
    #   2. 获取 OneBot 客户端与群 ID — _get_group_client(event, need_gid=True)
    #      返回 (unused, client, group_id, err)；client 为 None 则 yield 错误并 return。
    #   3. 用户输入校验 — 如 _safe_int(user_id, 0) 将字符串转为 int，失败返回默认值 0，
    #      然后 if not uid 判定无效。
    #   4. 调用 OneBot API — 通过 _call_group_api(client, action_name, ...) 封装
    #      client.call_action() 并检查返回码。某些工具直接调用 client.call_action() 并
    #      配合 _extract_list_result / _extract_data_result 提取响应数据。
    #   5. 结果格式化 — 构造人类可读的字符串，通过 yield event.plain_result(msg) 返回给 LLM。
    #   6. 异常兜底 — 所有 API / 处理异常由 except Exception 捕获并 yield 错误消息。
    # =============================================================================

    async def ban_group_member_tool(self, event: AstrMessageEvent, user_id: str, duration_minutes: int = 10):
        '''禁言群成员。当用户要求禁言某人时使用此工具。

        Args:
            user_id(string): 要禁言的用户QQ号
            duration_minutes(number): 禁言时长（分钟），默认10分钟
        '''
        # 1. 权限检查：ban_enabled 功能开关 + 调用者必须是群管理员/群主/插件管理员
        ok, err = await self._check_admin_cfg_access(event, "ban_enabled", "禁言")
        if not ok:
            yield event.plain_result(err)
            return
        try:
            # 2. 获取 OneBot 客户端和当前群号；need_gid=True 表示需要提取 group_id
            _, client, gid, err = await self._get_group_client(event, need_gid=True)
            if not client:
                yield event.plain_result(err)
                return
            # 3. 输入校验：将 user_id 从 str 转为 int；_safe_int 在解析失败时返回 0（默认值）
            uid = self._safe_int(user_id, 0)
            if not uid:
                yield event.plain_result("用户QQ号格式无效")
                return
            # 4. 禁言时长处理：先 clamp 到 [1 分钟, 30 天]（单位分钟），再转秒
            duration_seconds = (min(max(duration_minutes, 1), 30 * 24 * 60) * 60)
            # 第 2 步除以 60 再乘以 60 是为了舍去不满 60 秒的部分，保证 duration 是整分钟
            duration_seconds = (duration_seconds // 60) * 60
            # 5. API 调用：_call_group_api 内部调 client.call_action('set_group_ban', ...)
            #    并检查 API 返回码；失败时返回 (False, 错误信息)
            ok, err = await self._call_group_api(client, 'set_group_ban', "禁言", group_id=gid, user_id=uid, duration=duration_seconds)
            if not ok:
                yield event.plain_result(f"禁言失败: {err}")
                return
            # 6. 构造成功响应（plain_result 将以纯文本形式回复）
            yield event.plain_result(f"已禁言 {user_id} {duration_minutes}分钟")
        except Exception as e:
            # 7. 任何未预料的异常（网络断开、JSON 解析失败等）都被捕获并返回
            yield event.plain_result(f"禁言失败: {e}")

    async def unban_group_member_tool(self, event: AstrMessageEvent, user_id: str):
        '''解除群成员禁言。当用户要求解除某人禁言时使用此工具。

        Args:
            user_id(string): 要解除禁言的用户QQ号
        '''
        # 同样先检查 unban_enabled 开关与管理员权限
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
            # duration=0 表示解除禁言，这是 OneBot set_group_ban 的约定
            ok, err = await self._call_group_api(client, 'set_group_ban', "解除禁言", group_id=gid, user_id=uid, duration=0)
            if not ok:
                yield event.plain_result(f"解除禁言失败: {err}")
                return
            yield event.plain_result(f"已解除 {user_id} 的禁言")
        except Exception as e:
            yield event.plain_result(f"解除禁言失败: {e}")

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
            # reject_add_request=False 表示踢出后允许再次加群；如果为 True 则拉黑
            ok, err = await self._call_group_api(client, 'set_group_kick', "踢人", group_id=gid, user_id=uid, reject_add_request=False)
            if not ok:
                yield event.plain_result(f"踢人失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 踢出群聊")
        except Exception as e:
            yield event.plain_result(f"踢人失败: {e}")

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
            # enable=True 开全体禁言，False 关；_call_group_api 负责校验返回码
            ok, err = await self._call_group_api(client, 'set_group_whole_ban', "全体禁言", group_id=gid, enable=enable)
            if not ok:
                yield event.plain_result(f"设置全体禁言失败: {err}")
                return
            yield event.plain_result(f"已{'开启' if enable else '关闭'}全体禁言")
        except Exception as e:
            yield event.plain_result(f"设置全体禁言失败: {e}")

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
            # set_group_card 设置群名片，card 为空字符串可清除名片
            ok, err = await self._call_group_api(client, 'set_group_card', "设置群名片", group_id=gid, user_id=uid, card=card)
            if not ok:
                yield event.plain_result(f"设置群名片失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的群名片设为: {card}")
        except Exception as e:
            yield event.plain_result(f"设置群名片失败: {e}")

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
            # _send_group_notice 是 NapCat/LLOneBot 等实现私有 API，非标准 OneBot 动作
            ok, err = await self._call_group_api(client, '_send_group_notice', "发送群公告", group_id=gid, content=content)
            if not ok:
                yield event.plain_result(f"发布公告失败: {err}")
                return
            yield event.plain_result("群公告已发布")
        except Exception as e:
            yield event.plain_result(f"发布公告失败: {e}")

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
            # 此工具直接调 client.call_action 而非 _call_group_api，因为需要解析返回的列表数据；
            # _extract_list_result 从 OneBot 响应中提取数组（兼容 data.members / data / 直接数组等多种格式）
            result = await client.call_action('get_group_member_list', group_id=gid)
            members = self._extract_list_result(result)
            if not members:
                yield event.plain_result("群成员列表为空")
                return
            # 格式化：最多展示前 30 人，优先显示群名片（card），其次昵称；附带身份标记
            member_texts = []
            for m in members[:30]:
                card = m.get("card", "")
                nickname = m.get("nickname", "")
                name = card if card else nickname
                role = m.get("role", "member")
                role_text = {"owner": "群主", "admin": "管理员", "member": "成员"}.get(role, role)
                member_texts.append(f"- {name}({m.get('user_id')}) [{role_text}]")
            # _truncate 确保返回文本不超过 LLM 上下文长度限制
            yield event.plain_result(self._truncate(f"群成员（共{len(members)}人）：\n" + "\n".join(member_texts)))
        except Exception as e:
            yield event.plain_result(f"获取成员列表失败: {e}")

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
            # set_group_admin：enable=True 设管理员，False 取消；仅群主可调用
            ok, err = await self._call_group_api(client, 'set_group_admin', "设置管理员", group_id=gid, user_id=uid, enable=enable)
            if not ok:
                yield event.plain_result(f"设置管理员失败: {err}")
                return
            yield event.plain_result(f"已{'设为' if enable else '取消'} {user_id} 的管理员")
        except Exception as e:
            yield event.plain_result(f"设置管理员失败: {e}")

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
            # OneBot 中设置群头衔的动作为 set_group_special_title，而非 set_member_title
            ok, err = await self._call_group_api(client, 'set_group_special_title', "设置头衔", group_id=gid, user_id=uid, special_title=title)
            if not ok:
                yield event.plain_result(f"设置头衔失败: {err}")
                return
            yield event.plain_result(f"已将 {user_id} 的头衔设为: {title}")
        except Exception as e:
            yield event.plain_result(f"设置头衔失败: {e}")

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
            # get_group_shut_list 为 OneBot 标准 API（部分实现），返回被禁言成员列表
            result = await client.call_action('get_group_shut_list', group_id=gid)
            shut_list = self._extract_list_result(result)
            if not shut_list:
                yield event.plain_result("当前没有禁言成员")
                return
            member_texts = []
            for m in shut_list[:15]:
                uid = m.get("user_id", "")
                nickname = m.get("nickname", "")
                # shut_up_timestamp 是禁言结束时的 Unix 时间戳；通过当前时间计算剩余时长
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
            # 将人类可读的类型名映射为 OneBot set_group_add_option 的 add_type 数字代码
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
            # recall 不需要群 ID，只需客户端即可；因此 need_gid=False（默认），_get_group_client 返回 (_, client, err)
            _, client, err = await self._get_group_client(event)
            if not client:
                yield event.plain_result(err)
                return
            mid = self._safe_int(message_id, 0)
            if not mid:
                yield event.plain_result("消息ID格式无效")
                return
            # OneBot 撤回消息动作为 delete_msg（注意不是 recall_msg）
            ok, err = await self._call_group_api(client, 'delete_msg', "撤回消息", message_id=mid)
            if not ok:
                yield event.plain_result(f"撤回失败: {err}")
                return
            yield event.plain_result(f"已撤回消息 {message_id}")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

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
            # set_essence_msg 是 OneBot 标准动作（需群主/管理员权限）
            ok, err = await self._call_group_api(client, 'set_essence_msg', "设精华", message_id=mid)
            if not ok:
                yield event.plain_result(f"设精华失败: {err}")
                return
            yield event.plain_result(f"已将 {message_id} 设为精华")
        except Exception as e:
            yield event.plain_result(f"设精华失败: {e}")

    async def delete_essence_message_tool(self, event: AstrMessageEvent, message_id: str):
        '''取消群精华消息。

        Args:
            message_id(string): 要取消精华的消息ID
        '''
        # 注意：这里复用了 essence_enabled 开关（与 set 使用同一个配置项）
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
            # _del_group_notice 也是 NapCat/LLOneBot 私有 API
            ok, err = await self._call_group_api(client, '_del_group_notice', "删除公告", group_id=gid, notice_id=notice_id)
            if not ok:
                yield event.plain_result(f"删除公告失败: {err}")
                return
            yield event.plain_result(f"已删除公告 {notice_id}")
        except Exception as e:
            yield event.plain_result(f"删除公告失败: {e}")

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
            # get_group_root_files 获取群文件根目录，私有 API；取出 files 和 folders 两个列表
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
            # _get_group_notice 私有 API，返回公告列表数据
            result = await client.call_action('_get_group_notice', group_id=gid)
            notices = self._extract_list_result(result)
            if not notices:
                yield event.plain_result("暂无公告")
                return
            lines = [f"群公告（{len(notices)}条）"]
            for n in notices[:10]:
                notice_id = n.get('notice_id', '')
                sender_id = n.get('sender_id', '')
                # msg 字段可能是嵌套 dict（{text: "..."}）也可能是纯字符串，需要兼容处理
                _msg = n.get('msg')
                content = ((_msg.get('text', '') if isinstance(_msg, dict) else '') or n.get('content', ''))[:60]
                ts = n.get('publish_time', 0)
                t = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '未知'
                lines.append(f"  [{notice_id}] {content}... ({sender_id}, {t})")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"获取公告失败: {e}")

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
            # ── 安全目录检查 ─────────────────────────────────────────────────
            # 这是 upload 工具特有的关键安全措施：防止 LLM 被诱导上传任意系统文件。
            # LLM 生成的 file_path 可能包含 ".." 路径穿越，比如 "../../etc/passwd"。
            #
            # 检查逻辑：
            #   1. 获取安全基线目录 safe_dir = <data_dir>/uploads（插件数据目录下的 uploads 文件夹）
            #   2. 将用户提供的 file_path 做 abspath 规范化（解析所有 ..）
            #   3. 用 os.path.commonpath([safe_dir, normalized_path]) 判断
            #      规范化后的路径是否仍在 safe_dir 之下。
            #   4. 只有 path_allowed == True 才继续执行。
            # ─────────────────────────────────────────────────────────────────
            safe_dir = os.path.abspath(os.path.join(str(self._get_data_dir()), "uploads"))
            normalized_path = os.path.abspath(file_path or "")
            try:
                path_allowed = os.path.commonpath([safe_dir, normalized_path]) == safe_dir
            except ValueError:
                # 不同驱动器（如 C: vs D:）时 commonpath 会抛 ValueError，此时认定为不安全
                path_allowed = False
            if not path_allowed:
                yield event.plain_result(f"仅允许上传插件数据目录 uploads 下的文件: {safe_dir}")
                return
            # 文件存在性检查：确保文件确实存在于磁盘上
            if not os.path.isfile(normalized_path):
                yield event.plain_result(f"文件不存在: {normalized_path}")
                return
            # 如果未指定上传文件名，则使用原文件名
            name = file_name or os.path.basename(normalized_path)
            # 调用 upload_group_file（私有 API），并提取返回的 file_id 作为凭证
            result = await client.call_action('upload_group_file', group_id=gid, file=normalized_path, name=name)
            fid = result.get('file_id', '未知') if isinstance(result, dict) else '未知'
            yield event.plain_result(f"已上传，file_id: {fid}")
        except Exception as e:
            yield event.plain_result(f"上传失败: {e}")
