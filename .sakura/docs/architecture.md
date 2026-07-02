# 项目架构文档

## 一、模块划分

| 模块 | 职责 |
|------|------|
| `main.py` | 插件入口，注册消息与事件处理器 |
| `commands.py` | 28 项群管指令全集 |
| `moderation.py` | 违禁词匹配与 AI 审核核心 |
| `anti_flood.py` | 刷屏检测与限速机制 |
| `automaton.py` | 用户状态自动机 |
| `appeal.py` | 申诉处理及定时解禁 |
| `membership.py` | 群成员权限管理 |
| `storage.py` | SQLite 数据持久化抽象层 |
| `web.py` | WebUI 后端 API 路由 |
| `pages/dashboard/` | 前端仪表盘静态文件 |
| `onebot.py` | OneBot 协议适配层 |
| `remote.py` | 跨群远程群管执行 |
| `llm_tools.py` | LLM 调用封装 |
| `scheduler.py` | 定时任务管理 |
| `utils.py` | 通用工具函数 |

## 二、审核流程

1. **AC 自动机初筛** — 67k 词库 O(n) 快速匹配
2. **LLM 二次判断** — 语义级内容过滤，有超时和错误回退
3. **执行动作** — 撤回消息 + 说明，可选联动禁言

## 三、OneBot 协议约束

- **撤回窗口**：仅 2 分钟内消息可撤回
- **禁言最小粒度**：60 秒（即使 API 接受秒，最小生效单位 60s）
- **历史消息上限**：`get_group_msg_history` 上限 100 条
- **频率限制**：批量操作需间隔，避免 API 限速

## 四、继承体系

- `Main` 类通过多重 Mixin 组合功能
- Mixin 列表：`ModerationMixin`, `AntiFloodMixin`, `CommandsMixin`, `UtilitiesMixin` 等
- Mixin 依赖链需通过继承顺序保证
