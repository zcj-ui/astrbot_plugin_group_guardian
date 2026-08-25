# QQ群智能守护者（GroupGuardian）v2.9.0 使用教程

## —— 智谱 GLM-4V-Flash 视觉模型配置指南（视频广告检测 / OCR 识图审核）

本教程针对使用**智谱 GLM-4V-Flash**（免费视觉多模态模型）的用户，覆盖：

- 插件 zip 安装
- 在 AstrBot 中添加智谱 Provider
- 开启并调优「视频广告检测」「OCR 识图审核」
- 验证与排障

---

## 一、准备工作

### 1. 注册智谱开放平台并创建 API Key

1. 打开智谱开放平台：<https://open.bigmodel.cn>（大模型开放平台）
2. 注册/登录账号
3. 进入「API Keys」页面，点击「创建 API Key」
4. 复制生成的 Key，格式为 **`{ID}.{SECRET}`**（如 `1a2b3c4d.xxxxxxxx`，中间有英文句点）
   - **注意**：Key 只完整显示一次，请立即保存
5. 确认账户中有 **GLM-4V-Flash** 模型可用（该模型免费，可在「模型广场 / 资源包」中查看额度）

> 需要梯子吗？不需要。智谱是国内服务，机器人服务器直连即可。

### 2. 确认 AstrBot 版本

本插件要求 **AstrBot >= 4.24.2**。

---

## 二、安装插件

### 方式 A：zip 手动安装（推荐本次交付）

1. 将 `group_guardian_v2.9.0.zip` 复制到 AstrBot 所在服务器
2. 打开 AstrBot 管理面板 → 「插件商店 / 插件管理」→ 点击 **上传 / 安装本地插件**
3. 选择 zip 文件，等待安装完成
4. 在插件列表中确认「QQ群智能守护者」已启用

> zip 内是 `astrbot_plugin_group_guardian/` 目录（含 `metadata.yaml`），符合 AstrBot 插件规范。

### 方式 B：Git 安装（开发者）

```bash
git clone https://github.com/zcj-ui/astrbot_plugin_group_guardian.git
# 或直接复制本项目目录到 AstrBot 的 plugins 目录
```

### 方式 C：AstrBot 插件商店搜索安装

在插件商店搜索「群守护者 / group_guardian」，安装后更新到 v2.9.0。

---

## 三、在 AstrBot 中添加智谱 GLM-4V-Flash Provider

AstrBot 官方内置了智谱适配器（类型名 `zhipu_chat_completion`，兼容 OpenAI 接口）。

### 操作步骤

1. 打开 AstrBot 管理面板 → **「模型提供商 / Provider」** 页面
2. 点击 **「添加提供商」**
3. **类型（Type）**：选择
   - **「智谱 Chat Completion 提供商适配器」**（zhipu_chat_completion）—— 若你的 AstrBot 版本内置了该类型
   - 或选择 **「OpenAI 兼容」/「OpenAI API」** 类型手动配置
4. 填写配置：

   | 配置项 | 填写内容 |
   | ------ | -------- |
   | **API Key（key）** | 上一步复制的智谱 Key：`{ID}.{SECRET}` |
   | **Base URL** | `https://open.bigmodel.cn/api/paas/v4`（若已预填则保持默认） |
   | **模型名称（model）** | `glm-4v-flash` |
   | **启用（enable）** | 开启 |

5. 点击 **「测试」/「保存」**，看到成功提示即配置完成

> 智谱 Key 以 `{ID}.{SECRET}` 形式整体填入「API Key」即可（智谱兼容 OpenAI 的 Bearer Token 认证）。

---

## 四、配置插件（核心）

### 1. 必须先做的全局配置

打开 AstrBot 管理面板 → 找到「QQ群智能守护者」插件的 **设置**：

| 配置项 | 推荐值 | 说明 |
| ------ | ------ | ---- |
| `disclaimer_agreed` | ✅ 勾选 | 阅读并同意免责声明（必填，否则插件不工作） |
| `enabled` | ✅ 开启 | 插件总开关 |
| `auto_moderate_enabled` | ✅ 开启 | 自动审核总开关 |
| `scan_ad` | ✅ 开启 | 广告正则检测 |
| `admin_list` | 填写你的 QQ 号 | 管理员才能用群管指令（必填） |

### 2. 配置视觉识别（图片 OCR + 视频广告检测共用）

| 配置项 | 推荐值 | 说明 |
| ------ | ------ | ---- |
| `ocr_provider_id` | **智谱 Provider 的 ID**（第 2 步添加时生成的 id） | 指定用 GLM-4V-Flash 做视觉识别 |
| `ocr_enabled` | ✅ 开启 | 开启图片 OCR 识图审核 |
| `ocr_prompt_template` | `default` | 内置提示词模板，可换 `strict`（更严格） |
| `llm_moderation_enabled` | ✅ 开启 | 开启 LLM 二次判断 |
| `qrcode_decode_enabled` | ✅ 开启 | 开启本地二维码解码（用 OpenCV，免费） |

> `ocr_provider_id` 留空时：图片/视频识别会自动回退到 `moderation_llm_provider_id`，再留空则用 AstrBot 默认 Provider。**建议显式指定智谱 provider。**

### 3. 开启「视频广告检测」（本次新增功能）

| 配置项 | 推荐值 | 说明 |
| ------ | ------ | ---- |
| `video_audit_enabled` | ✅ 开启 | **视频广告检测总开关（默认关闭，需手动开启）** |
| `video_max_frames` | `3` | 每个视频最多抽帧数（1-10，越大越准但视觉调用越多） |
| `video_frame_interval_sec` | `5` | 抽帧间隔（秒） |
| `video_max_size_mb` | `30` | 超过此体积的视频不检测（防流量/存储耗尽） |
| `video_download_timeout` | `25` | 下载超时（秒） |
| `video_audit_timeout` | `60` | 单条消息视频审核总超时（秒） |

**开启后效果**：群里有人发视频 → 自动下载 → 抽 3 帧 → 每帧交给 GLM-4V-Flash 识别（OCR 文字 + 二维码解码）→ 若识别出「加群微信 xxx」「扫码进群」等广告内容 → 并入正文初筛 → LLM 复核 → 撤回 / 禁言。

### 4. 按群覆盖（可选）

以上所有配置均支持**按群覆盖**：在插件 WebUI →「多群配置」中为指定群单独设置阈值与开关。

---

## 五、验证是否生效

### 1. 看日志

在 AstrBot 日志中搜索：

- `[GroupMgr] 视频审核` —— 视频审核相关日志
- `[GroupMgr] OCR` —— 图片识别相关日志
- 若出现 `OCR LLM调用失败` / `视频抽帧失败`，参考下方「常见问题」

### 2. 实测

- 发一张含广告文字的图片 → 应被撤回并提示
- 发一个含二维码/广告文字的视频 → 应被撤回并提示
- 正常视频（无广告）→ 应放行（不误杀）

### 3. 在插件 WebUI 看统计

插件自带管理面板：**总览** 页可看到拦截数量、**违规记录** 页可查看被撤回的消息明细。

---

## 六、常见问题（FAQ）

### Q1：视频检测没反应，日志显示「视频抽帧依赖 opencv-python-headless 不可用」

**原因**：服务器上没装 OpenCV。

**解决**：在 AstrBot 的 Python 环境执行：

```bash
pip install opencv-python-headless numpy
```

然后重启 AstrBot。

### Q2：日志显示「OCR LLM调用失败」或视觉识别总是空

**排查步骤**：

1. 确认智谱 Provider 已启用、能通过 AstrBot 的「测试」按钮
2. 确认 `ocr_provider_id` 填的是智谱 Provider 的 **ID**（不是类型名）
3. 确认模型名是 `glm-4v-flash`（不要写成 `glm-4v` 或 `glm-4v-plus`）
4. 确认服务器能访问 `open.bigmodel.cn`
5. 确认智谱账号的 GLM-4V-Flash 额度充足（免费但有每日限额）

### Q3：视频下载失败 / 超过大小限制

- 把 `video_max_size_mb` 调大（如 50），但注意会占用更多流量和存储
- 确认机器人能访问视频 URL（部分平台视频 URL 需要带 Referer 或有时效）

### Q4：GLM-4V-Flash 单次只能传一张图？

是的。glm-4v-flash 上下文内一般只支持**一张图**。本插件已适配：**逐帧单独调用**视觉模型，每次只传一帧，不影响多帧视频审核。

### Q5：视频审核会不会很耗钱？

GLM-4V-Flash **免费**（智谱开放平台提供免费额度），主要成本是服务器流量/CPU。默认每个视频只抽 3 帧、限 30MB，风险可控。

### Q6：误杀正常视频怎么办？

- 关闭 `video_audit_enabled` 可整体关闭
- 调大 `video_frame_interval_sec`（抽帧更稀疏，降低误判概率）
- 把提示词模板换为 `default`（通用识别）而非 `strict`
- 在「多群配置」中仅对可疑群开启

### Q7：不想用智谱了，能用别的视觉模型吗？

可以。`ocr_provider_id` 换成任何支持视觉的 Provider（GPT-4o、Gemini、通义千问 VL、豆包 等），其余不变。

---

## 七、本次 v2.9.0 新增功能速览

| 功能 | 说明 |
| ---- | ---- |
| **视频广告检测** | 自动下载群内视频 → OpenCV 抽帧 → GLM-4V-Flash 逐帧识别 + 二维码解码 → 并入统一审核流程 |
| 视频源解析 | 支持 `convert_to_file_path` / URL / 本地路径 / 协议端 `get_file` API 四级兜底 |
| 安全降级 | 下载失败 / 抽帧失败 / 识别失败均静默放行，不误杀；临时文件用完即删 |
| 6 项新配置 | 均可按群覆盖，WebUI 设置面板自动出现 |

---

## 八、目录结构（zip 内容）

```
astrbot_plugin_group_guardian/
├── metadata.yaml          # 插件元信息（名称/版本 v2.9.0）
├── main.py                # 主入口
├── moderation.py          # 审核主流程（已接入视频审核分支）
├── video_audit.py         # 新增：视频广告检测模块
├── image_audit.py         # 图片 OCR / 二维码审核
├── _conf_schema.json      # 配置定义（含 6 项视频审核配置）
├── lexicon.db             # 内置词库
├── pages/                 # WebUI 管理面板
├── requirements.txt       # 依赖
└── ...（其余群管模块）
```

---

## 九、可选增强（v2.9.1）

### 1. 广告图黑名单（感知哈希，省视觉 API 费用）

| 配置项 | 推荐值 | 说明 |
| ------ | ------ | ---- |
| `ad_hash_blacklist_enabled` | ✅ 开启 | 图片/视频帧识别前先比对历史广告样本（pHash），命中直接标记「已知广告」并跳过视觉 API |
| `ad_hash_distance` | `10` | 相似度阈值（0-64），越小越严格 |
| `ad_hash_auto_learn` | ✅ 开启 | 广告确认违规后自动学习新样本 |

**效果**：某张广告图被 GLM-4V 确认过一次后，下次同图/近似图直接命中黑名单，不再调用视觉 API，免费额度和费用大幅节省。黑名单保存在 `data/plugin_data/astrbot_plugin_group_guardian/hash_blacklist.json`。

### 2. 广告分级处置（降低误伤投诉）

| 配置项 | 推荐值 | 说明 |
| ------ | ------ | ---- |
| `ad_escalation_enabled` | ✅ 开启 | 按窗口内广告违规次数升级处罚 |
| `ad_escalation_warn_at` | `1` | 第 1 次仅撤回+警告 |
| `ad_escalation_ban_at` | `2` | 第 2 次禁言 |
| `ad_escalation_kick_at` | `3` | 第 3 次踢出群聊 |
| `ad_escalation_window_seconds` | `604800` | 统计窗口（7 天），窗口外重置 |

**效果**：广告违规不再一罚到底（撤回+禁言直接踢），改为「警告→禁言→踢出」逐步升级，误伤投诉时更容易解释。次数记录保存在 `data/plugin_data/astrbot_plugin_group_guardian/ad_escalation.json`。

> 两个增强默认都是关闭的，需要手动开启。它们都支持按群覆盖。

---

## 十、视频检测实时性优化（v2.9.2）

群发视频多、或想降低 GLM-4V 调用次数/延迟时，开启以下三项（均可按群覆盖）：

| 配置项 | 推荐值 | 说明 |
| ------ | ------ | ---- |
| `video_frame_mode` | `scene` | 场景切换抽帧：仅在画面明显变化时保留帧，广告信息多在关键画面，减少无效帧 |
| `video_scene_threshold` | `30` | 帧间差异阈值，越大越少抽帧 |
| `video_quick_precheck` | ✅ 开启 | 每帧本地特征预检（饱和度/纹理/文字区域，<50ms），低分帧跳过视觉 API |
| `video_precheck_threshold` | `0.5` | 预检得分低于该值则跳过视觉 API，越高过滤越多 |
| `video_fingerprint_cache` | ✅ 开启 | 广告确认过的整段视频（首帧哈希+时长指纹）下次直接命中，群发同一广告视频零视觉调用 |

**效果**：正常视频约 70% 的帧在预检阶段被过滤，广告视频平均只需 2~3 次 GLM-4V 调用；同一广告视频被多人转发时，从第二次起完全跳过检测（直接命中指纹缓存）。

> 建议先开启 `video_fingerprint_cache` + `video_quick_precheck`（安全且收益大），再按需切 `video_frame_mode=scene`。
