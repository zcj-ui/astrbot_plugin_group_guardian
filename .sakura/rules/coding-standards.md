# 编码规范与项目约定

## 一、Mixin 继承规范

- 多 Mixin 集中继承于 `Main` 类，继承列表需手动同步维护
- **MRO 规则**：继承列表中**靠左的类优先级更高**（左高右低）
- 引入新 Mixin 时，需强制检查 `Main` 类定义并更新继承列表
- 依赖关系的 Mixin 应放在右侧（如 `CommandsMixin` 依赖 `UtilitiesMixin`，则 `UtilitiesMixin` 在左）

## 二、配置设计规范

- 命名模式：`功能名_enabled` + `功能名_count`（如 `kick_recall_enabled` + `kick_recall_count`）
- 类型明确、提供默认值和取值范围
- 配置项通过 `_conf_schema.json` 声明，与业务代码分离

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
