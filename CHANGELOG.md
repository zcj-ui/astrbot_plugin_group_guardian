# Changelog

## v2.5.1 - 2026-07-04

### 新功能

- **严格模式：群管指令仅群主/群管理员可用**（Issue #31）：新增 `member_action_require_group_role` 配置（默认关闭，可按群覆盖）。开启后 `/禁言` `/踢人` `/设置名片` 等群管指令要求操作者本群角色为群主或群管理员——即使是插件全局管理员，在其不是群管的群里也无法通过指令乱操作。查询指令与 WebUI 远程执行不受此限
- **`/查看违禁词` 指令**（采纳 PR #29 思路）：列出指令添加的自定义违禁词，可按脏话/广告分类筛选

### 说明

- PR #17 / PR #29 均基于旧代码分支，合并会回退 v2.5.0 的全部改进（PR #17 甚至会重新引入 #18/#19 的 MRO 崩溃、删除踢人撤回功能），其有价值的思路（审核管线拆分、自定义违禁词、`/查看`列表）已在本地实现并改进，建议关闭这两个 PR

## v2.5.0 - 2026-07-04

### 新功能

- **组合消息检测（防分段规避）**: 用户把违禁词拆成多条消息逐字发送（如 外/挂/进/群 四条）时，聚合该用户近期消息合并检测，命中后撤回全部涉及消息（Issue #27）。新增 `combine_detect_enabled` / `combine_detect_count` / `combine_detect_window_seconds` 配置
- **自定义违禁词**: 新增 `/添加违禁词 <脏话|广告> <关键词>` 和 `/删除违禁词` 指令（仅插件管理员），存入 SQLite 规则库即时生效；配置面板新增 `custom_swear_keywords` / `custom_ad_keywords` 列表（Issue #24, 吸收 PR #29 思路并改用规则库实现）
- **OCR 二维码强化**: default/strict 提示词模板要求视觉模型明确报告「图片包含二维码」及引导语，配合广告规则拦截扫码引流（Issue #26）

### 安全加固（ZhaisirAI 扫描 #32）

- **上传路径检查加固**: `upload_group_file_tool` 改用 `os.path.realpath` 解析符号链接后比较，并确保 uploads 目录存在（Critical #2）
- **表名注入防护**: `_ensure_column` 增加表名/列名白名单校验（Major #6）
- **WebUI 认证说明**: 明确注释 `register_web_api` 路由由 AstrBot Dashboard JWT 统一鉴权，插件层不重复实现（Critical #1 为框架层职责）

### 可靠性

- **后台重建重试上限**: `_background_full_rebuild` 连续失败 5 次后指数退避并退出，防止无限循环耗尽 CPU（Minor #4）
- **AC 自动机降级警告**: pyahocorasick 缺失时输出 warning 而非静默失效（Suggestion #1）

### 文档

- 黑白名单配置项描述完全重写，明确各名单的实际行为（Issue #28）

### 内部多智能体审查修复（发布前自查，7 个 P0 + 6 个 P1）

发布前经 5 视角对抗式审查（正确性/并发/安全/集成/功能逻辑）+ 逐条独立验证，修复以下自引入缺陷：

- **[P0] 新指令 100% 崩溃**：`_RULE_CATEGORY_MAP` 误作 `CommandsMixin` 类属性，因 Main 不继承该 Mixin 会 AttributeError（#18/#19 同源坑），改为模块级常量
- **[P0] 后台重建退避是死代码**：失败 sleep 后缺 `continue`，实际从不重试，已补
- **[P0] 删违禁词按 ID 跨分类误删**：新增 `get_moderation_rule` 先校验分类归属
- **[P0] 审核/防刷屏处罚互不感知**：双向交叉检查冷却，防止短时禁言覆盖长时禁言、解禁计划被缩短
- **[P0] 用户自定义词 ReDoS**：指令路径 `re.escape` 字面量化；WebUI 正则路径拒绝嵌套量词 `(x+)+` 等高危结构
- **[P0] AC 不可用时字面量词静默丢弃**：新增 `add_literal_keywords`，AC 缺失时降级为 `re.escape` 正则而非丢弃
- **[P0] 防刷屏关闭+组合检测开启内存泄漏**：`_anti_flood_data` 主动触发清理
- **[P1] 组合检测重复撤回当前消息 / 并发重复触发 LLM**：排除当前消息 ID + 60 秒处理冷却去重
- **[P1] combine_detect 缺 WebUI 范围校验**：补 `_config_int_ranges` 条目
- **[P1] 自定义词当正则致语义偏离**：改字面量处理，非法正则改为 warning 不静默吞

## v2.4.2 - 2026-06-29

### Bug 修复

- **[Critical] `_extract_at_targets` AttributeError**: 方法原定义在 `CommandsMixin`，但 `Main` 不继承该类（通过显式调用），导致 `self._extract_at_targets()` 抛出 `AttributeError`（Issue #18 #19）。已移至 `UtilitiesMixin`
- **禁言时长单位**: 改回以分钟为用户输入单位（`/禁言 @某人 30` = 30 分钟），内部自动 `*60` 转秒传给 OneBot，与 QQ 平台的分钟粒度一致

### 新功能

- **踢人自动撤回消息**: 新增 `kick_recall_enabled` 和 `kick_recall_count` 配置，踢人时自动撤回该成员最近消息（Issue #15）
- **LLM 调用超时保护**: 审核和 OCR 的 LLM 调用增加 60 秒 `asyncio.wait_for` 超时，防止 Provider 挂起阻塞整个审核管线（#14 Major #2）

### 重构

- **审核管线拆分**: `_handle_message` 从 ~320 行拆分为 8 个独立方法（#14 Major #1, PR #17 思路吸收）：
  - `_pre_check_message`: 同步前置检查（白名单/黑名单/功能开关）
  - `_handle_user_blacklist`: 用户黑名单处理
  - `_handle_qq_favorite`: QQ 收藏检测与撤回
  - `_parse_message_chain`: 消息链解析（文本/图片/转发/JSON/App）
  - `_apply_ocr`: OCR 图片识别
  - `_initial_screening`: 正则/词库初筛
  - `_execute_rule_penalty`: 规则违规处罚
  - `_execute_llm_penalty`: LLM 违规处罚
- **`_handle_message` 主函数降至 ~70 行编排逻辑**

### 优化

- **CSV 导出文件名安全处理**: 词库导出文件名过滤特殊字符，防止 HTTP 头注入（#14 Minor #6）
- **配置文件**: 新增 `kick_recall_enabled`、`kick_recall_count` 配置项

## v2.4.0 - 2026-06-26

### Bug 修复

- **[Critical] 权限系统重构**: 移除所有 21 处 `@filter.permission_type(ADMIN)` 框架级装饰器。该装饰器仅认 AstrBot 全局 `admin_id`，会阻断插件管理员、群主、群超管、F5 动态授权等非全局管理员使用命令（Issue #5）。权限校验现统一由插件内部 `_is_admin` 4 级权限系统处理，安全性不降低
- **[Critical] JSON/App 卡片消息审核遗漏**: 审核管线 `_handle_message` 步骤 B 未提取 json/app 类型消息段的文本内容，导致 JSON 卡片（分享链接、推广）和 App 消息完全绕过内容审核。新增 `_extract_json_card_text` / `_extract_app_card_text` 方法，提取 prompt/desc/title/jumpUrl 等字段送入审核
- **[Critical] 嵌套转发消息未递归解析**: 转发消息中包含的二级转发仅标记为 `[嵌套转发]` 而不提取内容，用户可通过多层转发嵌套规避审核。现支持最多 2 层递归解析嵌套转发内容
- **撤回通知不含违规原因**: `ban_notice` 模板现在支持 `{reason}` 变量，可展示具体违规原因
- **禁言时长 double-read**: `_mute_member` 未接收 duration 参数导致重复读取配置，现在审核管线直接传入已计算的 ban_duration
- **`cmd_revoke_admin_perm` 权限检查不完整**: 仅检查 `_get_admin_list()` 而遗漏 AstrBot 全局管理员，改用 `_is_plugin_admin`
- **`recall_last` / `recall_all` 冗余权限检查**: 手写 3 步权限校验与 `_prepare_group_action` 重复，统一改用后者；`recall_all` 同时修复 @ 目标解析
- **申诉非文字消息无反馈**: 用户发送图片/语音申诉时，仅首次提示后续无反馈，现每次非文字消息均回复提示
- **调度器崩溃不恢复**: `_scheduler_loop` 异常后任务终止不再重启，现增加连续错误计数和自动暂停恢复机制

### 功能变更

- **禁言时长单位改为秒**: `/禁言`、`/批量禁言` 命令及 `ban_group_member` / `batch_ban_members` LLM Tool 的时长参数从分钟改为秒，与 OneBot 协议和 QQ 平台原生单位一致（Issue #6, PR #7）
- **日志预览扩展**: `msg_preview` 从 100 字符扩展到 200 字符，WebUI 审核日志列表展示更完整
- **代码重构**: `_is_admin` / `_is_plugin_admin` 提取公共方法 `_get_all_admin_ids()` / `_is_group_admin_blocked()`，消除重复代码

### 新功能

- **指令支持 @某人**: `/禁言`、`/解禁`、`/踢人`、`/设置名片` 现在支持 @某人 而非仅限手动输入 QQ 号（Issue #8, PR #9）
- **入群审核群内通知**: 自动审核通过/拒绝后在群内发送通知，新增 `join_audit_notify` 配置开关（Issue #12）
- **入群审核不再吞没事件**: `_on_group_request` 仅在实际处理了申请时才 `stop_event()`，未启用审核时不阻断其他插件

### WebUI 优化

- **统计面板增强**: `_web_stats` 不再每次查库计算规则数量，改用内存匹配器属性；新增 `configured_groups_count`、`super_admin_count` 字段
- **群列表增强**: 每个群新增 `has_config` 字段标记是否有独立配置；今日拦截统计不再限制只在白名单群内
- **今日统计增强**: `_web_today_stats` 新增 `block_rate` 拦截率和群名映射，前端无需二次查询
- **日志查询增强**: `_web_get_logs` 支持 `group_id` / `user_id` / `action` 过滤参数和 `offset` 分页，返回 `total` 总数
- **用户聚合增强**: `_web_get_moderation_users` 支持 `group_id` 过滤，返回用户涉及群数和群列表
- **已配置群列表**: `_web_configured_groups` 返回每群覆盖配置项数量

### 多群管理优化

- **配置分类标签**: `_web_get_group_config` 每项新增 `category` 分类字段（基础开关/审核规则/防刷屏/夜间限速/OCR/申诉/入群审核等），前端可按分类分组展示
- **批量设置 API**: 新增 `/group_config/batch_set` 接口，一次设置多个配置项
- **跨群复制配置**: 新增 `/group_config/copy` 接口，从源群复制全部独立配置到目标群

## v2.3.4 - 2026-06-07

### 功能增强

- **凌晨/夜间独立限速**：防刷屏支持在指定夜间时段使用独立的每秒 / 每分钟 / 每小时消息上限。默认关闭；开启后按服务器本地时间判断，默认 00:00-06:00 使用 3/10/30 阈值。
- **WebUI 夜间限速配置**：设置页新增夜间限速开关、开始/结束小时和夜间三档阈值；刷屏监控页显示夜间规则是否生效及当前夜间阈值。
- **WebUI 防刷屏说明优化**：设置页和监控页明确展示“最近 1 秒 / 60 秒 / 3600 秒”的滑动窗口语义，避免误解为当天累计；监控页预警/关注状态改为按当前配置和夜间生效阈值动态计算，不再使用写死阈值。
- **重复消息检测说明与范围对齐**：重复消息窗口明确为“最近 N 秒内同一内容重复”，默认 120 秒、最大 3600 秒；后端保存范围与运行时均收紧到 3600 秒，避免误解为一天累计。

### 文档与版本

- **README 同步当前功能状态**：更新当前版本、WebUI 页面、配置表和路线图，补齐入群审核、申诉/解禁、多群配置、动态授权等已交付能力。
- **版本号统一到 v2.3.4**：同步 `version.json`、`metadata.yaml`、`constants.py` 与 Dashboard 兜底版本。

### 修复

- **申诉文字提示原子化**：`请用文字说明你的申诉理由。` 通过 SQLite 条件更新保证只发送一次，避免并发私聊空事件重复提示。
- **私聊申诉入口收紧**：仅真实用户私聊消息进入申诉流程，notice/request/meta 等非消息事件直接忽略；图片、表情、戳一戳、CQ 图片码等非文字内容不再触发 LLM 复核。

## v2.3.3 - 2026-06-07

### WebUI 稳定性与加载修复

- **修复 Page Bridge 初始化偶发失败**：Dashboard 现在显式引入 AstrBot `bridge-sdk.js`，等待 `window.AstrBotPluginPage.ready()` 后再绑定 `apiGet` / `apiPost`，避免页面偶发显示 `Bridge 未加载`、`API 不可用` 或长时间停留在连接状态。
- **所有 WebUI API 调用增加前端超时保护**：`safeGet` / `safePost` 统一封装 12 秒超时、错误归一化与 API 不可用提示，接口异常时不再让页面无反馈卡住。
- **配置加载去重缓存**：多个页面模块共用同一次 `config` 请求，保存配置后自动失效缓存，减少重复请求和初始化抖动。
- **修复大量错误响应误判**：统一使用 `isApiError()` 判断后端错误，避免接口返回失败时前端仍提示“成功”。
- **增强 DOM 绑定容错**：初始化阶段和规则/名单中心的固定按钮改为防御式绑定，缺少某个节点时不会中断整个 Dashboard 初始化。
- **修复移动端底部导航缺项**：补齐入群、权限、多群、任务等移动端入口，并改为横向滚动，避免小屏幕下功能页无法进入。

### 性能优化

- **群列表 / 群成员列表增加短 TTL 缓存与 OneBot 超时**：WebUI 获取群列表和群成员时分别增加 20 秒 / 15 秒缓存，并对 OneBot API 设置 8 秒 / 10 秒超时；超时时优先返回旧缓存，避免切换页面时反复阻塞。
- **批量删除日志不再拉取全量导出**：新增按 `user_ids` 删除审核日志的后端能力，WebUI 批量删除用户记录时不再先请求 10 万条 `logs/export`，大幅降低卡顿与内存占用。
- **SQLite 批量删除分块执行**：按日志 ID 或用户 ID 删除时分批处理，避免 SQLite 参数数量超限。
- **Dashboard 图表请求并发化**：趋势、分布、时段、群排行等图表并发加载，减少仪表盘等待时间。

### 逻辑修复

- **修复词库全局开关与单群覆盖冲突**：词库改为编译所有有效分类，审核阶段再按全局/单群配置过滤；同时修复单分类增量重建仍按全局开关删除编译缓存的问题，避免“全局关闭、某群开启”的分类在保存词库后失效。
- **入群审核按群过滤词库分类**：`join_reject_use_lexicon` 命中违禁词库时也会尊重该群的词库分类开关，不再出现普通消息不拦、入群申请仍按已关闭分类拒绝的错位。
- **定时自动解禁按群配置生效**：登记解禁和执行到期解禁时均按目标群读取 `auto_unban_enabled` 与永久禁言托管时长；扫描间隔保持全局调度语义。
- **多群配置项与实际运行逻辑对齐**：过滤不适合单群覆盖的全局项，支持 `bool` / `int` / `string` / `text` 的配置类型，保存时统一做范围限制和选项校验。
- **配置读取容错增强**：布尔字符串、空字符串、整型范围、字符串枚举选项统一归一化，避免配置文件或 WebUI 输入类型不稳定导致逻辑偏差。
- **群管/远程操作更多前置校验**：批量禁言、批量踢人、远程操作等路径继续复用目标角色与机器人权限预检，减少必然失败的 OneBot 调用。

### 安全与渲染修复

- **修复 WebUI 属性转义问题**：`data-*`、`title` 等 HTML 属性统一使用属性级转义，降低特殊字符导致 DOM 结构错乱的风险。
- **CSS 选择器参数安全化**：按成员 QQ 定位输入框时使用 `CSS.escape` 兜底，避免特殊值破坏选择器。
- **后端配置保存校验更严格**：字符串配置带 `options` 时拒绝非法值，整型配置统一限制到安全范围。

### 验证

- 全量 `python -m py_compile` 通过。
- Dashboard 内联脚本通过 Node `vm.Script` 解析校验。
- `git diff --check` 通过。
- 前后端 API 契约检查通过：前端 46 个 API 调用均有后端路由，GET/POST 无错配。

## v2.3.2 - 2026-05-31

### 权限体系修正（重要）

- **区分“群操作权限”与“插件全局管理员”**：群主 / 群管理员通过群角色获得的权限，现在**仅在白名单群**（未设白名单时为非黑名单群）内生效，且只授予**该群的群管操作权限**，不再等同于插件全局管理员。
- **修复提权漏洞**：此前 `/设置管理插件`（管理全局插件管理员名单）与 `/自动审核`（全局运行开关）使用统一的管理员判定，白名单群群主可借此把任意人提升为全局插件管理员。现新增 `_is_plugin_admin` 判定（仅认全局名单 + AstrBot admin_id），上述插件级操作改用它校验，群角色授权者无法执行。
- `/群管理授权`（F5）收紧为“群主或插件管理员”可改，并修正文案为“本群群管操作权限”。
- `_conf_schema.json` 中 F5 两项配置文案同步修正，明确为“本群群管操作权限（非插件全局管理员）”。

### 功能增强

- **`/设置管理` 支持 @ 与设置/取消**：可用 `/设置管理 @某人 设置`、`/设置管理 <QQ号> 取消` 等形式设置或取消**群管理员**。权限限制为**白名单群的群主**或**插件管理员**，普通群管无法借此扩张管理层。新增 `_extract_at_targets` 从消息链解析被 @ 的 QQ。

### WebUI 体验优化

- 新增通用弹窗组件 `confirmModal()`（二次确认，支持危险样式）与 `promptModal()`（输入框）。
- 高危操作由“点击即执行”改为弹窗二次确认：设为 / 移除插件管理员、加入群黑名单、移除群超管、删除动态授权配置、恢复 bot 管理权限，显著降低误操作风险。

### 验证

- 全量 `python -m py_compile` 通过；`_conf_schema.json` JSON 校验通过；Dashboard 内联 JS 经 `node --check` 通过。
- 权限逻辑经离线探针验证 11 个场景（白名单群主有权 / 非白名单群主无权 / 群主非插件管理员 / 群级权限黑名单剥夺等）全部符合预期。

## v2.3.1 - 2026-05-31

### Bug 修复（防刷屏 / 内容审核 / 申诉的重复处罚）

本次修复一类共性问题：处罚或裁决在 `await` 外部操作（禁言、撤回、LLM 复核、解禁）期间未锁定状态，导致事件队列里的积压消息 / 用户连发消息重复触发，造成"被处罚后人已发不出消息，却仍被反复禁言并反复要求申诉"等现象。

- **防刷屏重复处罚修复**：新增处罚冷却机制（`anti_flood.py`）。用户被刷屏处罚后进入冷却期，期间其积压 / 后续消息只静默忽略，不再重复禁言 / 撤回 / 记日志 / 开申诉。冷却登记时同步清空该用户计数队列，避免冷却结束后窗口内残留旧消息立即再次命中。冷却时长取禁言时长（仅撤回不禁言时取一个保底值，最小 30 秒），并纳入定期清理回收。
- **内容审核重复处罚修复**：黑名单 / 正则直接处置 / LLM 违规处置在连发多条消息时，违规消息仍逐条撤回，但禁言 / 通知 / 登记定时解禁在冷却期内只执行一次，消除"重复禁言同一人 + 刷多条通知"的噪音。
- **私聊申诉并发裁决修复**：用户连发多条私聊申诉时，原先每条都会进入裁决并各自调用一次 LLM 复核、重复解禁、重复回复。改为基于 SQLite 的原子抢占（`claim_appeal`，waiting→judging 的 CAS），只有第一条能抢到裁决权，后续请求静默退出。复核出错回滚为 waiting 允许窗口内重试；超时清理同时回收因崩溃 / 重载卡在 judging 的申诉，避免用户永久无法再申诉。

### 验证

- 全量 `python -m py_compile` 语法校验通过。
- 防刷屏：连发 12 条仅处罚 1 次，其余进入冷却静默，队列清空。
- 内容审核：连发 8 条违规 → 撤回 8 次、禁言 / 通知各 1 次；黑名单连发 5 条仅处理 1 次；冷却到期自动恢复。
- 私聊申诉：连发 5 条私聊仅 1 条抢到裁决权；回滚后可重试；终态后不可再抢。

## v2.3.0 - 2026-05-31

### 修复与增强（群管操作）

- **禁言可设置时长 + 新增解禁按钮**：WebUI 群成员列表每行新增时长输入框（分钟）与「解禁」按钮，不再写死 10 分钟。
- **群管操作前置权限检测**：禁言/踢人/设名片/头衔/设管理等写操作执行前，先检测机器人自身在该群是否为管理员/群主，以及目标角色——对群主一律拦截、bot 非群主时不能操作其他管理员，并返回清晰提示（不再静默失败）。指令端与 WebUI 端均生效。
- **名单类输入支持批量**：插件管理员、群白/黑名单、用户黑/白名单、群超管、群权限黑名单的添加框均支持一次输入多个 QQ/群号（逗号或空格分隔）。

### 重大更新

- **多群独立配置**：新增 `group_configs` 表与 WebUI「多群配置」页。**所有通用配置**（76 项：全部功能开关、审核/词库/防刷屏/申诉/入群开关与阈值、封禁通知模板等）均可按群单独配置，未设置的项**继承全局默认**。布尔项支持「继承/强制开/强制关」三态，整型项留空即继承。**仅白名单群可配置**（白名单为空时所有群可配）。审核管线、防刷屏、词库分类、群管功能开关（`_cfg_check`）全程按群读取配置，真正实现多群多策略。排除项：名单类、Provider 选择、免责声明、暗色模式（全局语义）。
- **单群管理类名单迁移到 SQLite**：群白名单、群黑名单、用户黑名单、审核白名单、插件管理员 5 类名单从配置文件迁移到 `managed_lists` 表，统一由 WebUI 管理；配置 schema 中对应项设为隐藏（仅作升级兼容兜底）。首次启动自动从旧 config 迁移（DB 为空时回退读旧值，杜绝升级丢管理员）。
- **入群自动审核**：监听加群申请，按「通过词 / 拒绝词 / 违禁词库」自动通过或拒绝，其余按默认动作（转人工 / 自动通过 / 自动拒绝）。规则支持按群配置（`join_audit_rules` 表），WebUI「入群审核」页可视化增删改。
- **刷屏申诉工作流（F2）**：开启后，刷屏处罚时群内 @ 当事人，要求其私聊机器人说明原因；私聊触发后 LLM 结合「申诉理由 + 该用户群内最近 30 条上下文 + 原处罚」复合审核，成立则自动解禁、不成立维持、超时维持。状态机存于 `appeals` 表，WebUI「申诉/解禁」页可查。
- **定时自动解禁（F3）**：插件发起的禁言可登记到期解禁，后台任务按 `auto_unban_scan_interval` 秒轮询执行，重启后从 `scheduled_unbans` 表恢复。永久禁言按 `auto_unban_permanent_hours` 托管解禁。
- **批量管理（F4）**：新增 `/批量禁言`、`/批量踢人` 指令与 `batch_ban_members`、`batch_kick_members` LLM 工具；WebUI 群成员列表支持多选后批量执行（禁言/解禁/踢/设名片/设头衔/设管理/取消管理），单次上限 50 人。
- **动态群管理员授权（F5）**：可按群开启「群主/群管自动成为该群插件管理员」，被下管理后约 10 秒内自动失效（角色缓存 TTL 由 300s 收紧到 10s，缓存存角色而非布尔）。授权对象（群主/管理员）可分别开关。
- **群超管**：WebUI 可为指定群单独设置插件管理员（仅该群生效）。
- **群权限黑名单**：群主可移除本群某群管的 bot 管理权限（`/移除群管权限`、`/恢复群管权限`，优先级最高），WebUI「权限管理」页可视化维护。
- **WebUI 远程执行**：「群管理」页选择群成员后，可远程执行任意群管操作（禁言/解禁/踢/名片/头衔/精华/公告/群名/全体禁言/撤回等），支持单个与批量。

### 配置迁移

- **单群管理类名单迁移到 SQLite**：群白名单、群黑名单、用户黑名单、审核白名单、插件管理员 5 类名单从 `_conf_schema.json` 迁移到 `managed_lists` 表，WebUI 增删改一律走 DB。首次启动自动从旧 config 迁移（`meta.lists_migrated` 标记，DB 为空时回退读旧值，杜绝升级丢管理员）。
- 全局标量开关、阈值、模板、Provider 选择仍保留在 `_conf_schema.json`（AstrBot 标准机制）。

### 新增配置项

- 入群审核：`join_audit_enabled`、`join_accept_keywords`、`join_reject_keywords`、`join_reject_use_lexicon`、`join_default_action`、`join_reject_reason`
- 申诉：`appeal_enabled`、`appeal_window_minutes`、`appeal_context_count`、`appeal_at_template`
- 定时解禁：`auto_unban_enabled`、`auto_unban_scan_interval`、`auto_unban_permanent_hours`
- 动态授权：`group_admin_grant_enabled`、`legacy_role_admin_enabled`
- 所有新功能开关**默认关闭**，不影响现有用户行为。

### 新增模块

- `membership.py`（入群审核）、`appeal.py`（申诉工作流）、`scheduler.py`（后台调度：定时解禁 + 申诉超时清理）、`remote.py`（WebUI 远程执行统一入口）。
- `storage.py` 新增表：`join_audit_rules`、`appeals`、`scheduled_unbans`、`group_admin_grant`、`managed_lists`、`group_super_admins`、`group_admin_block`。

### 性能优化与重构

- **审核热路径去重**：`_handle_message` 中 `user_id`/`user_name` 由每条消息取 3 次改为开头取一次贯穿全程。
- **重构 `_call_llm_safe`**：130 行 4 层闭包嵌套拆分为 `_invoke_provider_methods` / `_call_llm_by_provider_id` / `_LLMErrorBag` 三个清晰单元，行为不变。
- **防刷屏去重遍历**：`_check_anti_flood` 的「重复消息」分支不再二次遍历队列，主循环内同步收集消息 ID。
- **词库编译去重**：`_compile_lexicon` 与 `_lexicon_category_enabled` 重复的 switch_map 合并为 `_lexicon_switch_map()`，关键词拆解提取为 `_extract_lexicon_parts()`。
- 角色缓存改存「角色字符串」，支持 F5 授权配置变更即时生效。

### Bug 修复

- **修复 `web.py` 导出词库 CSV 崩溃**：`_web_export_lexicon_keywords` 调用了未导入的 `send_file`，改为与导出日志一致的元组返回（带 UTF-8 BOM，Excel 中文不乱码）。
- **修复 `tuple[bool, str]` 注解兼容性**：`web.py` 两处增量重建方法使用了 Python 3.9+ 的小写下标泛型，改回 `typing.Tuple`，兼容 3.8。
- **清理死代码**：移除 `utils.py` 中 pickle 缓存残留（`cache_dir`/`db_mtime`/空操作的 `_invalidate_lexicon_cache`）及无用 `Path` 导入。

### WebUI

- 新增「入群审核」「权限管理」「申诉/解禁」三个标签页，沿用主面板 GitHub Primer 风格与 SVG 图标。
- 群成员列表新增单个/批量远程操作能力。
- 重做插件 Logo（盾牌 + 对勾，蓝色渐变，更精致）。

### 验证

- 已通过 `python -m py_compile` 全量语法校验、`_conf_schema.json` JSON 校验、dashboard 内联 JS 的 `node --check`。
- 已通过离线集成测试：mixin 导入、Main MRO 组装、storage 新表增删改查、远程执行端到端、权限判定。
- 注：开发环境无 AstrBot 运行时，真机功能（加群事件字段、私聊申诉、主动消息）需用户在 NapCat/LLOneBot 等协议端实测。

## v2.2.7 - 2026-05-27

### 新增

- **单用户审核白名单**：新增 `user_white_list` 配置，名单内用户自动跳过审核与防刷屏检测，优先级高于用户黑名单。
- **WebUI 白名单管理**：设置页新增“审核白名单用户”管理区，支持添加/移除；后端新增 `/user_whitelist/add`、`/user_whitelist/remove` API。

### 优化

- **运行态统计补充**：`/stats` 与 `/config` 返回中补充审核白名单数据（数量与列表），便于 WebUI 统一展示。
- **配置热更新同步**：`/config` 保存后同步维护 `_user_white_set`，无需重启即可生效。

### 维护修复

- **规则中心空白修复**：切换到“规则中心”时先渲染页面骨架再加载数据，避免 `#ruleRebuildStatus` 尚未创建导致的 `textContent` 空指针异常。
- **重建状态轮询修复**：重建状态轮询改为在“规则中心”激活时触发，不再误绑定到“设置”页。
- **设置页分区重构**：设置页新增分区切换（核心/审核/OCR/词库/功能/名单/高级/全量），默认按分区展示，降低信息密度与滚动负担。
- **规则中心移动端优化**：热更新区域改为双栏卡片布局（移动端自动单栏），工具栏按钮与输入框响应式重排，手机端可读性显著提升。
- **规则中心视觉增强**：新增状态卡片、面板过渡动画与重建状态微动效，减少“纯表格堆叠”观感并提升交互反馈。
- **规则筛选按钮状态修复**：规则中心分段按钮点击后即时更新激活态样式，不再出现“按钮点了和没点一样”的视觉反馈缺失。

## v2.2.6 - 2026-05-27

### 新增

- **规则中心分类搜索**：关键词分类选择从原生下拉升级为可搜索输入（`datalist`），分类较多时可直接输入并回车切换。
- **批量导入反馈明细**：`/lexicon/keywords/add_batch` 新增返回输入总数、去重后数量、新增数量、重复数量（输入内重复/库内重复）及重复样例。

### 优化

- **规则中心状态保持**：左侧规则筛选切换时不再整块重绘，改为仅刷新规则列表，减少右侧词库上下文丢失。
- **设置页请求降噪**：`loadSettings()` 不再自动触发规则中心接口，避免切换设置页时产生无效规则/词库请求。

### 维护

- **词库判重查询能力补充**：`SQLiteStorage` 新增 `list_existing_lexicon_keywords()`，支持大批量关键词分片查询，避免 SQLite 参数上限问题。
- **版本一致性更新**：同步 `constants.py`、`metadata.yaml`、`version.json`、README 到 `v2.2.6`。

## v2.2.5 - 2026-05-27

### 违禁词热更新（主功能）

- **审核规则热更新**：新增审核规则分页查询、增删改、启停接口，支持脏话/广告规则通过 WebUI 即时生效。
- **词库关键词热更新**：新增分类统计、分页查询、新增/删除关键词接口，支持按分类增量重建 AC 自动机。
- **增量 + 异步双重重建**：保存规则/关键词后先重建目标分类，再后台异步全量校验重建，并在前端显示重建状态。
- **WebUI 规则中心**：设置页新增“规则热更新”区域，支持规则搜索、编辑、启停、删除，以及关键词分类管理。

### 新增

- **重复消息检测**：新增同内容重复发送检测，支持配置检测窗口（秒）与触发次数，达到阈值后按防刷屏策略执行禁言/撤回。
- **长文本刷屏检测**：新增单条消息长度阈值检测，超长文本可直接触发防刷屏处理，防止大段文本刷屏。

### 配置

- 新增 `repeat_detect_enabled`、`repeat_detect_window_seconds`、`repeat_detect_count` 三项重复消息检测配置。
- 新增 `long_text_detect_enabled`、`long_text_threshold` 两项长文本刷屏检测配置。

### 优化

- 防刷屏消息记录结构扩展为 `(时间戳, 消息ID, 归一化文本, 文本长度)`，并兼容旧结构自动迁移读取。
- 防刷屏主流程接入格式化后的消息文本（含转发/QQ收藏/图片等），提升非纯文本消息场景下检测准确性。

### 维护修复

- **移除不安全反序列化**：停止使用 `pickle.load` 读取运行时 AC 缓存，规避可写数据目录中的反序列化执行风险。
- **后台重建任务可取消**：插件卸载/热重载时主动取消未完成的规则重建任务，避免旧实例残留后台协程继续运行。
- **重复关键词不再假成功**：新增已存在关键词时返回明确错误，不再触发无意义的增量/后台重建。
- **规则热更新异常日志补全**：规则/关键词增删改关键路径在未知异常时记录 `logger.exception(...)`，便于排障。
- **前端重建轮询去重**：Dashboard 仅注册一个重建状态轮询定时器，避免重复初始化时叠加请求。
- **布尔配置解析修复**：Web API 不再把字符串 `"false"` / `"0"` 误判为 `True`，热更新接口统一按显式布尔语义解析。
- **整型配置边界补全**：防刷屏、重复消息、长文本等整型配置增加服务端范围限制，避免通过 API 写入负数或异常值。
- **重复规则提示友好化**：规则新增/更新命中唯一约束时返回“规则已存在”，不再直接暴露底层 SQLite 异常文本。
- **全量词库接口默认轻量化**：`/lexicon` 默认返回分类摘要，只有显式 `full=1` 时才返回完整词库，降低大响应开销。
- **全量配置区整型输入修复**：Dashboard Schema 配置区不再用 `|| 0` 吞掉非法输入与显式 `0` 的差异。
- **热更新一致性改进**：规则/关键词写入数据库后，若增量重建失败会自动切换为后台全量重建，并向前端返回 `deferred` 状态，避免“数据库已更新但运行态未同步”的静默不一致。
- **词库开关变更改为增量生效**：配置页切换 `lexicon_*` 时不再同步全量重编译，而是按分类增量重建并后台校验，减少 WebUI 保存阻塞。
- **规则更新命中检查**：更新不存在的规则 ID 时返回明确错误，不再误报成功。
- **配置持久化告警补全**：当配置对象不支持 `save_config()` 时写出告警日志，避免修改只留在内存里却无提示。

## v2.2.4 - 2026-05-27

### 修复

- **LLM 上下文常量缺失**：补回 `CONTEXT_MESSAGE_MAX_CHARS` 和 `CONTEXT_TOTAL_MAX_CHARS`，避免 LLM 二次审核读取群聊上下文时触发 `NameError`。
- **版本号一致性**：同步 `metadata.yaml`、`constants.py`、`version.json`、README 与 Dashboard 兜底版本显示到 `v2.2.4`。
- **群管提示词注入开关生效**：新增 `on_llm_request` 钩子，在启用且免责声明已同意时，为本轮 LLM 请求追加群号、调用者权限和群管工具安全规则；关闭 `prompt_injection_enabled` 后不再注入。

### WebUI

- 将设置页中的“防注入检测”文案调整为“群管提示词注入”，避免用户误解为独立的恶意 Prompt 检测模块。

### 维护优化

- **防刷屏禁言逻辑修复**：`anti_flood_mute_duration=0` 时不再调用禁言 API，按配置执行“仅撤回不禁言”，并调整日志动作为“刷屏处理”。
- **词库重编译触发条件显式化**：仅在 `lexicon_*` 配置项实际变更时重编译 AC 自动机，避免误触发同步阻塞。
- **AC 缓存反序列化校验**：加载 `ac_cache/*.pkl` 后增加 `KeywordAutomaton` 类型校验，缓存损坏/不兼容时自动回退重建。

## v2.2.2 - 2026-05-25

### 性能优化

- **AC 自动机磁盘缓存**：`_compile_lexicon` 首次构建后 pickle 到 `ac_cache/<category>.pkl`，后续重载仅比较 DB 文件 mtime，未变化则 `pickle.load` 秒加载，重载从 3-4 秒缩短至 < 1 秒
- 词库更新（`lexicon.db` 或运行时 DB 变更）自动触发缓存失效重建，无需手动清理
- `_check_anti_flood` 逆向单次遍历：最新→最旧，越过 3600s 立即 break，O(命中范围)
- 三档限流全设 0 时零开销跳过的早返回

### 架构改进

- **提取 `_anti_flood_guard` 方法**：防刷屏 30 行内联逻辑封装为独立方法，返回 `(blocked, notice)`，`_handle_message` 调用后由管线 yield 通知
- 防刷屏检测归位到白名单检查**之后**：黑名单群跳过、白名单空则全群生效、白名单非空则仅白名单群生效，避免非目标群触发 `_is_admin` API 调用

### WebUI

- **设置页配置补全**：`_web_get_config` 对新配置项回退到 schema 默认值，首次加载即可看到防刷屏开关
- Dashboard 设置页「审核设置」新增 2 个防刷屏开关（总开关 + 撤回开关），「其他设置」底部新增防刷屏数值配置（秒/分/时上限、禁言时长、撤回阈值）
- Dashboard 新增「刷屏监控」Tab：实时追踪每群每人的消息速率，预警（红）/ 关注（黄）/ 正常（绿）状态标签
- Web API 新增 `/anti_flood/status` 端点，返回追踪数据快照

### 修复

- 修复 `loadFlood is not defined`：函数定义从 `init()` 内部提升到全局作用域
- 修复 `h is not defined`：HTML 转义改用 `esc()` 函数
- 修复非白名单群仍在调用防刷屏逻辑（`_is_admin` API）
- 修复 `_web_get_config` 不返回 schema 默认值导致新配置项前端不可见

## v2.2.1 - 2026-05-25

### 重大更新

- **防刷屏检测**：新增 `anti_flood.py` 模块，追踪所有消息类型（转发/QQ收藏/图片/JSON等）的发送频率，支持按每秒/每分钟/每小时独立设置速率上限，超限自动禁言并可选批量撤回

### 新增配置（7项）

- `anti_flood_enabled` — 防刷屏总开关（默认开启）
- `anti_flood_rate_per_second` — 每秒消息上限（默认5条）
- `anti_flood_rate_per_minute` — 每分钟消息上限（默认20条）
- `anti_flood_rate_per_hour` — 每小时消息上限（默认60条）
- `anti_flood_mute_duration` — 刷屏禁言时长秒数（默认300秒）
- `anti_flood_recall_enabled` — 是否撤回刷屏消息（默认开启）
- `anti_flood_recall_threshold` — 撤回阈值，超限条数达此值才撤回（默认20条）

### 架构

- **新增 `anti_flood.py`**：`AntiFloodMixin` 类，基于 sliding window 追踪 `(时间戳, 消息ID)`，自动清理过期数据，所有消息类型均计入不区分文本/图片/转发
- **管线集成**：防刷屏检查在管理员豁免后、内容审核前执行，管理员不受防刷屏限制

## v2.2.0 - 2026-05-24

### 重大更新

- **Aho-Corasick 自动机匹配**：66,993 条词库关键词从正则引擎替换为 `pyahocorasick` 自动机，单次扫描 O(n+命中数) 而非 O(n·m)，长文本审核性能提升百倍以上
- **脏话/广告规则统一 HybridMatcher**：13 条脏话 + 517 条广告规则拆解为纯文本后优先走 AC，无法拆解的（含 `\s*`、`\d{5,12}`、`.{0,N}` 等语法）保留正则回退
- **新增 `automaton.py` 模块**：`KeywordAutomaton`（纯文本 AC）+ `HybridMatcher`（AC 优先 + 正则回退）+ `regex_to_literals()`（正则拆解器）
- **新增依赖**：`pyahocorasick>=2.1.0`

### 移除

- **所有正则级联拼接代码**：移除 `_build_combined_regex()`、`_build_combined_regex_from_escaped()`、`import re`（moderation.py 保留自身逻辑所需）
- **死代码全量清理**：移除 `patterns.py`（已删除）、`_POLITICAL_WHITELIST` 引用、无用 import（`asyncio`/`json`/`datetime`）、未使用变量 `card`

### Bug 修复

- **词库关键词 AC 前误调 `re.escape()`**：导致含 `.cn` 等字符的关键词永远匹配不上
- **`commands.py` 类型不匹配**：`_compiled_lexicon.get("political")` 是 `KeywordAutomaton` 但被当作 `List[re.Pattern]` 迭代 → 运行时 `TypeError` 被 except 静默吞掉
- **`web.py` 无用 import**：`import time` `import json` `from datetime import datetime`
- **`storage.py` 外键失效**：`PRAGMA foreign_keys=ON` 未设置，级联删除不生效

## v2.1.0 - 2026-05-24

### 重大更新

- **数据统计仪表盘**：新增 WebUI 数据分析看板，包含每日拦截/放行趋势图、违规类型分布、24小时时段分布、群拦截排行
- **SQL 分析查询**：`get_daily_trend`、`get_violation_distribution`、`get_group_activity_ranking`、`get_hourly_distribution` 四个统计接口
- **内置词库 DB 化**：`lexicon.db` 新增 `moderation_rules` 表（13 条脏话正则 + 517 条广告正则），正则规则从硬编码改为 SQLite 存储；`storage.py` 初始化时从内置 DB 同时加载词库和正则规则
- **正则规则热载就绪**：`moderation_rules` 表支持在线增删改正则规则，后续版本可通过 WebUI 管理

### 修复

- **注册顺序修正**：`@register` 装饰器在命令绑定完成后执行，避免 AstrBot 扫描到空类
- **模块归属修正**：Handler 统一显式注册在 main.py，去除子模块装饰器残留
- **_handle_message 修复**：事件监听改用 `async for/yield` 转发 async generator，修复 `await` 报错
- **CSS 媒体查询修复**：修复手机端样式脱离 `@media` 块导致全局覆盖

### 代码规范

- **源码大规模注释**：所有 10 个 Python 源码文件补充了完整的行内业务注释
- **模块职责清理**：移除图片代理接口、aiohttp 依赖等已废弃代码
- **命令注册重构**：管理命令补全 `PermissionType.ADMIN` 权限校验

## \[2.0.0] - 2026-05-23

### 重大更新

- **SQLite 存储上线**：新增 `group_guardian.db`，审核日志改为写入 SQLite，支持按日志 ID、时间、群号、用户 ID、操作类型建立索引
- **外置词库 DB 化**：新增 `lexicon.db`，将原 `lexicon.json` 中 66,993 条关键词迁移为 SQLite 表结构，并移除仓库中的 JSON 词库文件
- **WebUI 迁移助手**：设置页新增 SQLite 迁移助手，展示数据库、旧日志 JSON、内置词库 DB 状态
- **迁移警告与确认机制**：执行迁移前必须输入确认文本；迁移会删除旧 `moderation_logs.json` 并保留 `.bak` 备份

## \[1.9.10] - 2026-05-23

### 紧急修复

- **修复插件加载失败**：`StarTools.get_data_dir()` 不能在拆分后的 `utils.py` 中直接调用，否则 AstrBot 会按 `utils` 子模块查找插件元数据并失败；现在改为在 `main.py` 初始化阶段获取数据目录，并由工具模块复用该路径

## \[1.9.9] - 2026-05-23

### 架构重构

- **模块拆分**：将 3400+ 行 `main.py` 拆分为 `web.py`、`utils.py`、`moderation.py`、`llm_tools.py`、`commands.py`、`onebot.py`、`constants.py`，`main.py` 仅保留插件注册、初始化和生命周期逻辑
- **装饰器兼容处理**：保留所有命令、事件监听和 LLM Tool 装饰器，并将装饰器方法显式绑定回 `Main`，兼容 AstrBot 可能只扫描主插件类方法的加载方式
- **版本常量独立**：新增 `constants.py` 统一管理插件名称和版本号，避免多处硬编码

### 安全与稳定性修复

- **WebUI API 返回解包修复**：前端统一解包 `{status, data}` 响应，修复统计、日志、配置等接口数据结构不匹配问题
- **审核日志 ID 修复**：日志 ID 改为单调递增，避免 `deque(maxlen=500)` 满后 ID 重复导致详情、删除定位混乱
- **异步日志写入异常处理**：保存日志的 executor 任务增加完成回调，避免后台写入异常被静默吞掉
- **OneBot 返回值兼容**：统一解析 OneBot API 的 `data` 包装，修复群成员、群文件、历史消息、合并转发等接口返回值兼容问题
- **图片代理域名校验加固**：域名校验改为精确匹配或子域匹配，避免 `evilqpic.cn` 这类后缀绕过\[已下线]
- **权限体系统一**：移除 AstrBot 全局 ADMIN 装饰器依赖，统一使用插件内管理员/群管理员判断，避免插件管理员无法触发命令；危险写操作仍要求管理员权限\[已取消]

### 配置更新

- **新增** **`word_count_enabled`** **配置项**：可在 WebUI 控制 `/字数统计` 是否开放，默认关闭
- **新增** **`group_stats_enabled`** **配置项**：可在 WebUI 控制 `/群统计` 是否开放，默认开启
- **配置默认值修复**：`_cfg()` 会优先读取 `_conf_schema.json` 中的默认值，避免代码默认值与配置 schema 不一致

### 验证

- 已通过 `python -m py_compile` 语法检查
- 已通过 `_conf_schema.json` JSON 校验
- 已通过方法数量、装饰器保留、乱码扫描和 `git diff --check` 检查

## \[1.9.8] - 2026-05-22

### Bug 修复

- **表情包识别精度优化**：`_is_sticker_image` 中 `face` 关键词从简单子串匹配改为路径段匹配（`/face/`、`/face?`、`&face=`、`?face=`），避免误判包含 "surface"、"interface" 等词的普通图片 URL
- **`_web_get_moderation_users`** **并发安全修复**：直接引用 `deque` 改为 `list()` 快照，防止迭代过程中数据被修改
- **`_web_get_logs`** **参数解析修复**：`int()` 转换添加 `ValueError`/`TypeError` 捕获，非法 `limit` 参数不再导致 500 错误
- **`_save_logs`** **节流计时修复**：无事件循环时不再更新 `_last_log_save`，避免跳过写入后节流计时器仍被更新导致日志长时间不持久化
- **`_stats_cache`** **内存泄漏修复**：`group_stats`（上限500）、`user_stats`（上限2000）、`user_names`（上限2000）添加容量上限，超限时自动淘汰计数最低的条目
- **`_invalidate_stats_cache`** **完整性修复**：重置时同步清理 `group_stats`、`user_stats`、`user_names`，避免脏数据残留
- **`_client`** **缓存失效机制**：`_recall_msg`、`_kick_member`、`_mute_member` 调用失败时清除 `_client` 缓存，下次调用时重新获取有效连接
- **LLM 审核 JSON 提取优化**：优先匹配含 `"violation"` 键的 JSON 对象，避免误匹配 LLM 返回中其他花括号内容
- **前端 XSS 防护**：图片 URL 插入 `src` 属性时使用 `escAttr` 转义，防止恶意 URL 注入
- **死代码清理**：移除未使用的 `has_sticker` 变量
- **`_web_today_stats`/`_web_stats`** **并发安全修复**：遍历 `_moderation_logs` 改为 `list()` 快照
- **`设精华`/`取消精华`** **命令修复**：`int(args[1])` 改为 `_safe_int()`，非法消息ID不再导致命令崩溃
- **`_fetch_context_messages`** **修复**：`_get_client()` 无参数调用改为直接使用 `self._client`，避免 TypeError

### 代码优化与维护

- **WebUI 显示所有 OCR 图片**：`_log_moderation` 不再过滤表情包图片 URL，所有被审核的图片（包括表情包/商城表情）均可在 WebUI 查看弹窗中展示
- **版本号统一管理**：新增 `_PLUGIN_VERSION` 常量，`@register` 装饰器和 `_web_stats` API 均引用该常量，避免版本号硬编码多处不一致
- **移除未使用的** **`import struct`**：该导入从未被使用
- **移除未使用的** **`_log_lock`**：`asyncio.Lock()` 创建后从未使用，已清理
- **移除未使用的** **`_check_qq_favorite`** **方法**：该方法从未被调用，与 `_check_qq_favorite_non_forward` 逻辑重复
- **`import csv, io`** **移至文件顶部**：符合 PEP 8 规范，避免方法内延迟导入
- **`list.remove()`** **安全化**：新增 `_safe_list_remove` 辅助方法，所有 `list.remove()` 调用替换为安全版本，避免 `ValueError` 异常
- **`_web_delete_logs`** **ID 转换安全化**：`int()` 转换添加异常捕获，非法 ID 不再导致整个删除操作失败
- **命令方法大规模重构**：提取 `_check_admin_cfg_access`、`_get_group_client`、`_call_group_api` 三个辅助方法，消除 20+ 个命令方法中重复的权限检查+获取客户端+调用API模式，减少约 300 行重复代码
- **第一批 llm\_tool 方法重构**：`ban_group_member`、`unban_group_member`、`kick_group_member`、`set_whole_group_ban`、`set_member_card`、`send_group_announcement`、`get_group_member_list`、`set_group_admin`、`set_group_name`、`set_member_title`、`get_banned_members` 共 11 个方法使用辅助方法重构
- **查询类命令重构**：`字数统计`、`群统计`、`搜索成员`、`公告列表`、`文件列表`、`成员列表`、`禁言列表`、`删文件` 共 8 个命令使用辅助方法重构
- **`_get_group_client`** **返回值解包 Bug 修复**：`_, client, _` 解包后用 `_` 作为错误消息会导致取到错误值，全部改为 `err` 变量名
- **`搜索成员`** **命令添加 ADMIN 权限**：该命令可搜索成员信息，属于敏感操作，添加 `@filter.permission_type(ADMIN)` 装饰器
- **删除死代码方法**：移除从未被调用的 `_get_image_file_from_event`（15行）和 `_check_forward_msg_qq_favorite`（30行），其功能已被其他方法覆盖
- **删除未使用的 import**：移除 `Reply` 和 `Image`（仅在死代码方法中使用）
- **`_should_scan_message`** **移除不可达分支**：移除始终为 True 的 `isinstance` 检查和不可达的 `return True`
- **`int()`** **转换安全化**：`_kick_member`、`_mute_member`、`_search_keyword_in_messages`、`_web_get_moderation_users`、批量撤回等方法中的 `int(group_id)` 改为 `_safe_int()`，防止 ValueError
- **命令方法** **`int()`** **转换安全化**：`禁言`、`解禁`、`踢人`、`设置名片`、`头衔`、`设置管理` 共 6 个命令中 `int(user_id)` 改为 `_safe_int()`，非法 QQ 号不再导致命令崩溃
- **`_is_admin`** **类型转换统一**：`int(group_id)`/`int(user_id)` 改为 `_safe_int()`，与全局风格一致
- **`_fetch_context_messages`** **类型转换统一**：`int(group_id)` 改为 `_safe_int()`
- **`recall_last`** **参数解析安全化**：`int(args[1])` 改为 `_safe_int(args[1], 1)`
- **`_mute_member`** **配置读取安全化**：`int(config.get(...))` 改为 `_safe_int()`
- **`cmd_ban`** **时长参数安全化**：`int(args[2])` 改为 `_safe_int(args[2], 10)`
- **`patterns.py`** **重复正则清理**：移除 41 条完全重复的 AD\_PATTERNS 条目（含 3 条出现 3 次的重复），AD\_PATTERNS 从 558 条精简为 517 条
- **移除未使用的** **`Optional`** **import**：`typing.Optional` 在代码中从未使用
- **`.gitignore`** **修复**：修正 `-/.git/` 和 `-` 错误条目为 `.git/`
- **`_log_moderation`** **简化**：`valid_urls` 过滤逻辑从 5 行循环简化为列表推导式
- **QQ收藏消息识别逻辑修复**：转发消息中的QQ收藏检测不再依赖发送者昵称包含"QQ收藏"字样，改为统一使用 `_is_qq_favorite_text` 和 `_check_dict_seg_qq_favorite` 检测消息内容特征（`sharechain.qq.com`、JSON/app数据中的收藏标识），新增 `json` 类型消息段的收藏检测
- **`sender`/`publisher`** **None安全修复**：5处 `.get('sender', {})` 和 `.get('publisher', {})` 改为 `.get('sender') or {}`，防止 API 返回 `null` 值时 AttributeError 崩溃
- **禁言列表解析统一**：`cmd_banned_list` 的 `get_group_shut_list` 结果解析与 LLM 工具统一，支持 dict 返回值
- **公告列表解析统一**：两处 `_get_group_notice` 结果解析统一，同时兼容 `data` 和 `notices` 字段
- **未使用变量清理**：4处 `group_id, client, gid, err =` 中未使用的 `group_id` 改为 `_`
- **静默异常添加日志**：`_load_config_schema`、`_fetch_context_messages`、批量撤回单条失败等处添加 debug/warning 日志
- **提取公共方法** **`_get_admin_list`**：3处重复的管理员列表清洗逻辑合并
- **提取公共方法** **`_extract_list_result`**：9处重复的 API 结果解析逻辑合并

### 文件清理

- 删除根目录 `index.html`（旧版 WebUI 残留）
- 删除 `ui_templates.md`（UI 模板参考文件）

## \[1.9.7] - 2026-05-21

### WebUI 全面重构

- **Linear/Vercel 简约风格**：CSS 全面替换为极简风格（细边框/大量留白/精致 Toggle/下划线链接按钮）
- **暗色模式**：CSS 变量体系（`:root` 亮色 + `[data-theme="dark"]` 暗色），侧边栏底部切换按钮，`ls` 包装器兼容 iframe 沙箱 localStorage 禁用
- **移动端优化**：新增底部导航栏（600px 以下替代隐藏的侧边栏），40+ 条移动端 CSS 适配规则，表格横向滚动，输入框/按钮全宽布局
- **消息展开**：审核日志和被撤回用户记录的长消息支持点击展开/收起完整内容
- **免责声明卡片重构**：Linear 风格（6px 圆点状态/下划线链接/36x20px Toggle/底部 hint）
- **免责声明横幅同步**：勾选同意后总览页横幅实时隐藏

### Bug 修复

- **`bridge.ready()`** **挂起修复**：先绑定 `apiGet`/`apiPost` 再调 `ready()`，加 5 秒超时（`Promise.race`），超时后仍正常初始化
- **`localStorage`** **沙箱禁用修复**：新增 `ls` 包装器，iframe 沙箱环境自动降级为内存存储
- **免责声明链接跳转修复**：`<a target="_blank">` 改为 `window.top.location.href` → `window.open` → `window.location.href` 三级降级
- **`renderListTags`** **硬编码颜色修复**：`color:#909399` 改为 `color:var(--text-muted)` 适配暗色模式
- **消息展开查询范围修复**：`document.querySelectorAll` 改为容器内 `c.querySelectorAll`
- **展开消息宽度限制修复**：展开时移除 `max-width` 限制，`lin-msg-full` 最大宽度从 380px 增至 500px

## \[1.9.6] - 2026-05-20

### 免责声明机制

- **新增免责声明同意机制**：使用插件前必须阅读并同意免责声明，未同意时所有功能（审核、群管、LLM工具等）不可用
- **WebUI 免责声明卡片**：设置页顶部新增免责声明区域，包含阅读链接（跳转 mianze.0n.pub）和同意开关
- **总览面板警告横幅**：免责声明未同意时，总览页顶部显示橙色警告横幅，点击可跳转至设置页
- **`_cfg_check`** **拦截**：LLM 工具调用时检查免责声明状态，未同意返回提示信息
- **`_handle_message`** **拦截**：自动审核流程检查免责声明状态，未同意时静默跳过
- **配置项** **`disclaimer_agreed`**：新增布尔配置项，默认 `false`，同意后自动持久化

### 安全修复

- **`_write_logs_sync`** **UnboundLocalError 修复**：`tempfile.mkstemp` 抛异常时 `tmp_path` 未赋值，`except` 块中 `os.path.exists(tmp_path)` 触发 `UnboundLocalError` 掩盖原始异常；现初始化 `tmp_path = None` 并在清理前检查
- **`_web_get_config`** **敏感信息泄露修复**：从黑名单过滤改为白名单机制，仅返回 `_conf_schema.json` 中定义的配置项，防止未知命名的敏感字段（如 ak/sk/auth 等）被API暴露
- **`quart_request`** **为None时WebAPI防护**：添加 `_check_quart_available` + `_wrap_web_handler` 包装器，所有Web API在Quart不可用时返回明确错误而非 `AttributeError`
- **`_save_logs`** **同步写入退化修复**：`RuntimeError` 时不再退化执行同步文件写入，改为跳过并警告，避免阻塞事件循环

### 代码质量

- **import顺序修正（PEP 8）**：`import tempfile` 从方法内部移至文件顶部；`_PLUGIN_NAME` 移至所有import之后
- **`_check_qq_favorite`** **深层嵌套重构**：提取 `_is_qq_favorite_text`、`_check_dict_seg_qq_favorite`、`_check_forward_msg_qq_favorite` 三个辅助方法，将6-8层嵌套降为2-3层
- **`_register_web_apis`** **循环注册**：20个重复的 `register_web_api` 调用改为数据驱动的循环注册，代码量减少90行
- **`_admin_role_cache`** **内存泄漏修复**：缓存超过1000条时自动清理过期条目，防止长时间运行后字典无限膨胀

### 正则优化

- **patterns.py 冗余正则清理**：修复335处 `(?:X|X)` 无意义重复模式（如 `(?:人|人)` → `人`），AD\_PATTERNS从571条精简为558条，减少正则引擎不必要的分支回溯

***

## \[1.9.5] - 2026-05-20

### 修复

- **`_web_stats`** **NameError 修复**：`len(logs)` 引用未定义变量导致 Dashboard 统计接口 500 错误，改为 `len(self._moderation_logs)`
- **`recall_all`** **逻辑缺陷修复**：docstring 声明 `[条数]` 但代码将参数当 `user_id` 使用。重写参数解析，支持 `/批量撤回 [条数]`、`/批量撤回 @用户 [条数]`、`/批量撤回 用户QQ号 [条数]` 三种用法，默认撤回20条
- **`_web_get_config`** **方法丢失修复**：上版本重构时误删方法声明，导致获取配置接口异常

### 优化

- **`_web_update_config`** **硬编码重构**：将 40+ 个硬编码字段名列表改为从 `_conf_schema.json` 自动推断类型，新增配置项无需手动维护字段列表
- **`terminate`** **异步化**：`_write_logs_sync` 改用 `await asyncio.to_thread()` 剥离到线程池，避免关机时事件循环阻塞
- **`except Exception: pass`** **静默异常修复**：8处关键位置改为 `logger.debug`，保留调试信息不再吞异常
- **`_try_get_sender_id`** **精简**：5段重复 try/except 精简为 lambda 列表循环，代码量减半

### 文档

- **新增「为什么内置这么多正则？」章节**：解释571条广告正则的必要性、性能保障措施、误判处理机制
- **路线图更新**：SQLite 详细化（多维度索引查询、自动归档清理、分页搜索排序、全自动迁移）；新增「数据统计仪表盘」和「Aho-Corasick 自动机匹配」规划项

***

## \[1.9.4] - 2026-05-20

### 重大更新

#### GIF动图审核增强

- **新增GIF动图识别**：自动检测图片URL是否为GIF格式（`.gif`后缀或URL中包含`.gif`）
- **GIF专用OCR提示词**：GIF图片送审时自动追加多帧内容识别提示，引导视觉模型关注每一帧的违规内容
- **GIF审核结果标注**：OCR识别结果前缀标注 `[GIF动图]`，方便区分普通图片和动图

#### 表情包/商城表情审核

- **新增** **`market_face`（商城表情）消息段识别**：之前商城表情完全不被审核，现在会提取图片URL送OCR识别
- **表情包专用OCR提示词**：商城表情送审时自动追加表情包文字转录和违规判断提示
- **表情包审核结果标注**：OCR识别结果前缀标注 `[表情包]`
- **新增** **`scan_sticker_enabled`** **配置开关**：可独立控制是否审核表情包/商城表情（默认开启）
- **`_should_scan_message`** **新增** **`market_face`** **类型**：商城表情消息现在会触发审核流程
- **`_format_message_content`** **新增** **`market_face`** **格式化**：上下文消息中商城表情显示为 `[商城表情]`

### 性能优化

#### 并发安全与高并发场景

- **LLM审核并发限流**：新增 `asyncio.Semaphore(5)`，限制最多5个并发LLM调用，防止上百群同时触发审核导致API限流/内存爆炸
- **日志文件原子写入**：`_write_logs_sync` 改用 `tempfile.mkstemp` + `os.replace` 原子写入，防止并发写入导致数据损坏
- **`asyncio.get_running_loop()`** **替换弃用API**：`asyncio.get_event_loop()` 在Python 3.10+已弃用，改用 `get_running_loop()` + `RuntimeError` 回退
- **管理员角色缓存**：`_is_admin` 不再每次消息都调用 `get_group_member_info` API，添加5分钟TTL内存缓存，上百群场景下API调用量减少99%+
- **管理员缓存自动清理**：WebUI修改 `admin_list` 时自动清除 `_admin_role_cache`，确保权限变更立即生效

#### 数据结构与算法

- **日志管理改用** **`deque(maxlen=500)`**：自动淘汰旧数据，无需手动截断和重建ID，消除O(n)拷贝
- **白名单/黑名单改用** **`set`** **查找**：`_group_white_set`/`_group_black_set`/`_user_black_set`，查找从O(n)降为O(1)
- **LLM错误去重改用** **`set`**：`_call_llm_safe` 中错误去重从 `any(err in list)` O(n²) 优化为 `set` O(1)
- **统计数据增量缓存**：`_stats_cache` 字典在 `_log_moderation` 中增量更新，WebUI统计API从全量遍历降为O(1)读取
- **合并转发消息一次解析**：新增 `_resolve_forward_messages` 方法，一次API调用同时提取文本和QQ收藏检测结果，消除重复 `get_forward_msg` 调用

#### IO与资源控制

- **异步日志写入**：`_save_logs` 使用 `run_in_executor` 在线程池中执行文件写入，不再阻塞事件循环
- **词库命中日志降级**：`_check_lexicon` 命中日志从 `logger.info` 降为 `logger.debug`，减少高频场景日志IO
- **上下文消息长度限制**：每条上下文截断200字，总上下文截断3000字，防止提示词过长导致LLM调用失败
- **`group_id`** **参数传递优化**：`_call_llm_for_moderation` 新增 `group_id` 参数，避免重复调用 `_get_group_id`

### 修复

#### AstrBot加载问题（关键）

- **添加** **`@register`** **装饰器**：AstrBot要求插件类必须使用 `@register` 注册，之前缺失导致部分场景无法加载
- **添加** **`terminate`** **方法**：插件卸载/重载时同步写入未保存的日志，防止防抖期间的数据丢失

#### 逻辑缺陷

- **`scan_forward_msg`** **开关修复**：关闭后转发消息文本不再合并到审核内容，但QQ收藏检测仍正常工作
- **词库加载路径优化**：优先从 `StarTools.get_data_dir()` 读取自定义词库，防止插件更新时词库被覆盖
- **`_web_update_config`** **白名单/黑名单同步set**：WebUI修改名单时同步更新 `_group_white_set`/`_group_black_set`/`_user_black_set`

***

## \[1.9.3] - 2026-05-20

### 重大更新

#### 合并转发消息审核

- **新增合并转发消息解析**：之前合并转发消息完全不被审核，现在会通过 `get_forward_msg` API 获取转发消息内容
- 新增 `_extract_forward_text` 方法：解析转发消息中的文本、图片、嵌套转发等内容
- 新增 `_should_scan_message` 对 `forward` 类型消息段的检测
- 新增 `scan_forward_msg` 配置开关（默认开启）
- 转发消息内容格式化为 `[转发]昵称: 内容`，与原始文本合并后一起送入审核

#### OCR 识图审核

- **新增 LLM 视觉模型识图功能**：使用支持图片理解的 LLM 模型识别图片内容进行审核
- 新增 `_ocr_images` 方法：批量 OCR 识别（最多3张图片）
- 新增 `_call_llm_ocr` 方法：调用 LLM 视觉模型，支持三级降级策略
- 新增 `ocr_enabled` 配置开关（默认关闭，需配置视觉LLM）
- 新增 `ocr_provider_id` 配置：**必须手动选择**视觉 LLM Provider，未配置则 OCR 功能不生效
- **内置3种OCR提示词模板**：
  - `default`（通用识别）：识别图片内容并转录文字，标注广告/违规内容
  - `strict`（严格审核）：重点检查广告、色情、政治、暴恐、赌博等违规内容
  - `text_only`（纯文字转录）：仅转录图片中的文字，不做分析
- 新增 `ocr_prompt_template` 配置：选择内置提示词模板
- 新增 `ocr_custom_system_prompt` / `ocr_custom_user_prompt` 配置：自定义OCR提示词（覆盖模板）
- OCR 识别结果以 `[OCR识图内容]` 前缀追加到审核文本

#### QQ收藏自动撤回

- **新增QQ收藏内容自动撤回**：检测到QQ收藏转发的消息自动撤回
- 新增 `_check_qq_favorite` 方法：多维度检测QQ收藏转发内容
- 新增 `recall_qq_favorite_enabled` 配置开关（默认开启）
- 识别逻辑：检查转发消息中发送者的 `nickname`/`card` 是否包含"QQ收藏"、检查 `app` 类型消息段内容、检查文本内容

#### WebUI 管理面板更新

- **新增 OCR 识图设置卡片**：在设置页面新增独立的 OCR 识图设置区域
- **新增 OCR Provider 下拉选择器**：从后端获取可用 Provider 列表，下拉选择视觉模型
- **新增审核 LLM Provider 下拉选择器**：高级设置中审核 LLM 也改为下拉选择
- **新增 OCR 提示词模板下拉选择**：通用识别/严格审核/纯文字转录三种模板
- **新增自定义 OCR 提示词输入框**：系统提示词和用户提示词独立编辑
- **审核设置新增开关**：合并转发审核开关、QQ收藏撤回开关
- **新增** **`/providers`** **API**：返回所有可用 LLM Provider 列表供前端选择

#### 命令简介

- **为全部25个命令添加 docstring 简介**：AstrBot 会自动解析 docstring 展示命令帮助
- 用户在聊天中查看命令列表时，现在能看到每个命令的用途和用法说明

### 修复

#### Bug修复

- **修复** **`_call_llm_ocr`** **中** **`get_current_chat_provider_id('')`** **空字符串调用问题**：移除不可靠的空字符串调用，改为直接使用配置的 provider\_id 或遍历所有可用 provider
- **修复QQ收藏检测逻辑过于宽泛**：之前使用 `'收藏' in forward_text` 关键词匹配，会误匹配任何包含"收藏"的转发消息；改为检查发送者昵称是否为"QQ收藏"，大幅降低误判率
- **修复** **`_format_message_content`** **缺少** **`forward`** **类型格式化**：合并转发消息现在显示为 `[合并转发消息]`

***

### 修复

#### 英文指令替换为中文

- **全部英文指令改为中文**：`/word_count` → `/字数统计`、`/group_stats` → `/群统计`、`/search_member` → `/搜索成员`、`/recall_last` → `/撤回最新消息`、`/recall_all` → `/批量撤回`
- **指令提示文本中文化**：所有指令用法提示中的参数类型也改为中文（如 `swear` → `脏话`）

#### AstrBot 管理员自动同步

- **AstrBot 管理员自动写入插件 admin\_list**：插件启动时从 `context.astrbot_config` 读取 `admin_id`，自动合并到插件 `admin_list` 并持久化
- **`_is_admin`** **同时检查 AstrBot 管理员**：即使插件配置中未填写，AstrBot 设置的管理员也能使用管理功能
- 日志输出同步的管理员列表，方便排查

#### 管理员消息被误审核

- **修复管理员消息也会被智能审核系统撤回的严重Bug**：`_handle_message` 方法中缺少管理员权限检查
- 在审核逻辑开头（配置检查之后）添加 `if await self._is_admin(event): return`
- 现在管理员消息完全跳过审核流程

***

## \[1.9.1] - 2026-05-08

### 修复

#### 管理员消息被误审核

- **修复管理员消息也会被智能审核系统撤回的严重Bug**：`_handle_message` 方法中缺少管理员权限检查
- 在审核逻辑开头（配置检查之后）添加 `if await self._is_admin(event): return`
- 现在管理员消息完全跳过审核流程

#### README 指令文档完善

- **新增完整指令介绍**：分为管理操作指令（需管理员权限）、查询统计指令（所有人可用）、AI LLM 工具三个分类
- **指令全中文**：所有指令名称和参数描述使用中文
- 补充遗漏指令：字数统计、群统计、搜索成员、撤回最新消息、成员列表、禁言列表、公告列表、文件列表

***

## \[1.9.0] - 2026-05-08

### 重大修复

#### LLM 工具调用完全重写

- **修复所有 20 个 LLM 工具无法被 AI 调用的致命 Bug**
- `return dict` → `yield event.plain_result()`：AstrBot 的 `@filter.llm_tool` 要求生成器模式，之前用 `return` 导致工具调用无响应
- `"""` → `'''`：AstrBot 解析 docstring 时要求使用三单引号
- 参数类型 `int` → `number`：AstrBot 只支持 `string/number/object/boolean/array`，不支持 `int`
- 参数类型 `bool` → `boolean`：同上

#### API 返回值检查

- **新增** **`_check_api_result()`** **辅助函数**：检查 OneBot API 返回的 `status`/`retcode`
- **所有 27 处 API 调用添加返回值检查**：之前调用踢人/禁言等 API 后直接返回成功，实际可能失败
- 失败时返回具体错误信息（错误码 + 错误消息）

#### 管理命令权限修复

- **所有 17 个管理命令添加** **`@filter.permission_type(filter.PermissionType.ADMIN)`** **装饰器**
- 之前普通成员也能调用管理命令（仅靠内部 `_is_admin` 检查，命令已被触发）
- 现在由 AstrBot 框架层在调用前拦截非管理员用户
- 移除命令函数内部冗余的 `_is_admin` 检查

#### 头像显示修复

- **群头像**：改用腾讯官方 `https://p.qlogo.cn/gh/{gid}/{gid}/`
- **成员头像**：改用 `https://q.qlogo.cn/headimg_dl?dst_uin={uid}&spec=640`（640px 高清）
- **前端绕过 AstrBot URL 拦截**：使用 `data-*` 属性 + JS 动态设置 `src`，避免 AstrBot 页面系统将外部 URL 转换为代理 URL
- **URL 字符串拆分**：将 `https:` 和 `//域名` 分开拼接，绕过 AstrBot 对 JS 中 URL 的自动替换

### 变更

- 最低 AstrBot 版本从 `>=4.16.0` 提升到 `>=4.24.2`（WebUI 插件页面支持所需）

***

## \[1.8.2] - 2026-05-08

### 修复

#### 配置持久化

- **所有 WebUI 修改的配置不再丢失**：之前修改白名单/黑名单/管理员/开关等仅修改内存，重启后丢失
- 新增 `_save_config_safe()` 方法，所有配置修改后自动调用 `self.config.save_config()` 持久化到磁盘
- 涉及：白名单增删、黑名单增删、用户黑名单增删、管理员增删、设置保存（8处）

#### 日志持久化

- **审核日志持久化到本地文件**：`moderation_logs.json`，重启后不丢失
- 新增 `_load_logs()` 启动时从文件加载日志
- 新增 `_save_logs()` 每次日志变更（新增/删除/清空）后自动保存
- 移除所有 `getattr(self, "_moderation_logs", [])` 防御式访问，改为 `__init__` 中初始化

#### 头像修复

- **成员头像**：修正为 HTTPS 协议 `https://q1.qlogo.cn/g?b=qq&nk={uid}&s=100`
- **群头像**：使用外部 API `https://api.mmp.cc/api/qqgroup?text={gid}`
- **CSP 策略**：添加 `<meta http-equiv="Content-Security-Policy">` 允许加载外部图片

***

## \[1.8.1] - 2026-05-08

### 修复

#### WebUI 数据不显示

- **Bridge API 返回格式不匹配**：Bridge SDK 自动解包 `{"status":"success","data":...}` 返回 `data` 部分，但前端检查 `res.status === 'success'` 永远不成立
- 移除所有 `res.status === 'success'` 检查，改为直接使用返回数据
- 移除所有 `res.data` 引用，改为直接使用 `res`
- 错误处理改为检查 `res.status === 'error'`（仅 safeGet/safePost 捕获异常时返回）

#### 头像不显示

- **群头像**：从 `p.qlogo.cn` 改为外部 API `https://api.mmp.cc/api/qqgroup?text={gid}`
- **成员头像**：修正为 `http://q1.qlogo.cn/g?b=qq&nk={uid}&s=100`（100x100 非高清）

#### 清理

- 移除调试用的 `console.log('[DEBUG]')` 代码

***

## \[1.8.0] - 2026-05-07

### 重大修复

#### 配置与运行时同步（7轮审查共修复24个Bug）

- **`moderation_ban_duration`** **单位不一致**：schema 定义为"分钟"默认10，代码和前端使用"秒"默认1800 → 统一为"秒"默认1800
- **`_web_update_config`** **缺少6个开关**：`group_honor_enabled`、`at_all_remain_enabled`、`ignore_requests_enabled`、`group_msg_history_enabled`、`group_portrait_enabled`、`group_sign_enabled` 未在 `bool_keys` 中，前端保存时静默忽略
- **`ban_notice`** **缺失 schema 定义**：AstrBot 内置面板无法显示此项
- **`_web_update_config`** **`list_keys`** **追踪错误**：`updated.append(key)` 在类型检查外，非 list 值也被标记为已更新
- **`_web_update_config`** **`int_keys`** **追踪错误**：`updated.append(key)` 在 try 外，无效值也被标记为成功
- **`moderation_ban_duration`** **无范围校验**：可传入0或负数 → 添加 min=60/max=2592000 限制

#### 运行时变量同步

- **`loadAdmins()`/`loadSettings()`** **覆盖** **`CONFIG`** **对象**：`CONFIG = res.data` 丢失 `CONFIG._groups` → 改为 `CONFIG = { ...CONFIG, ...res.data }`
- **`_web_update_config`** **修改** **`admin_list`** **后未规范化运行时格式**：添加 admin\_list 和 enabled 的运行时同步

#### 统计与日志

- **`_web_stats`/`_web_today_stats`** **过滤条件错误**：`"拦截"` 从不匹配实际 action 字符串 → 改为 `"撤回" in action`
- **`_web_get_groups`** **`today_count`** **统计包含"放行"**：添加 `"撤回" in action` 过滤
- **`_web_get_moderation_users`** **缺少** **`ts`** **字段**：前端时间显示为空
- **`today_start`** **在循环内重复计算**：移到循环外提升性能

#### 前端

- **批量删除使用** **`/logs`（50条限制）**：改为 `/logs/export`（全量）
- **`.filter(Boolean)`** **过滤掉** **`id=0`**：改为 `.filter(id => id !== undefined && id !== null)`
- **Tab 切换到群管理未加载管理员列表**：添加 `loadAdmins()` 调用
- **搜索触发 API 每次按键**：改为本地 `CURRENT_MEMBERS` 缓存过滤
- **Modal 取消按钮使用 inline onclick**：改为 `addEventListener`
- **`if not member_count`** **对** **`member_count=0`** **误触发 API**：改为 `if member_count is None`
- **`admin_list`** **列表推导在每个成员循环内重复**：预计算为 `admin_set` 集合

#### 前端设置面板

- **缺少禁言时长/Provider ID/通知模板输入框**：添加 `renderExtraSettings()` 函数
- **6个功能开关未在设置页面显示**：补充到 `FEATURE_TOGGLES` 数组

***

## \[1.7.0] - 2026-05-07

### 重大更新

#### WebUI Dashboard 全面升级

- **4标签页架构**：总览、群管理、违规记录、设置
- **总览面板**：8项统计卡片（今日拦截/总审核/放行/白名单数/黑名单数/用户黑名单数/管理员数/总日志）+ 今日拦截排行榜（群排名+用户排名）
- **群管理面板**：
  - 群卡片网格，显示群头像/群名/群号/成员数/状态标签
  - 白名单群显示今日拦截数
  - 点击群卡片查看群成员列表（群主/管理员/成员分色显示，头衔颜色与QQ官方一致）
  - 一键加入/移出白名单/黑名单
  - 从群成员中直接设置/移除插件管理员
  - 群列表和成员搜索（本地过滤，无API调用）
- **违规记录面板**：
  - 被撤回用户聚合视图（同一用户多次违规合并显示，可展开详情）
  - 勾选/全选、批量删除、CSV导出
  - 完整审核日志表格（时间/群号/用户/消息/操作/原因）
- **设置面板**：
  - 核心开关（插件总开关/自动审核/审核通知）
  - 审核开关（脏话检测/广告检测/AI审核/AI禁言/防注入）
  - 词库开关（8个分类独立控制）
  - 功能开关（23项群管功能独立控制）
  - 高级设置（禁言时长/LLM Provider ID/通知模板）
  - 名单管理（群白名单/群黑名单/用户黑名单的增删）

#### 新增 19 个 Web API

- `/stats`、`/config`（GET/POST）、`/lexicon`、`/logs`
- `/moderation_users`、`/logs/delete`、`/logs/export`
- `/groups`、`/group_members`
- `/whitelist/add`、`/whitelist/remove`
- `/blacklist/add`、`/blacklist/remove`
- `/user_blacklist/add`、`/user_blacklist/remove`
- `/admin/add`、`/admin/remove`
- `/today_stats`

***

## \[1.6.0] - 2026-05-07

### 新增

#### WebUI 基础版

- 注册基础 Web API：统计、配置读写、词库、日志
- 创建 `pages/dashboard/` 插件页面
- 审核日志支持 `id`、`ts`、`msg_text` 字段

#### 被撤回用户查看

- 用户聚合视图：同一用户多次违规合并，可展开查看每条记录
- 显示用户名、QQ号、消息内容、违规原因、时间
- 支持单条删除和批量删除
- 支持 CSV 导出

***

## \[1.5.0] - 2026-05-07

### 新增

#### LLM 工具权限系统

- 28项群管工具添加独立开关配置
- 每个工具调用前检查对应 `_enabled` 配置
- 配置项：`ban_enabled`、`unban_enabled`、`kick_enabled`、`whole_ban_enabled`、`set_card_enabled`、`send_announcement_enabled`、`delete_announcement_enabled`、`list_announcements_enabled`、`member_list_enabled`、`set_admin_enabled`、`set_group_name_enabled`、`set_title_enabled`、`banned_list_enabled`、`join_verify_enabled`、`recall_enabled`、`essence_enabled`、`group_files_enabled`

#### 防 Prompt 注入

- 新增 `prompt_injection_enabled` 配置开关
- 关闭后 LLM 不会收到群管工具的使用说明和权限规则

***

## \[1.4.2] - 2026-05-06

### 修复

#### 辱骂检测收紧

- **侮辱性脏话（傻逼、废物、脑残、操你妈等）对任何对象使用都应撤回，包括对机器人**
- 解决 "傻逼机器人"、"废物管理" 等消息不被撤回的问题
- 核心准则从"从宽原则"改为"侮辱性脏话一律撤回"
- 轻微口头禅（卧槽、我靠、牛逼）、自嘲（我太菜了）、游戏调侃（垃圾队友）仍不违规

#### LLM 提示词优化

- system\_prompt 更新：明确要求严格处理侮辱性脏话
- 判断流程更新：去掉"从宽原则"，改为"侮辱性脏话一律撤回"
- is\_sensitive 描述更新：去掉"从宽判断"

***

## \[1.4.1] - 2026-05-06

### 优化

#### LLM 提示词：新增政治敏感判断标准

- 新增第5条审核标准：政治敏感类（political）
- 明确列出**违规**情况：颠覆政权、侮辱领导人、分裂国家、邪教传播
- 明确列出**不违规**情况：技术讨论、游戏讨论、新闻讨论、历史讨论、医学讨论、日常用语、歌词诗句、英文缩写
- 解决 political 词库误报率高导致 LLM 误判的问题

#### 白名单扩充

- 新增 `乱伦`、`爱滋`、`爱滋病`、`艾滋`、`草` 到 `_POLITICAL_WHITELIST`
- 这些词属于色情/日常词汇，不应触发政治敏感审核

#### 审核标准编号修正

- 修复审核标准编号重复（两个"5."）→ 正确编号为 1-7

***

## \[1.4.0] - 2026-05-06

### 重大变更

#### 审核策略：political 不再强制拦截

- **`political`（政治敏感）从强制拦截改为走 LLM 二次判断**
- `political` 词库存在严重的数据质量问题：混入了大量游戏/技术/医学/色情/日常词汇（约200+个误分类词）
- 强制拦截会导致 "服务器"、"管理"、"系统"、"子宫"、"爷爷" 等日常用语被误杀
- 改为 LLM 判断后，能结合上下文语境区分真正的政治敏感和日常用语

#### 简化白名单

- `_POLITICAL_WHITELIST` 大幅简化：从 45+ 个词减少到 25 个
- 只保留真正不该出现在词库里的日常词汇（服务器、管理、医学术语等）
- 其他误报词由 LLM 上下文判断处理，不再需要白名单

### 当前强制拦截策略

- **强制拦截（跳过LLM）**：`reactionary`（反动言论）、`illegal_url`（违规网址）
- **走LLM判断**：`political`（政治敏感）、`swear`（辱骂）、`ad`（广告）、`porn`（色情）、`violent_terror`（暴恐）、`weapons`（涉枪涉爆）等

***

## \[1.3.9] - 2026-05-06

### 修复

#### Political 词库医学/色情词汇白名单

- **扩展** **`_POLITICAL_WHITELIST`** **白名单**：新增医学/色情/日常词汇
- 解决 "子宫"、"睾丸"、"阴毛"、"梅毒"、"艾滋" 等医学术语被误判为政治敏感
- 解决 "做爱"、"性交"、"性爱"、"开房" 等日常用语被误判
- 解决 "爷爷"、"小便"、"大便"、"排泄" 等日常词汇被误判
- 解决 "根正苗红" 等中性词汇被误判

#### 词库数据质量问题

- `political` 词库中混入了大量色情内容关键词（约200+个）
- 这些色情词会被 `porn` 分类的正则匹配到，不影响色情检测
- 白名单过滤掉了其中的医学/日常词汇，避免强制拦截误杀

***

## \[1.3.8] - 2026-05-06

### 修复

#### Political 词库误报白名单

- **新增** **`_POLITICAL_WHITELIST`** **白名单**：过滤 `political` 词库中明显误分类的关键词
- 解决 "服务器"、"管理"、"系统"、"官方"、"维护"、"客服"、"运营"、"测试" 等日常词汇被误判为政治敏感
- 解决游戏相关词汇误报：GM、私服、外挂、game、master、client、server 等
- 解决技术词汇误报：admin、administrator、system、test、cs 等
- 解决注音符号误报：ㄅ、ㄆ、ㄇ、ㄈ、ㄉ、ㄊ、ㄋ
- 解决符号误报：`&`

***

## \[1.3.7] - 2026-05-06

### 修复

#### 全面词库短词误报修复

- **所有词库分类最低关键词长度统一提高到 3 字符**（仅 `illegal_url` 保持 2 字符）
- 解决 `other` 分类中大量短词误报：`64`、`BJ`、`SM`、`AV`、`die`、`ma`、`sex`、`freedom`、`CCTV`、`www`、`铃声`、`子宫`、`限量`、`兼职` 等
- 解决 `livelihood` 分类中常见词误报：`打人`、`拆迁`、`纠纷`、`盗窃`、`打针`、`崩盘`、`救市` 等
- 解决 `ad` 分类中短词误报：`小姐`、`BT`、`LY` 等
- 解决 `political`/`tencent_ban` 分类中 `他妈`、`下台`、`边防`、`香港`、`民主` 等短词误报

#### 词库命中日志增强

- 新增词库命中详细日志，显示具体命中了哪个关键词
- 格式：`[GroupMgr] 词库命中 [分类]: 关键词='xxx'`
- 方便排查误报和词库问题

***

## \[1.3.6] - 2026-05-06

### 修复

#### 词库误报修复

- **提高** **`political`** **和** **`tencent_ban`** **分类最低关键词长度**：从 2 字符提高到 3 字符
- 解决 "他妈的服务器是土豆吗" 等日常用语被误判为政治敏感的问题
- 词库中 "他妈" 等 2 字符脏话词被错误归类到政治敏感分类，现在自动过滤

#### 词库命中日志增强

- 新增词库命中详细日志，显示具体命中了哪个关键词
- 格式：`[GroupMgr] 词库命中 [分类]: 关键词='xxx'`
- 方便排查误报和词库问题

#### 通用关键词长度过滤

- 所有词库分类的关键词最低长度统一为 2 字符
- `political` 和 `tencent_ban` 分类特殊处理，最低 3 字符

***

## \[1.3.5] - 2026-05-06

### 重大修复

#### LLM调用签名修复

- 修复 `_call_llm_safe` 中LLM调用签名构造错误：`{system_prompt: prompt}` → `{'system_prompt': system_prompt, 'prompt': prompt}`
- 该bug导致第一次LLM调用必定失败，依赖第二次签名兜底

#### 空值安全全面加固

- 修复 `get_all_providers()` / `get_insts()` 可能返回 None 导致迭代崩溃
- 修复 `.get(key, [])` 无法防御 None 值的问题（6处）
- 修复 `m.get('sender')` / `n.get('msg')` 返回非字典时崩溃
- 修复 `event.message_str` 可能为 None
- 修复 `Image` 组件属性可能不存在
- 修复配置列表初始化未防御非 list 类型

#### 整数转换安全

- 全部 `int()` 转换改为 `_safe_int()` + 友好错误提示（15+ 处）
- 涉及：禁言、踢人、设管理、改群名、设头衔、撤回、精华、公告、文件等所有API调用

#### JSON解析修复

- 修复 `_parse_moderation_response` 嵌套 JSON 正则匹配不完整
- 改用 `find/rfind` 从第一个 `{` 匹配到最后一个 `}`

### 审核策略优化

#### 提示词放宽

- 核心准则从"零容忍从严"改为"合理审核、从宽原则"
- 辱骂类：朋友间互怼、口头禅、游戏情绪表达 → 不违规
- 广告类：保持**零容忍**，任何引流/联系方式/推销一律违规
- 色情类：暧昧玩笑、恋爱话题 → 不违规
- 判断流程：不确定时 → 不撤回

#### 强制拦截类型报告修复

- 修复 `hit_labels[0]` 可能不是实际触发拦截的类型
- 改用 `next(c for c in hit_labels if c in force_block_cats)` 获取准确类型

### 其他修复

- `on_llm_request` 统一使用 `_get_group_id` / `_try_get_sender_id` 降级获取
- `shut_up_timestamp` / `file_size` 类型安全
- `get_group_shut_list` 返回值 None 检查
- `pm.get_insts()` 返回值 None 检查

***

## \[1.3.4] - 2026-05-04

### 优化

#### 审核策略调整

- **强制拦截类别缩减**：仅保留 `political`(政治敏感)、`reactionary`(反动言论)、`illegal_url`(违规网址)
- **色情/涉枪涉爆/暴恐/贪腐等改为LLM审核**：命中后交由LLM结合上下文判断
- **严重辱骂保持强制拦截**：涉及家人死亡诅咒、极端侮辱人格的关键词仍直接拦截
- 减少误判，提升对调侃、玩笑等场景的容错率

#### Bug修复

- **修复广告检测被词库覆盖的bug**：外置词库未命中时会覆盖内置正则的匹配结果，导致广告内容漏检

***

## \[1.3.3] - 2026-05-04

### 修复

#### 类型转换安全

- 修复多处 `int()` 转换缺少异常保护的问题
- `_is_admin`：群号和用户ID转换失败时记录警告并返回False
- `_fetch_context_messages`：群号转换失败时返回空列表
- `delete_msg`：消息ID转换失败时记录警告并返回
- `on_llm_request`：群号和用户ID转换添加 `(ValueError, TypeError)` 捕获

#### 空值处理

- `_format_message_content`：添加 `raw_message is None` 处理
- `seg.get('data', {})` 可能返回None，改为 `seg.get('data', {}) or {}`

#### 内存优化

- `_call_llm_safe`：限制错误日志长度（120字符），添加去重逻辑
- 最终异常信息只显示前5条错误，防止无限累积

#### 配置补全

- `_conf_schema.json` 补全 `user_black_list` 配置项定义

#### 通知格式

- 撤回通知：当 `ai_reason` 为空时，移除末尾多余的" - "横杠

#### 命令处理

- `/` 开头消息处理：使用集合替代列表提升查找效率
- 优化边界情况处理，避免重复代码

***

## \[1.3.2] - 2026-05-04

### 修复

#### 词库+号关键词拆分

- 修复词库中 `+` 号连接的关键词无法单独检测的问题
- 例如 `美国疾控中心+冠状病毒` 现在会拆分成 `美国疾控中心` 和 `冠状病毒` 两个独立关键词
- 单独输入 `冠状病毒` 或 `李文亮` 等词现在可以正确检测
- 拆分后只保留2个字符以上的词，避免误报

***

## \[1.3.1] - 2026-05-04

### 修复

#### 敏感词库强制拦截

- 修改审核逻辑，**只要命中任何敏感词库分类即强制拦截**
- 新增强制拦截分类：`porn`(色情)、`violent_terror`(暴恐)、`weapons`(涉枪涉爆)、`corruption`(贪腐)、`supplement`(补充)、`livelihood`(民生)、`tencent_ban`(腾讯封禁)
- 现在 12 个敏感分类全部采用强制拦截，无需LLM二次判断
- 提升审核效率，避免因LLM判断失误导致漏放违规内容

***

## \[1.3.0] - 2026-05-04

### 重大更新

#### 外置词库全面扩充

- 整合 `Sensitive-lexicon-1.2/Vocabulary` 目录下所有词库文件
- 词库版本升级至 1.3，总计 **66,993** 个关键词
- 12个分类全面覆盖：
  | 分类              | 关键词数   | 说明   |
  | --------------- | ------ | ---- |
  | ad              | 108    | 广告推广 |
  | porn            | 554    | 色情淫秽 |
  | political       | 41,859 | 政治敏感 |
  | reactionary     | 551    | 反动言论 |
  | violent\_terror | 178    | 暴恐内容 |
  | weapons         | 439    | 涉枪涉爆 |
  | corruption      | 240    | 贪腐相关 |
  | illegal\_url    | 14,594 | 违规网址 |
  | livelihood      | 513    | 民生敏感 |
  | supplement      | 7,292  | 补充词库 |
  | other           | 165    | 其他违规 |
  | tencent\_ban    | 500    | 腾讯封禁 |

#### 消息历史上下文感知

- 恢复消息历史获取功能，LLM审核时可感知最近30条上下文消息
- 帮助LLM更准确判断消息意图（如区分玩笑和恶意攻击）
- 上下文信息包含发送者昵称和消息内容

#### LLM工具调用说明

- 本插件使用 `text_chat` 直接调用LLM，不使用工具调用模式
- 支持配置专用审核LLM Provider（`moderation_llm_provider_id`）
- 如果LLM调用失败，会自动降级跳过审核，不会阻断正常聊天

***

## \[1.2.3] - 2026-05-04

### 优化

#### 敏感词增强

- 新增40+广告/引流相关正则表达式：跑路、收徒、带人、带你、跟我、学技术、挂圈、端圈、黑产、灰产、圈钱、割韭菜、吃香喝辣、神秘惊喜等

#### 功能精简

- 删除与群管关系不大的10项功能，保留核心群管功能

***

## \[1.2.2] - 2026-05-04

### 修复

#### 命令消息审核修复

- 修复 `/` 开头消息全部被跳过的问题
- 现在只跳过本插件的管理命令（如 `/禁言`、`/踢人` 等）
- 其他 `/` 开头的消息会继续审核（如 `/shabi` 会被检测）

#### 日志增强

- 增加外置词库命中日志，方便排查词库是否生效
- 增加LLM调用状态日志

***

## \[1.2.1] - 2026-05-04

### 修复

#### 权限判断修复

- 修复 `_is_admin` 只检查配置文件admin\_list的问题
- 现在会同时检查用户是否为群管理员或群主

#### 广告检测增强

- 新增40+广告/引流相关正则表达式

***

## \[1.2.0] - 2026-05-04

### 新增

#### 用户黑名单功能

- 支持配置 `user_black_list` 用户黑名单列表
- 自动拦截黑名单用户的加群申请（包括主动申请和被邀请）

#### 指令中文化

- 所有30个管理员指令从英文改为中文，更直观易用
- 指令参数同步支持中文（如`开启`/`关闭`/`状态`）

***

## \[1.1.0] - 2026-05-04

### 新增

#### 开户类违规检测（300+正则表达式）

- **身份冒用/虚假开户类**：冒用、盗用、伪造、假证、代开、代办、代注册等
- **证件相关**：身份证、户口本、护照、驾驶证等所有证件类型
- **隐私侵犯/人肉搜索类**：查询他人信息、人肉搜索、开盒、曝光隐私等
- **金融诈骗类**：信用卡套现、代还、提额、跑分、洗钱、刷单等
- **虚拟货币类**：DeFi、NFT、ICO、矿机、矿池、算力、节点等
- **赌博博彩类**：棋牌、牛牛、龙虎斗、投注、彩票等
- **非法交易类**：出售/收购微信号、QQ号、支付宝、对公账户等

#### LLM提示词优化

- 核心准则升级为"零容忍从严审核"
- 新增"不要试图理解违规者"原则
- 判断流程增加"任何涉及隐私、人肉、开盒的内容 → 一律违规"

***

## \[1.0.0] - 2026-05-03

### 初始版本

#### 群管功能（28项LLM可调用的工具）

- 禁言/解禁成员、踢出成员、全体禁言
- 设置群名片、群公告（发送/删除/查看）
- 群文件管理（上传/删除/移动/重命名/创建文件夹/删除文件夹）
- 精华消息（设置/取消）、撤回消息
- 群名称修改、专属头衔设置
- 群管理员设置、群打卡
- 群头像设置、加群方式设置
- 群荣誉查询、@全体剩余次数查询
- 被忽略加群请求查看、群历史消息获取

#### 智能审核系统

- 正则初筛 + 获取30条上下文 + LLM二次判断
- 外置JSON词库支持（`lexicon.json`），12类违规内容，5000+关键词
- 智能分级策略：直接拦截 + LLM二次判断
- 管理员消息免检测、群白名单/黑名单
- 每个功能独立开关、可指定审核专用LLM Provider

#### 配置系统

- 插件总开关、群白名单/黑名单
- 管理员列表（支持去空格匹配）
- 自动审核开关、撤回通知开关
- 28个群管功能独立开关
- 8个外置词库类别独立开关
