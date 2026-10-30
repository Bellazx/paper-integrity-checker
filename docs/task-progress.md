# 论文任务进度展示

独立只读页面：`/paper-progress/?tasks=<task_id>,<task_id>`。
前端轮询接口：`GET /paper-progress/api/task/{task_id}/progress`，建议间隔 15 秒。
服务监听本机 8013，独立于检测 API；读取任务状态和该论文的复审检查点、工具活动，不写数据库、不启动任务。

响应主要字段：

| 字段 | 含义 |
| --- | --- |
| `status` / `label` | 任务状态与中文阶段名称 |
| `steps` | 文件准备、初审、复审、报告与结果确认；每步为 completed/active/pending/skipped/stopped |
| `percent` | 仅 completed 为 100；其他状态为 null，不能根据时间或工具次数推算百分比 |
| `papers[].review_stage` | 从当前论文检查点推导的复审阶段，不表示最终风险结论 |
| `papers[].last_activity_at` | 最近可读取的工具调用/返回时间，UTC ISO 格式；无记录为 null |
| `papers[].activity_text` | 安全概述，不返回工具命令、会话原文、服务器路径或未发布风险结论 |
| `papers[].activity_state` | recent_activity：三分钟内有活动；quiet：超过三分钟；unknown：无活动信息；terminal：已结束 |
| `elapsed_seconds` | 累计耗时，包含等待和重启前时间；不是本轮运行时长 |
| `observed_at` | 本次读取时间，与实际工具活动时间不同 |

`quiet` 不能直接解释为卡死或失败；进程可能在等待模型响应。
轮询失败时保留旧显示并明确标为过期，不能显示为健康。
低风险初审完成的论文将复审阶段标记为 skipped。
完成状态来自任务记录，不由 PDF 文件存在、单轮低风险结论或工具调用次数推断。

部署单元：`deployment/paper-progress.service`。Nginx 使用 `/paper-progress/` 前缀代理至 `http://127.0.0.1:8013/`。
现有页面可复用该接口渲染阶段进度条；原 `/paper-api/api/task/{task_id}` 接口不变。
