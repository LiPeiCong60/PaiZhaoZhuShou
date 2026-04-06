# 最新 API 联调文档（截至 2026-04-03）

本文档基于当前仓库代码实际实现整理，入口以 `api.app:app` 为准，适合联调同学直接使用。

## 1. 基础信息

- 启动入口：`python -m uvicorn api.app:app --host 0.0.0.0 --port 8000`
- Swagger：`http://localhost:8000/docs`
- ReDoc：`http://localhost:8000/redoc`
- 基础前缀：`/api/v1`
- 会话模型：当前仅支持单活会话
- 参数风格：大多数接口使用 query 参数，不是 JSON body
- 上传风格：模板上传和图片分析上传使用 `multipart/form-data`

## 2. 联调前必须知道

- 除 `GET /api/v1/health`、`POST /api/v1/session/open`、`POST /api/v1/session/close`、`POST /api/v1/ai/analyze-upload` 外，其余接口都依赖当前会话。
- 没有活动会话时，依赖会话的接口返回 `503`，响应形如：

```json
{
  "detail": "服务未初始化，请先创建会话"
}
```

- 再次调用 `POST /api/v1/session/open` 会先关闭旧会话，再创建新会话。
- 当前 API 会话内部使用 `MockServoDriver()`，适合联调，不会默认直连真实云台硬件。
- 未配置 `SILICONFLOW_API_KEY` 时，AI 相关流程仍可跑通，但会返回 Mock AI 结果。

## 3. 模板接口重点说明

模板能力由 `TemplateService` 驱动，底层模板数据当前保存在本地 `.template_library/templates.json`，模板图片保存在 `.template_library/images/`。

### 3.1 模板导入链路

`POST /api/v1/templates/import` 的真实处理流程如下：

1. 接收上传文件，先写入系统临时文件。
2. 用 OpenCV 读取图片。
3. 调用检测器识别人像。
4. 从检测结果生成 `TemplateProfile`。
5. 持久化到本地模板库。
6. 自动把新模板设为当前选中模板。

这意味着模板导入成功后，不需要再额外调用一次选择接口，除非后续想切换到别的模板。

### 3.2 模板接口总览

| 接口 | 说明 | 会话依赖 |
| --- | --- | --- |
| `POST /api/v1/templates/import` | 上传并创建模板，成功后自动选中 | 是 |
| `GET /api/v1/templates/` | 获取模板列表 | 是 |
| `POST /api/v1/templates/select` | 选择模板 | 是 |
| `DELETE /api/v1/templates/{template_id}` | 删除模板 | 是 |

### 3.3 上传模板

`POST /api/v1/templates/import`

请求方式：

- `multipart/form-data`
- 文件字段名：`file`
- `name` 是 query 参数，不是 form 字段

请求示例：

```bash
curl -X POST "http://localhost:8000/api/v1/templates/import?name=半身模板" \
  -H "Content-Type: multipart/form-data" \
  -F "file=@C:/path/to/template.jpg"
```

成功响应：

```json
{
  "template_id": "9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a",
  "name": "半身模板",
  "created": true
}
```

真实行为说明：

- `template_id` 当前是 UUID 字符串，不是 `tpl_xxx` 风格。
- 如果未传 `name`，默认取上传文件名去掉扩展名后的值。
- 导入成功后会自动执行 `select_template(template_id)`。
- 模板图片会被复制到 `.template_library/images/<uuid>.<ext>`。

常见失败原因：

- 图片无法被 OpenCV 打开。
- 未检测到人物。
- 检测到了人，但无法生成有效模板区域。

失败响应示例：

```json
{
  "detail": "模板创建失败: 未检测到人物"
}
```

### 3.4 获取模板列表

`GET /api/v1/templates/`

请求示例：

```bash
curl "http://localhost:8000/api/v1/templates/"
```

成功响应：

```json
[
  {
    "template_id": "9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a",
    "name": "半身模板",
    "created_at": "2026-04-03 12:18:40",
    "image_path": ".template_library/images/fe87d6d1f2e249998d7d645f9a445a1b.jpg"
  }
]
```

注意事项：

- 当前公开列表只返回 4 个字段：`template_id`、`name`、`created_at`、`image_path`。
- `created_at` 当前是字符串时间，格式为 `YYYY-MM-DD HH:MM:SS`，不是 Unix 时间戳。
- 路由实际注册的是 `/api/v1/templates/`，建议联调时保留尾部斜杠。

### 3.5 选择模板

`POST /api/v1/templates/select`

请求示例：

```bash
curl -X POST "http://localhost:8000/api/v1/templates/select?template_id=9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a"
```

成功响应：

```json
{
  "message": "已选择模板 9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a",
  "selected_template_id": "9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a"
}
```

失败响应：

```json
{
  "detail": "模板不存在"
}
```

说明：

- `template_id` 是 query 参数。
- 选择成功后，会更新运行态中的 `selected_template_id`。
- 后续 `SMART_COMPOSE` 模式和状态接口都会使用这个值。

### 3.6 删除模板

`DELETE /api/v1/templates/{template_id}`

请求示例：

```bash
curl -X DELETE "http://localhost:8000/api/v1/templates/9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a"
```

成功响应：

```json
{
  "message": "模板已删除"
}
```

说明：

- 如果删除的是当前选中模板，运行态中的 `selected_template_id` 会被清空。
- 当前实现删除的是模板库中的记录，不会同步删除 `.template_library/images/` 下已保存的模板图片文件。
- 模板不存在时返回 `404`。

### 3.7 模板与构图状态的联动

模板接口通常需要和下面两个状态接口配合使用：

- `GET /api/v1/status`
- `GET /api/v1/status/compose`

`GET /api/v1/status/compose` 返回：

```json
{
  "compose_score": 76.4,
  "compose_ready": true,
  "selected_template_id": "9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a"
}
```

其中：

- `selected_template_id` 来自当前运行态。
- `compose_score` 和 `compose_ready` 只有在 `SMART_COMPOSE` 模式、存在稳定检测目标、且已选择模板时才会产生真实值。
- 没有模板或未进入模板构图模式时，这两个值通常分别表现为 `0.0` 和 `false`。
- `follow_mode=face` 时，模板比对会优先使用头部锚点；否则默认使用肩部锚点。

### 3.8 模板内部数据结构

虽然模板详情没有对外开放 API，但当前模板实际持久化的数据结构是 `TemplateProfile`，包含以下关键字段：

- `template_id`
- `name`
- `image_path`
- `created_at`
- `anchor_norm_x`
- `anchor_norm_y`
- `shoulder_anchor_norm_x`
- `shoulder_anchor_norm_y`
- `head_anchor_norm_x`
- `head_anchor_norm_y`
- `face_anchor_norm_x`
- `face_anchor_norm_y`
- `area_ratio`
- `facing_sign`
- `pose_points`
- `pose_points_image`
- `pose_points_bbox`
- `bbox_norm`

这说明当前模板不仅保存展示信息，还保存了人物锚点、姿态点、人体框比例等构图比对数据，因此联调时不要把模板理解为“只是一张图片”。

## 4. 全量接口清单

### 4.1 健康检查

`GET /api/v1/health`

响应：

```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### 4.2 会话接口

#### `POST /api/v1/session/open`

query 参数：

- `stream_url`：必填，OpenCV 可打开的视频流地址或设备编号字符串
- `mirror_view`：可选，默认 `true`
- `start_mode`：可选，默认 `MANUAL`

当前支持的 `start_mode`：

- `MANUAL`
- `AUTO_TRACK`
- `SMART_COMPOSE`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/session/open?stream_url=rtsp://your-stream-url&mirror_view=true&start_mode=MANUAL"
```

响应：

```json
{
  "session_id": "sess_ab12cd34",
  "status": "running",
  "message": "会话创建成功"
}
```

#### `POST /api/v1/session/close`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/session/close"
```

成功响应：

```json
{
  "message": "会话已关闭"
}
```

无活动会话时返回：

```json
{
  "detail": "没有活动的会话"
}
```

### 4.3 状态接口

#### `GET /api/v1/status`

该接口同时注册了：

- `/api/v1/status`
- `/api/v1/status/`

典型响应：

```json
{
  "session_id": "sess_ab12cd34",
  "mode": "SMART_COMPOSE",
  "follow_mode": "shoulders",
  "speed_mode": "normal",
  "compose_score": 76.4,
  "compose_ready": true,
  "selected_template_id": "9c7b9f1b-40ea-4ec0-8f8f-0d5b6b77dd4a",
  "tracking_stable": true,
  "ai_angle_search_running": false,
  "ai_lock_mode_enabled": true,
  "ai_lock_fit_score": 0.71,
  "ai_lock_target_box_norm": [0.38, 0.18, 0.24, 0.66],
  "latest_capture_path": "captures/2026-04-03/xxx.jpg",
  "latest_capture_error": null,
  "last_angle_search_result": {
    "best_score": 82.0,
    "summary": "当前角度构图更稳定",
    "best_pan": 2.0,
    "best_tilt": 0.0,
    "num_scanned": 5,
    "capture_path": "captures/2026-04-03/xxx.jpg"
  },
  "last_angle_search_error": null,
  "last_background_lock_result": null,
  "last_background_lock_error": null
}
```

#### `GET /api/v1/status/mode`

响应：

```json
{
  "mode": "MANUAL"
}
```

#### `GET /api/v1/status/compose`

返回模板构图相关状态，见上文模板章节。

#### `GET /api/v1/status/tracking`

响应：

```json
{
  "tracking_stable": true,
  "follow_mode": "shoulders",
  "speed_mode": "normal"
}
```

#### `GET /api/v1/status/ai`

响应：

```json
{
  "ai_angle_search_running": false,
  "ai_lock_mode_enabled": true,
  "ai_lock_fit_score": 0.71,
  "ai_lock_target_box_norm": [0.38, 0.18, 0.24, 0.66],
  "last_angle_search_result": null,
  "last_angle_search_error": null,
  "last_background_lock_result": null,
  "last_background_lock_error": null
}
```

### 4.4 控制接口

#### `POST /api/v1/control/mode`

query 参数：

- `mode`

允许值：

- `MANUAL`
- `AUTO_TRACK`
- `SMART_COMPOSE`

#### `POST /api/v1/control/manual-move`

两种调用方式二选一：

- 方式一：`action=w|a|s|d|up|down|left|right`
- 方式二：`pan_delta` 与 `tilt_delta`

如果既没传 `action`，也没传角度偏移，会返回 `400`。

#### `POST /api/v1/control/home`

云台回中。

#### `POST /api/v1/control/follow-mode`

query 参数：

- `follow_mode`

当前代码支持值：

- `shoulders`
- `face`

#### `POST /api/v1/control/speed`

query 参数：

- `speed_mode`

当前代码支持值：

- `slow`
- `normal`

### 4.5 抓拍接口

#### `POST /api/v1/capture/manual`

query 参数：

- `auto_analyze`：可选，默认 `false`

成功响应：

```json
{
  "message": "手动抓拍已完成",
  "capture_path": "captures/2026-04-03/xxx.jpg",
  "analysis": {
    "score": 82.0,
    "summary": "主体完整，构图稳定",
    "suggestions": ["人物可略向左"]
  },
  "analysis_error": null
}
```

### 4.6 AI 接口

#### `POST /api/v1/ai/analyze-upload`

说明：

- 不强依赖活动会话。
- 有活动会话时，会自动带上当前 `mode`、`follow_mode`、`speed_mode`、`compose_score`、`template_id`、`mirror_view` 作为上下文。

请求方式：

- `multipart/form-data`
- 文件字段：`file`
- query 参数：`analysis_type=photo|background`

#### `POST /api/v1/ai/angle-search/start`

query 参数：

- `pan_range`，默认 `6.0`
- `tilt_range`，默认 `3.0`
- `pan_step`，默认 `4.0`
- `tilt_step`，默认 `3.0`
- `max_candidates`，默认 `9`
- `settle_s`，默认 `0.35`

说明：

- 该接口是异步启动。
- 最终结果通过 `GET /api/v1/status` 或 `GET /api/v1/status/ai` 读取。
- 运行时会对扫描参数做收敛处理：`pan_range`/`tilt_range` 最小为 `1.0`，`pan_step`/`tilt_step` 最小为 `0.8`，`max_candidates` 会被限制在 `2-9` 之间。
- `settle_s` 虽然接口默认值是 `0.35`，但实际执行时会按不小于 `0.5` 秒处理。

#### `POST /api/v1/ai/background-lock/start`

query 参数：

- `pan_range`，默认 `6.0`
- `tilt_range`，默认 `3.0`
- `pan_step`，默认 `4.0`
- `tilt_step`，默认 `3.0`
- `max_candidates`，默认 `9`
- `settle_s`，默认 `0.35`
- `delay_s`，默认 `0.0`

说明：

- 该接口会启动背景扫描并进入锁机位模式。
- 结果通过状态接口读取。
- 扫描参数的运行时收敛规则与 `angle-search/start` 相同。

#### `POST /api/v1/ai/background-lock/unlock`

说明：

- 清空 `ai_lock_mode_enabled`
- 清空 `ai_lock_target_box_norm`
- 将 `ai_lock_fit_score` 重置为 `0.0`

## 5. 推荐联调顺序

推荐首轮联调顺序如下：

1. `POST /api/v1/session/open`
2. `POST /api/v1/templates/import`
3. `GET /api/v1/templates/`
4. `GET /api/v1/status/compose`
5. `POST /api/v1/control/mode?mode=SMART_COMPOSE`
6. `GET /api/v1/status`
7. `POST /api/v1/capture/manual?auto_analyze=true`
8. `POST /api/v1/ai/angle-search/start`
9. 轮询 `GET /api/v1/status/ai`
10. `POST /api/v1/session/close`

## 6. 这份文档特别修正的几个联调易错点

以下结论都来自当前代码，不是历史草案：

- 模板上传接口里的 `name` 是 query 参数，不是 multipart 表单字段。
- 模板列表接口返回的 `created_at` 是字符串时间，不是时间戳。
- 模板 ID 当前是 UUID，不是 `tpl_xxx`。
- 无活动会话时，依赖会话的接口返回 `503`，不是 `400`。
- 模板列表接口实际路径是 `/api/v1/templates/`，建议保留尾斜杠。
- 模板导入成功后会自动选中模板。
- 删除模板时会清空当前选中模板，但不会删除本地图片文件。
