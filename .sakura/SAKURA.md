# QQ群智能守护者 (GroupGuardian) – 项目概述

## 1. 项目简介

GroupGuardian 是一款面向 AstrBot 框架的综合性群聊管理插件，融合 28 项群管指令、AI 智能审核、违禁词热更新以及增强型 WebUI 管理面板，旨在让 QQ 机器人成为具备主动防御与便捷运维能力的群聊守护者。

## 2. 技术栈

- **运行环境**：Python 3.10+，基于 AstrBot 插件系统
- **消息协议**：OneBot（兼容 go-cqhttp 等标准实现）
- **数据存储**：SQLite（通过 `storage.py` 封装，含 `lexicon.db`）
- **Web 管理面板**：后端以 Python 提供 API 路由（`web.py`），前端采用静态 HTML + JavaScript 构建（`pages/dashboard` 目录），支持实时图表与交互操作
- **AI 审核能力**：通过 `llm_tools.py` 对接大型语言模型，实现语义级内容过滤
- **辅助技术**：正则表达式、定时调度（`scheduler.py`）、状态自动机（`automaton.py`）

## 3. 项目结构

| 模块 / 文件 | 职责说明 |
|------------|----------|
| `main.py` | 插件入口，注册消息与事件处理器 |
| `commands.py` | 管理指令全集（禁言、踢人、公告、精华等 28 项） |
| `moderation.py` | 违禁词匹配与 AI 审核核心逻辑 |
| `anti_flood.py` | 刷屏检测与限速机制 |
| `automaton.py` | 用户状态自动机（用于审核流程控制） |
| `appeal.py` | 用户申诉处理及定时解禁 |
| `membership.py` | 群成员权限管理（管理员、头衔、禁言状态） |
| `storage.py` | 数据持久化抽象层（SQLite 操作） |
| `web.py` | WebUI 后端路由与 API 实现 |
| `pages/dashboard/` | 前端仪表盘静态文件（HTML、JS、CSS） |
| `onebot.py` | OneBot 协议适配（消息发送、群操作调用） |
| `remote.py` | 跨群远程群管执行 |
| `llm_tools.py` | LLM 调用封装（用于 AI 审核） |
| `constants.py` | 全局常量与配置项默认值 |
| `utils.py` | 通用工具函数 |
| `scheduler.py` | 定时任务管理与周期操作 |
| `_conf_schema.json` | 插件配置项的 JSON Schema 定义 |
| `metadata.yaml` | 插件元数据（版本、依赖、描述） |
| `version.json` | 独立版本号文件，用于动态更新显示 |
| `requirements.txt` | Python 依赖列表 |
| `CHANGELOG.md` / `SECURITY.md` / `LICENSE` | 版本历史、安全策略、开源许可（MIT） |

## 4. 开发约定

- **模块职责单一**：每个核心功能（刷屏、审核、申诉、成员管理）均拆分独立模块，降低耦合
- **配置与元数据分离**：插件选项通过 `_conf_schema.json` 声明，`metadata.yaml` 负责发布信息，`version.json` 单独管理版本号，便于自动更新检测
- **协议适配层抽象**：`onebot.py` 封装 OneBot 调用，其他模块通过该接口发送消息及执行群操作，方便未来切换协议实现
- **定时任务统一管理**：所有周期性操作（如解禁、数据清理）集中于 `scheduler.py`，避免分散各处
- **WebUI 前后端分离**：后端 `web.py` 仅提供 RESTful API，前端页面独立部署在 `pages/dashboard/` 目录，便于开发调试及换肤
- **安全审计要点**：管理指令在 `commands.py` 中进行权限校验；SQL 操作均通过参数化查询（`storage.py`）防止注入；LLM 调用在 `llm_tools.py` 中增加超时和错误回退
- **贡献与规范**：包含 `.github` 下的 Issue / PR 模板及 CI 工作流，鼓励社区参与；维护 `CHANGELOG.md` 和 `SECURITY.md`，遵循开源最佳实践