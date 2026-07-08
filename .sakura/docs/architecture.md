# 项目架构文档

## 一、技术栈

- **运行环境**：Python 3.10+，AstrBot 插件系统
- **消息协议**：OneBot（go-cqhttp、NapCat、LLOneBot 等）
- **数据存储**：SQLite 双库设计 — `group_guardian.db`（主数据）+ `lexicon.db`（外置词库）
- **Web 面板**：Python API 路由 + 静态 HTML/JS
- **AI 审核**：LLM 语义级内容过滤 + OCR 视觉模型
- **辅助技术**：Aho-Corasick 自动机（67k 词库 O(n) 匹配）、正则回退、定时调度

## 二、模块划分

| 模块 | 职责 |
|------|------|
| `main.py` | 插件入口，注册消息与事件处理器 |
| `commands.py` | 28 项群管指令全集 |
| `moderation.py` | 违禁词匹配与 AI 审核核心 |
| `anti_flood.py` | 刷屏检测与限速机制，含消息队列 |
| `automaton.py` | 用户状态自动机 |
| `appeal.py` | 申诉处理及定时解禁 |
| `membership.py` | 群成员权限管理与加群审核 |
| `storage.py` | SQLite 数据持久化抽象层 |
| `web.py` | WebUI 后端 API 路由 |
| `pages/dashboard/` | 前端仪表盘静态文件 |
| `onebot.py` | OneBot 协议适配层 |
| `remote.py` | 跨群远程群管执行 |
| `llm_tools.py` | LLM 调用封装 |
| `scheduler.py` | 定时任务管理 |
| `utils.py` | 通用工具函数 |

## 三、审核流程

1. **协议字段剥离** — 剥离 OneBot 拼接内容（引用原文、问题原文），仅匹配用户纯输入
2. **AC 自动机初筛** — 67k 词库 O(n) 快速匹配
3. **二维码解码**（可选）— 轻量 pyzbar 解码 → OCR 兜底
4. **短消息拼接检测**（可选）— 连续短消息 + 间隔 < 5s 拼接后过筛
5. **LLM 二次判断** — 语义级内容过滤，有超时和错误回退
6. **执行动作** — 撤回消息 + 说明，可选联动禁言

## 四、OneBot 协议约束

- **撤回窗口**：仅 2 分钟内消息可撤回
- **禁言最小粒度**：60 秒（即使 API 接受秒，最小生效单位 60s）
- **历史消息上限**：`get_group_msg_history` 上限 100 条
- **频率限制**：批量操作需间隔，避免 API 限速

## 五、继承体系

- `Main` 类通过多重 Mixin 组合功能
- Mixin 列表：`ModerationMixin`, `AntiFloodMixin`, `CommandsMixin`, `UtilitiesMixin` 等
- Mixin 依赖链需通过继承顺序保证，MRO 规则为**左高右低**

## 六、消息链解析层级

1. 用户纯输入（核心匹配对象）
2. 协议拼接内容（引用原文、问题原文等，需剥离）
3. OneBot 元数据字段（comment 等，不可直接用于审核匹配）
- 区分这三层是防止误审系统附加内容的关键
