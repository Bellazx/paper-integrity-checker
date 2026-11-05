# 论文诚信检测 API 文档

本文档按当前 `api/` 代码整理，覆盖前端上传、提交、轮询、任务管理和开发者接口。

## 基本信息

- 服务根地址：`http://202.120.57.100/paper-api`
- FastAPI 内部前缀：`/api`
- 前端推荐流程：`POST /api/upload` -> `POST /api/detection/submit` -> `GET /api/task/{task_id}`
- 开发者写入 `yujing_*` 表流程：`POST /api/upload` -> `POST /api/task/submit` -> `GET /api/task/{task_id}`
- 支持压缩包：`.zip`、`.rar`
- 上传大小上限：`2GB`（2048MB）
- 批量论文上限：`10` 篇
- 用户 API 同时执行上限：`120` 个任务，全体批量/单篇共享 `120` 个检测、复审或报告生成工作位
- 未完成任务上限：`240` 个（含运行和排队）；超过后提交接口返回 `503`
- `/api/run` 流式接口保留给管理员调试，跨请求最多 `1` 个同时运行；超出返回 `429`

完整外部 URL 示例：

```text
http://202.120.57.100/paper-api/api/upload
http://202.120.57.100/paper-api/api/detection/submit
http://202.120.57.100/paper-api/api/task/{task_id}
```

## 接口总览

| 方法 | 路径 | 用途 | 推荐给前端 |
|---|---|---|---|
| `GET` | `/api/health` | 基础健康检查 | 是 |
| `GET` | `/api/health/detailed` | 健康检查 + 数据库连通性 | 运维 |
| `POST` | `/api/upload` | 上传并同步校验 ZIP/RAR，返回 `file_id` | 是 |
| `POST` | `/api/detection/submit` | 前端用户提交任务，结果写入 `detection_reports` | 是 |
| `POST` | `/api/task/submit` | 开发者提交任务，结果写入指定 `yujing_*` 表 | 否 |
| `GET` | `/api/task/{task_id}` | 查询任务状态、进度、最终结果 | 是 |
| `GET` | `/api/task` | 查询任务列表 | 可选 |
| `DELETE` | `/api/task/{task_id}` | 删除已完成或失败的任务记录 | 可选 |
| `POST` | `/api/run` | 单请求 NDJSON 流式执行完整流程 | 调试用 |
| `POST` | `/api/task/single` | 旧版：直接上传 ZIP 并处理单篇 | 兼容 |
| `POST` | `/api/task/batch` | 旧版：直接上传 ZIP 并处理批量 | 兼容 |

## 任务状态

`GET /api/task/{task_id}` 的 `status` 字段只会是以下值之一：

| status | 含义 |
|---|---|
| `queued` | 已创建，等待执行 |
| `extracting` | 解压/识别论文中 |
| `detecting` | 初审检测中 |
| `reviewing` | AI 复核中，仅初审高风险论文进入 |
| `generating_report` | 生成 AI 复核 PDF 中 |
| `completed` | 任务完成 |
| `failed` | 任务失败 |

`stage` 是更细的当前阶段字符串，通常与 `status` 对应，例如 `extracting`、`detecting`、`reviewing`、`generating_report`、`done`。

## 并发与限流

系统有两层保护：

- nginx 入口限流：上传、提交、轮询、`/api/run` 分别限制 QPS/连接数，超限返回 `429`。
- 应用层队列保护：异步任务路径最多保留 `240` 个未完成任务，超过后 `POST /api/detection/submit` 和 `POST /api/task/submit` 返回 `503`，并带 `Retry-After: 60`。

当前异步任务执行上限：

| 项目 | 上限 |
|---|---|
| 用户 `/api/detection/submit` 任务并行数 | `120` |
| 用户 API 跨任务、跨批次重型工作位总数 | `120` |
| 开发者 `/api/task/submit` 任务并行数 | `3` |
| 每批论文数 | `10` |
| 开发者单任务初审检测并发 | `2` |
| 每个批次 AI 复核并发 | `4`，用户入口仍受全局 `120` 工作位约束 |
| `/api/run` 跨请求并发 | `1` |

`GET /api/health` 的 `api_capacity` 返回已加载的任务/工作位上限、当前
`active_paper_stages` 与 `waiting_paper_stages`。120 是并发工作位数，
不是包含所有工具子进程和线程的操作系统进程总数。

本机部署配置见 `deployment/paper-api-priority.conf`：用户 API 使用独立并发预算，
CPU shares 为 `8192`（默认 `1024`），nice 为 `-5`，I/O best-effort 优先级为 `0`。
这些是资源争用时的相对优先级，不会为闲置任务预留 CPU。

调用方处理建议：

- 收到 `429`：降低请求频率，按 `Retry-After` 或至少 30-60 秒后重试。
- 收到 `503`：说明任务队列已满，不要立即重试，建议提示用户稍后提交。
- 前端轮询建议 2-5 秒一次，不要高频轮询。

## 压缩包结构

单篇模式可以是：

```text
paper.zip
├── paper.pdf
├── source data.xlsx
└── supplementary.pdf
```

也可以多包一层目录：

```text
paper.zip
└── paper_folder/
    ├── paper.pdf
    └── source data.xlsx
```

批量模式要求每篇论文在独立子目录中：

```text
batch.zip
├── paper_a/
│   ├── a.pdf
│   └── source_data.xlsx
├── paper_b/
│   ├── b.pdf
│   └── supplementary.xlsx
└── paper_c/
    └── c.pdf
```

上传校验会自动判断 `mode` 为 `single` 或 `batch`，并返回在 `file_info.mode` 中。提交时建议前端使用上传返回的 `mode`。

## 1. 上传并校验

```http
POST /api/upload
Content-Type: multipart/form-data
```

请求参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `file` | File | 是 | 无 | ZIP 或 RAR 压缩包 |
| `username` | string | 否 | `anonymous` | 用于生成 `file_id`，只保留字母数字、`_`、`-`，最长 20 位 |

示例：

```bash
curl -X POST "http://202.120.57.100/paper-api/api/upload" \
  -F "file=@paper.zip" \
  -F "username=frontend_user"
```

成功响应：

```json
{
  "file_id": "20260602103015_frontend_user_a1b2c3d4",
  "message": "上传成功，识别到 1 篇论文",
  "file_info": {
    "filename": "upload.zip",
    "size_mb": 15.3,
    "paper_count": 1,
    "mode": "single",
    "papers": [
      {
        "dir_name": "(root)",
        "has_pdf": true
      }
    ]
  }
}
```

上传包会保存到 `data/uploads/YYYYMMDD-task/{file_id}/`。`file_id` 带随机后缀，即使同一用户在同一秒上传同名文件，也会生成不同任务输入。

失败响应格式：

```json
{
  "success": false,
  "message": "压缩包内容校验失败，共 2 个问题",
  "errors": [
    "压缩包内含不安全路径: ../bad.pdf",
    "压缩包内未找到任何 PDF 文件，每篇论文必须包含至少一个 PDF"
  ]
}
```

常见 HTTP 状态码：

| 状态码 | 场景 |
|---|---|
| `400` | 格式不支持、压缩包损坏、内容校验失败 |
| `413` | 文件超过 2GB（2048MB） |

校验规则：

- 文件扩展名必须是 `.zip` 或 `.rar`
- 压缩包必须可正常打开
- 至少包含一个 `.pdf`
- 不允许路径中包含 `..` 或绝对路径
- 不允许 `.exe`、`.sh`、`.bat`、`.cmd`、`.py`、`.js`、`.jar`、`.dll`、`.so`、`.bin`、`.msi`、`.com`、`.vbs`、`.ps1`
- 批量模式最多 10 篇

## 2. 前端用户提交任务

前端用户应调用这个接口。该流程固定写入 `detection_reports` 表，前端不需要也不能传 `table_name`。

```http
POST /api/detection/submit
Content-Type: application/json
```

请求体：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `file_id` | string | 是 | 无 | `POST /api/upload` 返回的文件 ID |
| `mode` | string | 否 | `single` | `single` 或 `batch`；建议使用上传返回的 `file_info.mode` |
| `doi` | string | 否 | 空字符串 | 单篇模式可传 DOI 覆盖自动识别结果；批量模式不使用 |
| `author_type` | string | 否 | 空字符串 | 前端传入的作者类型，例如 `通讯作者`、`第一作者` |
| `max_workers` | integer | 否 | `4` | 并发参数；检测阶段内部会按服务端保护策略限流 |

单篇示例：

```bash
curl -X POST "http://202.120.57.100/paper-api/api/detection/submit" \
  -H "Content-Type: application/json" \
  -d '{
    "file_id": "20260602103015_frontend_user",
    "mode": "single",
    "doi": "10.3389/fcimb.2021.649067",
    "author_type": "通讯作者",
    "max_workers": 4
  }'
```

批量示例：

```bash
curl -X POST "http://202.120.57.100/paper-api/api/detection/submit" \
  -H "Content-Type: application/json" \
  -d '{
    "file_id": "20260602104520_frontend_user",
    "mode": "batch",
    "author_type": "第一作者",
    "max_workers": 4
  }'
```

成功响应：

```json
{
  "task_id": "20260602-a1b2c3d4",
  "status": "queued",
  "message": "任务已创建，1 篇论文进入检测队列",
  "poll_url": "/api/task/20260602-a1b2c3d4"
}
```

失败响应：

| 状态码 | 场景 |
|---|---|
| `404` | `file_id` 不存在或上传文件已过期 |
| `400` | `mode` 不是 `single`/`batch`，或解压失败，或批量超过 10 篇 |

`detection_reports` 入库规则：

- `submission_no` 固定等于 `file_id`
- 单篇提交：`fold_name` 写入 `NULL`
- 批量提交：同一个 `submission_no` 下，每篇论文用源目录名作为 `fold_name`
- submit 解压后的论文保存到 `data/input/YYYYMMDD-task/{task_id}/{paper_slug}/`；同 DOI/同文件名的不同任务不会共用输入目录
- 前端流程的初审/复审报告 namespace 为 `detection_reports/{task_id}`，避免同 DOI 报告 PDF 相互覆盖
- 初审完成后写入 `chushen_result`、`chushen_report_url`，`status=0`
- 只有初审高风险论文会进入 AI 复核
- AI 复核 PDF 生成并写库后，写入 `review_result`、`review_report_url`，`status=2`
- 如果 AI 复核进程超时、解析失败或重试耗尽，不会生成最终复核 PDF，也不会把流程错误写成 `review_result=高风险`；该记录保留 `status=0`、复审字段为空，任务返回 `status=failed`
- 同一个 `file_id + fold_name` 重复提交时会更新原记录，并重置旧复审字段

注意：`detection_reports.status` 是数据库生命周期码，不是成功/失败码。`status=0`
对初审低风险论文是正常的“初审已完成、无需复审”状态；不能因为
`review_result` 或 `review_report_url` 为空就显示失败。接口返回的
`result.detection_reports[]`（以及对应任务的 `papers[]`）还提供语义字段：

- `processing_status=completed_initial`：检测完成（初审低风险）
- `processing_status=completed_review`：检测完成（已复审）
- `processing_status=review_pending`：初审完成，等待复审
- `processing_status=review_failed`：复审失败/待重试
- `processing_status=failed`：检测失败

页面应优先使用 `processing_status` 和顶层任务 `status` 判断是否失败，不要直接把
数据库 `status=0` 映射为失败。

## 3. 开发者提交任务

开发者接口用于写入 `yujing_*` 表，不建议普通前端调用。

```http
POST /api/task/submit
Content-Type: application/json
```

请求体：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `file_id` | string | 是 | 无 | `POST /api/upload` 返回的文件 ID |
| `mode` | string | 否 | `single` | `single` 或 `batch` |
| `doi` | string | 否 | 空字符串 | 单篇模式可覆盖 DOI |
| `table_name` | string | 否 | `yujing_quanliang` | 目标表名，必须匹配 `^yujing_[a-z0-9_]{1,50}$` |
| `author_type` | string | 否 | 空字符串 | 作者类型 |
| `max_workers` | integer | 否 | `4` | 批量并发参数 |

示例：

```bash
curl -X POST "http://202.120.57.100/paper-api/api/task/submit" \
  -H "Content-Type: application/json" \
  -d '{
    "file_id": "20260602103015_dev",
    "mode": "single",
    "doi": "10.1038/s41586-024-00123-4",
    "table_name": "yujing_quanliang",
    "author_type": "通讯作者"
  }'
```

表名限制：

- 允许：`yujing_quanliang`、`yujing_test`、`yujing_abc_123`
- 禁止：`yujing`
- 禁止：任何不符合 `yujing_` 前缀和小写字母数字下划线规则的表名

## 4. 查询任务状态

```http
GET /api/task/{task_id}
```

示例：

```bash
curl "http://202.120.57.100/paper-api/api/task/20260602-a1b2c3d4"
```

处理中响应示例：

```json
{
  "task_id": "20260602-a1b2c3d4",
  "mode": "single",
  "status": "detecting",
  "stage": "detecting",
  "progress": {
    "current": 0,
    "total": 1,
    "stage": "detection"
  },
  "created_at": "2026-06-02T10:30:16.123456",
  "updated_at": "2026-06-02T10:31:20.123456",
  "completed_at": null,
  "result": null,
  "error": null,
  "papers": [
    {
      "doi_slug": "10.3389_fcimb.2021.649067",
      "doi": "10.3389/fcimb.2021.649067",
      "status": "pending",
      "error": null
    }
  ]
}
```

前端用户流程完成响应示例：

```json
{
  "task_id": "20260602-a1b2c3d4",
  "mode": "single",
  "status": "completed",
  "stage": "done",
  "progress": {
    "current": 1,
    "total": 1,
    "stage": "review"
  },
  "created_at": "2026-06-02T10:30:16.123456",
  "updated_at": "2026-06-02T10:45:20.123456",
  "completed_at": "2026-06-02T10:45:20.123456",
  "result": {
    "total_papers": 1,
    "detected_ok": 1,
    "detected_fail": 0,
    "high_risk_detected": 1,
    "reviewed": 1,
    "confirmed_high": 0,
    "downgraded": 1,
    "reports_generated": 1,
    "detection_reports": [
      {
        "submission_no": "20260602103015_frontend_user",
        "fold_name": null,
        "doi": "10.3389/fcimb.2021.649067",
        "chushen_result": "高风险",
        "chushen_report_url": "http://202.120.57.100/chinese_reports/detection_reports/10.3389_fcimb.2021.649067_xxx.pdf",
        "review_result": "低风险",
        "review_report_url": "http://202.120.57.100/review_reports/detection_reports/review_10.3389_fcimb.2021.649067.pdf",
        "status": 2,
        "review_error": null
      }
    ]
  },
  "error": null,
  "papers": [
    {
      "doi_slug": "10.3389_fcimb.2021.649067",
      "doi": "10.3389/fcimb.2021.649067",
      "status": "detected",
      "error": null
    }
  ]
}
```

任务失败响应示例：

```json
{
  "task_id": "20260602-a1b2c3d4",
  "mode": "single",
  "status": "failed",
  "stage": "generating_report",
  "progress": {
    "current": 0,
    "total": 1,
    "stage": "report"
  },
  "created_at": "2026-06-02T10:30:16.123456",
  "updated_at": "2026-06-02T10:40:00.123456",
  "completed_at": "2026-06-02T10:40:00.123456",
  "result": null,
  "error": "Report generation failed: ...",
  "papers": []
}
```

轮询建议：

- 单篇任务通常需要 `20-40` 分钟，建议每 `20-30` 秒轮询一次
- 批量任务耗时更长，建议每 `30-60` 秒轮询一次
- 当 `status` 为 `completed` 或 `failed` 时停止轮询
- `progress.current / progress.total` 是当前阶段完成的论文数，不是单篇任务的耗时百分比；单篇复审期间显示 `0/1` 属于正常情况。

需要阶段进度条和复审活动时，使用只读接口
`GET /paper-progress/api/task/{task_id}/progress`（注意前缀为 `/paper-progress`，不叠加 `/paper-api`）。
页面地址为 `/paper-progress/?tasks=<task_id>,<task_id>`，每 15 秒自动刷新。
该接口提供文件准备、初审、复审、报告确认的阶段状态，以及当前复审轮次和最近工具活动时间。
仅任务完成时返回 `percent=100`，运行中为 `null`；前端显示阶段进度及当前阶段动画，不按已耗时虚构百分比。
字段和接入说明见 [任务进度展示](../docs/task-progress.md)。

## 5. 查询任务列表

```http
GET /api/task?limit=20&offset=0&status=completed
```

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `limit` | integer | 否 | `20` | 返回数量 |
| `offset` | integer | 否 | `0` | 分页偏移 |
| `status` | string | 否 | 无 | 按任务状态过滤 |

响应示例：

```json
{
  "tasks": [
    {
      "task_id": "20260602-a1b2c3d4",
      "mode": "single",
      "status": "completed",
      "paper_count": 1,
      "created_at": "2026-06-02T10:30:16.123456"
    }
  ],
  "total": 1
}
```

## 6. 删除任务

```http
DELETE /api/task/{task_id}
```

只允许删除 `completed` 或 `failed` 状态的任务。

成功响应：

```json
{
  "message": "Task '20260602-a1b2c3d4' deleted"
}
```

失败场景：

| 状态码 | 场景 |
|---|---|
| `404` | 任务不存在 |
| `400` | 任务仍在运行，不能删除 |

## 7. 健康检查

基础健康检查：

```bash
curl "http://202.120.57.100/paper-api/api/health"
```

响应：

```json
{
  "status": "ok",
  "service": "paper-integrity-checker",
  "version": "1.0.0"
}
```

详细健康检查：

```bash
curl "http://202.120.57.100/paper-api/api/health/detailed"
```

响应：

```json
{
  "status": "ok",
  "service": "paper-integrity-checker",
  "version": "1.0.0",
  "db": "connected"
}
```

## 8. 流式调试接口

`POST /api/run` 是单请求流式接口，直接上传文件并以 NDJSON 返回每一步进度。它适合调试或命令行观察，不是前端主流程首选。

```http
POST /api/run
Content-Type: multipart/form-data
Accept: application/x-ndjson
```

请求参数：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `file` | File | 是 | 无 | ZIP 或 RAR 压缩包 |
| `mode` | string | 否 | 空字符串 | 空则自动识别；也可传 `single`/`batch` |
| `doi` | string | 否 | 空字符串 | 单篇 DOI 覆盖 |
| `table_name` | string | 否 | `yujing_quanliang` | 目标 `yujing_*` 表 |
| `author_type` | string | 否 | 空字符串 | 作者类型 |
| `max_workers` | integer | 否 | `4` | 并发参数 |

示例：

```bash
curl -N -X POST "http://202.120.57.100/paper-api/api/run" \
  -F "file=@paper.zip" \
  -F "mode=single" \
  -F "doi=10.1038/s41586-024-00123-4" \
  -F "table_name=yujing_quanliang"
```

返回是多行 JSON，每行一个事件：

```json
{"stage":"validating","message":"保存并校验压缩包..."}
{"stage":"extracting","message":"校验通过，1 篇论文 (single 模式)，解压中..."}
{"stage":"detecting","message":"[1/1] 10.1038_s41586-024-00123-4 → 高风险"}
{"stage":"reviewing","message":"[1/1] 10.1038/s41586-024-00123-4 → 建议低风险 (低风险)"}
{"stage":"completed","message":"全部完成","result":{"total_papers":1}}
```

## 9. 旧版兼容接口

以下接口仍保留，但新前端建议使用 `upload + detection/submit + task polling`。

### 直接提交单篇 ZIP

```http
POST /api/task/single
Content-Type: multipart/form-data
```

参数：

| 字段 | 类型 | 默认值 |
|---|---|---|
| `file` | File | 必填，仅 ZIP |
| `table_name` | string | `yujing_quanliang` |
| `skip_refs` | boolean | `false` |
| `author_type` | string | 空字符串 |
| `skip_review` | boolean | `false` |

### 直接提交批量 ZIP

```http
POST /api/task/batch
Content-Type: multipart/form-data
```

参数：

| 字段 | 类型 | 默认值 |
|---|---|---|
| `file` | File | 必填，仅 ZIP |
| `table_name` | string | `yujing_quanliang` |
| `skip_refs` | boolean | `false` |
| `author_type` | string | 空字符串 |
| `skip_review` | boolean | `false` |
| `max_workers` | integer | `4` |

## 10. 报告 URL

初审中文报告：

```text
http://202.120.57.100/chinese_reports/{namespace}/{filename}.pdf
```

AI 复核报告：

```text
http://202.120.57.100/review_reports/{namespace}/review_{doi_slug}.pdf
```

`doi_slug` 规则与代码中的 `doi_to_slug()` 一致，通常是把 DOI 中的 `/` 等特殊字符替换成 `_`：

```text
10.3389/fcimb.2021.649067
=> /review_reports/detection_reports/review_10.3389_fcimb.2021.649067.pdf
```

`namespace` 用于隔离不同业务表/流程产生的同 DOI 报告，避免 PDF 互相覆盖：

- 前端 `/api/detection/submit` 使用 `detection_reports/{task_id}`
- 开发者 `/api/task/submit` 使用 `{table_name}/{task_id}`，例如 `yujing_quanliang/20261104-f71f600c`

三类报告链接由 nginx 转给只读报告网关（`deployment/report_gateway.py`，端口 8002）。网关先按历史目录结构查找（含旧主机迁移来的 `data/migrated_99_reports`），找不到再按 `chinese_reports/`、`english_reports/`、`review_v2/` 下的命名空间目录查找。网关只返回 PDF；`review_v2/` 中的证据包等其他文件不对外提供。

## 11. 前端推荐实现

1. 调用 `POST /api/upload`，拿到 `file_id` 和 `file_info.mode`。
2. 调用 `POST /api/detection/submit`，传入 `file_id`、`mode`、`author_type`。单篇场景有 DOI 时传 `doi`。
3. 根据返回的 `poll_url` 轮询 `GET /api/task/{task_id}`。
4. `status=completed` 时读取：
   - `result.detection_reports[].chushen_report_url`
   - `result.detection_reports[].review_report_url`
   - `result.detection_reports[].english_chushen_report_url`
   - `result.detection_reports[].english_review_report_url`
   - `result.detection_reports[].chushen_result`
   - `result.detection_reports[].review_result`
5. `status=failed` 时展示 `error`。如果 `result.detection_reports[].review_error` 有值，表示初审已完成但 AI 复核进程失败，前端应展示为“复核失败/待重试”，不要展示为复核高风险。

前端不要根据任务是否有 `review_report_url` 判断初审是否成功。低风险论文不会进入 AI 复核，因此可能只有 `chushen_report_url`，没有 `review_report_url`。

提交检测时可在 JSON 中传 `language`：`zh`（仅中文）、`en`（仅英文链接）、`both`（默认，中文和英文均生成）。英文报告地址分别通过 `english_chushen_report_url` 和 `english_review_report_url` 返回；英文生成失败不会覆盖中文报告，具体原因会放在 `english_report_error`。

## 12. 常见问题

### 为什么低风险论文没有复核报告？

系统只对初审高风险论文执行 AI 复核。初审低风险论文只有初审中文报告，没有 AI 复核报告。

### 批量任务中如何区分同一次上传里的不同论文？

前端流程中 `submission_no=file_id`。批量模式下，多篇论文共享同一个 `submission_no`，用 `fold_name` 区分；单篇模式 `fold_name=null`。

### `file_id` 能重复提交吗？

可以。上传目录会保留，允许同一个 `file_id` 重复提交。前端用户流程写入 `detection_reports` 时，同一个 `submission_no + fold_name` 会更新已有记录。

`file_id` 标识一次上传保存的文件，重复提交检测时不变；`task_id` 标识每次检测任务。
如果重新调用上传接口，即使文件内容相同，也会得到新的 `file_id`；当前上传接口不按内容去重。

每次明确提交都会创建新的任务号，按提交到达时的状态决定执行策略：

- 同一 `file_id` 的论文仍在处理中（批量模式还须 `fold_name` 一致）：新任务替代旧任务。正文、源数据、检测配置和规则版本完全一致时，沿用已完成的初审及复审检查点；未保存的轮次重新执行。存在差异或缺少历史指纹时重新检测，不沿用旧结论。
- 同一 `file_id` 的旧任务已经完成或对应论文已有最终风险结论：新任务从初审重新执行正常流程，不复用旧任务的结果；初审低风险直接完成，高风险进入复审。
- 同一 `file_id` 的旧任务因服务重启、进程中断或复核流程错误而失败，且没有最终风险结论：重新检测会复用已完成的初审报告、证据包和 CLI 复核断点，从中断阶段继续；如果断点或输入校验不匹配，才回退到相应阶段重跑。
- 不同 `file_id` 始终独立处理，即使上传用户、DOI 和文件内容完全相同，也不替代或复用对方的任务。
- 被整体替代的旧任务返回 `status=superseded`、`superseded_by=<新任务号>`。前端应显示“已被新提交替代”，停止轮询旧任务，并转到新任务号；不得将其显示为检测失败。
- 批量任务仅替代匹配论文，其他论文继续执行。每篇的 `superseded_by`、`reused_from_task_id`、`submission_decision` 可在任务查询接口读取。
- 新任务提交完成时即更新数据库 `task_id` 并清空旧结论字段，初审完成后才填入新的初审结果。旧报告文件保留，不能用它表示新任务已完成。

`submission_decision` 为 `fresh`、`fresh_after_terminal`、`replace_unfinished` 或
`resume_unfinished`，分别表示首次独立处理、结束后重检、替代但不能复用、替代并续用检查点。

### 服务重启后任务会怎样？

任务状态持久化在 `data/tasks/{task_id}/status.json`。服务启动时会检查未完成任务：如果解压后的论文输入仍在磁盘，会自动重新排队并继续处理；只有在解压尚未完成或输入文件已不存在时，才会标记为 `failed`，错误为 `Server restarted during processing (task inputs unavailable)`。

### 报告生成失败时前端看哪里？

轮询结果中的顶层 `error` 会包含错误摘要，例如 `Report generation failed: ...`。如果是单篇检测失败，则对应论文的 `papers[].error` 会有具体原因。


### 复核引擎（2026-11-04 起 API 使用 v3）

API 与批量复核使用同一个复核引擎 v3（设计见 `review_v3/DESIGN.md`），由服务环境变量 `REVIEW_ENGINE=v3` 启用（`deployment/paper-api-review-engine.conf`，安装为 `/etc/systemd/system/paper-api.service.d/95-review-engine.conf`）。改为 `v2` 并重启服务即回退到下一节所述的旧流程。

- 流程：证据包与论文资料包 → 按维度分片并行调查（MiniMax-M3.1-Flash）→ 逐分片反证核验 → 分歧仲裁与必查项补查 → 定级 → 实质问题由 Opus 终审 → 每个风险问题截图 → 写作 → 发布校验。
- 风险结论由台账确定：任一维度存在经终审确认的实质问题即“有风险”。报告只列风险问题，每条写明位置、问题和影响。
- 证据截图：问题同时涉及论文页面和随文源数据时，两侧都附截图（源数据按问题引用的数值定位单元格，带表头和行名）；截图会话未完成时由程序补截。
- 时限：`REVIEW_PAPER_TIMEOUT_SECONDS` 为整篇总时限，默认 5400 秒（90 分钟）；各阶段另有时间框（`REVIEW_V3_*_SOFT_SECONDS`）。到总时限时用已核实的结论生成报告，未能核查的必查项在报告中写明。
- 所有阶段落盘检查点；`resume=True` 时已完成的分片直接复用。
- 每段会话记录工具调用的等待时间（`tool_wait_s`），并拆分出 PreToolUse hook 开始之前的部分（`before_hook_s`）；中位数超过 `REVIEW_V3_TOOL_WAIT_WARN_SECONDS`（默认 5 秒）时服务日志给出警告。命令本身通常在 1 秒内完成，持续的长等待说明问题在 CLI 或机器环境，而不在复核逻辑。

### v2 复核恢复与时间预算（2026-10-28，仅 `REVIEW_ENGINE=v2` 时适用）

- `run_review_single(..., resume=True)` / `run_reviews(..., resume=True)` 用于中断恢复，复用同一输入的综合草稿和 CLI 断点；默认 `resume=False` 表示显式重新复核，独立保存阶段历史。
- 第一轮、第二轮或综合阶段返回 `review_error` 后终止该篇，保留检查记录，不再把错误占位交给后续专家继续跑。
- CLI 研究、恢复和最终整理不再使用独立的阶段时间预算；三者共享单篇论文的唯一总时限。达到首轮轮数上限时，只有已有完整草稿且剩余问题属于格式校验，才直接进入无工具整理；没有草稿或存在事实/证据覆盖缺口时，先执行一次定向补查，默认轮数不低于首轮，禁止重复全表扫描。低风险核验、综合判断和覆盖修复的默认轮数也不低于 `REVIEW_MAX_TURNS`。到达总时限时回收当前 CLI 进程并保留断点，后续可从同一会话恢复；轮数上限仍可用 `REVIEW_MAX_TURNS`、`REVIEW_CLI_RECOVERY_MAX_TURNS` 和 `REVIEW_CLI_FINAL_MAX_TURNS` 调整。事实与证据缺口仍必须核查，不能放宽发布校验。超时或输出错误不作为论文风险证据。
- 首轮会根据确定性证据包选择自适应核查计划：无主图且已有强源数据证据时只读取证据和必要位置，禁止重复全表扫描；有图像或未决线索时保留 Bash/Read 定向核查；证据干净时使用只读覆盖检查。计划只减少重复工作，不跳过四个维度的发布校验。
- `REVIEW_PAPER_TIMEOUT_SECONDS` 是单次整篇调用的总时限，默认 5400 秒（90 分钟）；到期取消并回收当前 CLI 进程组，保存失败状态和已有断点。强确定性数据证据在首轮后只进入定向覆盖修复，不再自动重复第二次整篇调查。
- 显式设置 `REVIEW_PAPER_TIMEOUT_SECONDS=0`（或 `none` / `unlimited`）可取消该进程的整篇限时；未设置时采用 5400 秒（90 分钟）。英文结果标签 `low_risk` / `high_risk` 会转换为中文枚举，维度缺项和证据覆盖校验仍必须通过。
- 复核阶段的 Claude CLI 默认通过 `--settings /home/library/.claude/settings-mm.json` 启动；可用 `REVIEW_CLAUDE_SETTINGS_FILE` 指定其他配置文件。该设置只作用于复核子进程。
- 默认初轮最多 24 轮，低风险核验/综合阶段最多 12 轮，覆盖修复最多 8 轮；证据包无高风险线索且首轮覆盖校验通过的低风险论文默认直接闭环，不再重复启动第二个完整 CLI 会话；存在任何可疑线索时仍保留二次核验。所有阶段只受同一个整篇总时限约束。
- `REVIEW_FAST_CLEAN_LOWRISK=0` 可恢复所有低风险结果都进行二次核验；默认值 `1` 只对“证据包无 HIGH/确定性异常且首轮覆盖校验通过”的干净论文启用快速闭环。
- 直接调用 `review_cli.run` 的独立脚本必须由调用方提供总时限。已经运行的 Python 进程不会热加载此修改，仍使用启动时的超时策略。
- 相同输入的论文状态用进程锁保护；Phillip 恢复脚本还持有覆盖复核和发布的批次锁。重复启动立即报 already running。
- 批量任务逐篇原子保存结果；错误结果不参与发布。Phillip 恢复脚本有未完成论文时退出码为 2，发布清单使用独立文件，保留失败详情。
- 发布校验将各维度已明确给出的等价风险标签统一为内部枚举，并识别中英文图号；不从总风险推导缺失的维度结论。截图仍必须对应具体编号问题。
