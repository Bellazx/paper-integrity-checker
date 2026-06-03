# 论文诚信检测 API 文档

本文档按当前 `api/` 代码整理，覆盖前端上传、提交、轮询、任务管理和开发者接口。

## 基本信息

- 服务根地址：`http://10.119.9.99/paper-api`
- FastAPI 内部前缀：`/api`
- 前端推荐流程：`POST /api/upload` -> `POST /api/detection/submit` -> `GET /api/task/{task_id}`
- 开发者写入 `yujing_*` 表流程：`POST /api/upload` -> `POST /api/task/submit` -> `GET /api/task/{task_id}`
- 支持压缩包：`.zip`、`.rar`
- 上传大小上限：`2GB`（2048MB）
- 批量论文上限：`10` 篇
- 未完成任务上限：`30` 个；超过后提交接口返回 `503`
- `/api/run` 流式接口保留给管理员调试，跨请求最多 `1` 个同时运行；超出返回 `429`

完整外部 URL 示例：

```text
http://10.119.9.99/paper-api/api/upload
http://10.119.9.99/paper-api/api/detection/submit
http://10.119.9.99/paper-api/api/task/{task_id}
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
- 应用层队列保护：异步任务路径最多保留 `30` 个未完成任务，超过后 `POST /api/detection/submit` 和 `POST /api/task/submit` 返回 `503`，并带 `Retry-After: 60`。

当前异步任务执行上限：

| 项目 | 上限 |
|---|---|
| 全局异步任务并行数 | `3` |
| 每批论文数 | `10` |
| 初审检测子进程并发 | `2` |
| AI 复核并发 | `4` |
| `/api/run` 跨请求并发 | `1` |

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
curl -X POST "http://10.119.9.99/paper-api/api/upload" \
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
curl -X POST "http://10.119.9.99/paper-api/api/detection/submit" \
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
curl -X POST "http://10.119.9.99/paper-api/api/detection/submit" \
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
curl -X POST "http://10.119.9.99/paper-api/api/task/submit" \
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
curl "http://10.119.9.99/paper-api/api/task/20260602-a1b2c3d4"
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
        "chushen_report_url": "http://10.119.9.99/chinese_reports/detection_reports/10.3389_fcimb.2021.649067_xxx.pdf",
        "review_result": "低风险",
        "review_report_url": "http://10.119.9.99/review_reports/detection_reports/review_10.3389_fcimb.2021.649067.pdf",
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
- 前端展示进度时优先使用 `status` 和 `progress.current / progress.total`

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
curl "http://10.119.9.99/paper-api/api/health"
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
curl "http://10.119.9.99/paper-api/api/health/detailed"
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
curl -N -X POST "http://10.119.9.99/paper-api/api/run" \
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
http://10.119.9.99/chinese_reports/{namespace}/{filename}.pdf
```

AI 复核报告：

```text
http://10.119.9.99/review_reports/{namespace}/review_{doi_slug}.pdf
```

`doi_slug` 规则与代码中的 `doi_to_slug()` 一致，通常是把 DOI 中的 `/` 等特殊字符替换成 `_`：

```text
10.3389/fcimb.2021.649067
=> /review_reports/detection_reports/review_10.3389_fcimb.2021.649067.pdf
```

`namespace` 用于隔离不同业务表/流程产生的同 DOI 报告，避免 PDF 互相覆盖：

- 前端 `/api/detection/submit` 使用 `detection_reports/{task_id}`
- 开发者 `/api/task/submit` 使用传入的 `table_name`，例如 `yujing_quanliang`

## 11. 前端推荐实现

1. 调用 `POST /api/upload`，拿到 `file_id` 和 `file_info.mode`。
2. 调用 `POST /api/detection/submit`，传入 `file_id`、`mode`、`author_type`。单篇场景有 DOI 时传 `doi`。
3. 根据返回的 `poll_url` 轮询 `GET /api/task/{task_id}`。
4. `status=completed` 时读取：
   - `result.detection_reports[].chushen_report_url`
   - `result.detection_reports[].review_report_url`
   - `result.detection_reports[].chushen_result`
   - `result.detection_reports[].review_result`
5. `status=failed` 时展示 `error`。如果 `result.detection_reports[].review_error` 有值，表示初审已完成但 AI 复核进程失败，前端应展示为“复核失败/待重试”，不要展示为复核高风险。

前端不要根据任务是否有 `review_report_url` 判断初审是否成功。低风险论文不会进入 AI 复核，因此可能只有 `chushen_report_url`，没有 `review_report_url`。

## 12. 常见问题

### 为什么低风险论文没有复核报告？

系统只对初审高风险论文执行 AI 复核。初审低风险论文只有初审中文报告，没有 AI 复核报告。

### 批量任务中如何区分同一次上传里的不同论文？

前端流程中 `submission_no=file_id`。批量模式下，多篇论文共享同一个 `submission_no`，用 `fold_name` 区分；单篇模式 `fold_name=null`。

### `file_id` 能重复提交吗？

可以。上传目录会保留，允许同一个 `file_id` 重复提交。前端用户流程写入 `detection_reports` 时，同一个 `submission_no + fold_name` 会更新已有记录。

### 服务重启后任务会怎样？

任务状态持久化在 `data/tasks/{task_id}/status.json`。服务启动时会加载历史任务；如果发现任务在服务重启前未完成，会标记为 `failed`，错误为 `Server restarted during processing`。

### 报告生成失败时前端看哪里？

轮询结果中的顶层 `error` 会包含错误摘要，例如 `Report generation failed: ...`。如果是单篇检测失败，则对应论文的 `papers[].error` 会有具体原因。
