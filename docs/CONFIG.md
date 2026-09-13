# 配置说明（面向用户）

配置入口：**AstrBot WebUI → 插件管理 → QQ群管理 → 管理台**。
配置保存在 AstrBot 插件 KV 中（`settings` / `groups` / `keywords` / `trusted` 等键），审计数据保存在
`data/plugin_data/astrbot_plugin_qq_group_manager/moderation.db`（SQLite）。

## 1. 总览页

| 项 | 说明 |
| --- | --- |
| dry-run | 默认**开启**：所有处置动作只写审计日志，不真正撤回/禁言。确认判定准确后再关闭。 |
| 默认模式 | `lenient`（宽松）为首次安装默认值：违规只警告。可选 `standard` / `strict` / `log_only`。 |
| 平台通道 | 显示是否已拿到 qq_official 的 botpy 客户端；不可用时无法调用任何 QQ 接口。 |
| 能力受限告警 | 近 24 小时的能力受限汇总（如 `err_code=11253` 需要白名单）。 |
| 后台任务 | `flush_state`（30s 落盘配置）、`probe_capabilities`（默认 1800s 重探能力）、`maintenance`（每小时检查/每日裁剪）。 |

## 2. 群管理页

- 群在收到消息后**自动登记**；也可手动填入 `group_openid` 添加。
- 「探测」会调用平台只读接口，刷新能力矩阵（绿=可用、红=受限、黄=无只读探测接口）。
- 「启用审核」会先校验：
  1. 平台通道可用；
  2. 能读取机器人群内状态（否则提示可能未获白名单授权）；
  3. `recv_msg_setting == all`（已开启「接收全部消息」）——未开启则**拒绝启用**并给出操作指引；
  4. 机器人不是群管理员时给出警告（此时违规消息只能警告，无法撤回/禁言）。
- 「停用审核」立即生效；「移除记录」只删除插件侧记录，不影响平台与群成员。

> 逃生舱：配置项 `allow_without_full_msg`（默认 `false`，不建议开启）可跳过第 3 步校验。

## 3. 日志中心

| Tab | 数据来源 | 写入时机 |
| --- | --- | --- |
| 审核事件 | `mod_events` | M2 接入后产生（每条被审核的消息一行） |
| 动作执行 | `mod_actions` | M2 接入后产生（撤回/禁言/警告/上报的结果） |
| API 调用 | `api_calls` | **现在就有**：每次 QQ 接口调用（含 err_code、耗时、重试次数、trace_id） |
| 能力受限 | `capability_log` | **现在就有**：能力探测失败或接口返回权限类错误 |

支持按群、关键字、时间范围筛选，分页浏览，导出 CSV/JSON，以及按类型清空（不可恢复）。

## 4. 工具页

- **能力自检**：一次性探测全部已登记群的能力，输出每项的 ✅/❌/➖、`err_code` 与建议。
- **审计库维护**：按保留策略裁剪、VACUUM 整理、备份下载。
- **实时事件（SSE）**：验证 WebUI ↔ 后端通道；M2 后审核事件会实时出现。
- **指令速查 / 配置摘要**。

## 5. 关键配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 插件总开关 |
| `dry_run` | true | 只记录不处置 |
| `mode` | lenient | 默认处置强度（strict / standard / lenient / log_only） |
| `allow_without_full_msg` | false | 是否允许在未开启「接收全部消息」时启用审核（不建议） |
| `sample_rate` | 1.0 | 送审比例（M2） |
| `llm_min_confidence` | 0.7 | 判定置信度门槛（M2） |
| `mute_steps` | 3→10 分钟 / 4→1 小时 / 5→1 天 | 按严重度禁言时长（M2） |
| `retention_events_days` | 30 | 审核事件/动作保留天数 |
| `retention_api_days` | 7 | API 调用日志保留天数 |
| `retention_capability_days` | 30 | 能力受限日志保留天数 |
| `retention_join_days` | 90 | 入群申请记录保留天数 |
| `db_path` | 空 | 自定义审计库路径（默认 `data/plugin_data/.../moderation.db`） |
| `store_text` | false | 是否在审计库保存消息正文（默认只存摘要 + 前 120 字） |

## 6. 常见问题

**Q：为什么群信息显示"平台未返回"？**
A：群信息与群状态接口目前**仅对白名单机器人开放**（`err_code=11253`），需要向 QQ 开放平台申请；不影响其它功能。

**Q：为什么无法启用审核？**
A：需要群管理员在手机 QQ 的机器人资料页开启「接收全部消息」。管理台会直接给出指引原文。

**Q：成员列表 / 黑名单提示失败？**
A：这 5 个接口官方标注"该能力正在内邀接入中"。插件会调用并记录失败原因（含 trace_id），界面显示"内邀未开放"。

**Q：日志数据存在哪里？会不会无限增长？**
A：`data/plugin_data/astrbot_plugin_qq_group_manager/moderation.db`，按上表的保留期每日自动裁剪，可在工具页手动裁剪/备份/清空。
