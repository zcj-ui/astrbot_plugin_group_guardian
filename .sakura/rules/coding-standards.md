# 编码规范与项目约定

## 一、Mixin 继承规范

- 多 Mixin 集中继承于 `Main` 类，继承列表需手动同步维护
- **MRO 规则**：继承列表中**靠左的类优先级更高**（左高右低）
- 引入新 Mixin 时，需强制检查 `Main` 类定义并更新继承列表
- 依赖关系的 Mixin 应放在右侧（如 `CommandsMixin` 依赖 `UtilitiesMixin`，则 `UtilitiesMixin` 在左）
- 维护 Mixin 清单文档，避免遗漏（当前含 ModerationMixin、AntiFloodMixin、CommandsMixin、UtilitiesMixin 等）

## 二、配置设计规范

- 命名模式：`功能名_enabled` + `功能名_count`（如 `kick_recall_enabled` + `kick_recall_count`）
- 类型明确、提供默认值和取值范围
- 配置项通过 `_conf_schema.json` 声明，与业务代码分离
- **配置可见性同步**：新增命令别名或配置项时同步更新 README 命令列表和 `/help` 输出

## 三、模块职责

- 每个核心功能（刷屏、审核、申诉、成员管理）拆分独立模块
- 协议适配层抽象：`onebot.py` 封装 OneBot 调用，其他模块通过该接口操作
- 定时任务统一管理：所有周期性操作集中于 `scheduler.py`
- WebUI 前后端分离：后端仅提供 RESTful API，前端独立部署

## 四、安全规范

- SQL 操作均通过参数化查询（`storage.py`），防止注入
- LLM 调用增加超时和错误回退
- 管理指令在 `commands.py` 中进行权限校验

## 五、系统依赖处理

- 引入外部系统库（如 zbar、tesseract）必须提供降级策略
- 使用 `ImportError` 捕获 + 警告日志兜底
- 在分析中注明多平台安装提示
- `good first issue` 需评估依赖安装复杂度，标注环境要求

## 六、兼容性记录

- 不同 OneBot 实现的行为差异（如 NapCat 拼入引用原文、LLOneBot 差异）须在注释中记录
- 新增审核入口时默认检查是否存在"协议附加内容混入"风险
- 沉淀通用 `strip_protocol_fragments` 函数处理协议拼接字段剥离

## 七、测试要求

- 引用回复混合消息、多层嵌套引用加入测试用例
- 覆盖"纯回复/回复+文字/纯文字"三种情况
- 为每个命令方法添加至少一个简单集成测试，纳入 CI
