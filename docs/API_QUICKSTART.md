# 联调接口文档（API Quickstart）

## 1. 适用范围

这份文档面向联调方，说明当前版本已经可用的 FastAPI 接口，以及建议的首批调用顺序。

当前文档只描述已经在代码里注册并接到真实 service 的接口，不包含未来规划中的占位接口。

## 2. 当前接口特性

- API 入口：`api.app:app`
- 基础前缀：`/api/v1`
- 当前为单活会话模型
- 大多数接口当前使用 query 参数，而不是 JSON body
- AI 自动找角度和背景锁机位为异步启动，结果需轮询状态接口获取
- 模板上传使用 `multipart/form-data`
- 当前 API 会话默认使用 `MockServoDriver()`，适合联调流程，不默认直连真硬件
- 若未配置 `SILICONFLOW_API_KEY`，AI 会走 Mock AI，但流程本身仍真实执行

## 3. 启动方式

### 3.1 启动 API

```powershell
python -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

### 3.2 Swagger

启动后可访问：

- `http://localhost:8000/docs`
- `http://localhost:8000/redoc`

## 4. 联调前提

联调顺序建议始终遵循下面规则：

1. 先 `open_session`
2. 再调模板、控制、抓拍、AI 接口
3. 最后 `close_session`

如果没有活跃会话，依赖会话的接口会返回 `400`。

## 5. 接口总览

### 5.1 健康检查

`GET /api/v1/health`

示例：

```bash
curl "http://localhost:8000/api/v1/health"
```

返回：

```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### 5.2 会话接口

#### `POST /api/v1/session/open`

作用：

- 创建真实会话上下文
- 初始化视频源、识别器、云台、service
- 启动 API 内部帧循环

参数：

- `stream_url`：必填，视频流地址或可被 OpenCV 打开的输入
- `mirror_view`：可选，默认 `true`
- `start_mode`：可选，默认 `MANUAL`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/session/open?stream_url=rtsp://your-stream-url&mirror_view=true&start_mode=MANUAL"
```

返回：

```json
{
  "session_id": "sess_ab12cd34",
  "status": "running",
  "message": "会话创建成功"
}
```

#### `POST /api/v1/session/close`

作用：

- 关闭当前会话
- 停止帧循环
- 释放识别器、视频源、云台等资源

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/session/close"
```

返回：

```json
{
  "message": "会话已关闭"
}
```

### 5.3 状态接口

#### `GET /api/v1/status`

作用：

- 获取完整状态快照
- 用于轮询 AI 异步任务进度和结果

示例：

```bash
curl "http://localhost:8000/api/v1/status"
```

返回示例：

```json
{
  "session_id": "sess_ab12cd34",
  "mode": "SMART_COMPOSE",
  "follow_mode": "shoulders",
  "speed_mode": "normal",
  "compose_score": 76.4,
  "compose_ready": true,
  "selected_template_id": "tpl_xxx",
  "tracking_stable": true,
  "ai_angle_search_running": false,
  "ai_lock_mode_enabled": true,
  "ai_lock_fit_score": 0.71,
  "ai_lock_target_box_norm": [0.38, 0.18, 0.24, 0.66],
  "latest_capture_path": "captures/2026-04-02/xxx.jpg",
  "latest_capture_error": null,
  "last_angle_search_result": {
    "best_score": 8.6,
    "summary": "构图更稳定",
    "best_pan": 2.0,
    "best_tilt": 0.0,
    "num_scanned": 5,
    "capture_path": "captures/2026-04-02/xxx.jpg"
  },
  "last_angle_search_error": null,
  "last_background_lock_result": null,
  "last_background_lock_error": null
}
```

#### `GET /api/v1/status/mode`

返回当前模式。

#### `GET /api/v1/status/compose`

返回：

- `compose_score`
- `compose_ready`
- `selected_template_id`

#### `GET /api/v1/status/tracking`

返回：

- `tracking_stable`
- `follow_mode`
- `speed_mode`

#### `GET /api/v1/status/ai`

返回：

- `ai_angle_search_running`
- `ai_lock_mode_enabled`
- `ai_lock_fit_score`
- `ai_lock_target_box_norm`
- `last_angle_search_result`
- `last_angle_search_error`
- `last_background_lock_result`
- `last_background_lock_error`

### 5.4 模板接口

#### `POST /api/v1/templates/import`

作用：

- 上传模板图片
- 真正调用 `TemplateService.import_template()`
- 自动选择新模板

请求：

- `multipart/form-data`
- 文件字段名：`file`
- 可选字段：`name`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/templates/import" ^
  -H "Content-Type: multipart/form-data" ^
  -F "file=@C:/path/to/template.jpg" ^
  -F "name=半身模板"
```

返回：

```json
{
  "template_id": "tpl_xxx",
  "name": "半身模板",
  "created": true
}
```

#### `GET /api/v1/templates/`

作用：

- 获取模板列表

返回示例：

```json
[
  {
    "template_id": "tpl_xxx",
    "name": "半身模板",
    "created_at": 1712030000.0,
    "image_path": ".template_library/images/xxx.jpg"
  }
]
```

#### `POST /api/v1/templates/select`

参数：

- `template_id`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/templates/select?template_id=tpl_xxx"
```

返回：

```json
{
  "message": "已选择模板 tpl_xxx",
  "selected_template_id": "tpl_xxx"
}
```

#### `DELETE /api/v1/templates/{template_id}`

示例：

```bash
curl -X DELETE "http://localhost:8000/api/v1/templates/tpl_xxx"
```

### 5.5 控制接口

#### `POST /api/v1/control/mode`

参数：

- `mode`

允许值：

- `MANUAL`
- `AUTO_TRACK`
- `SMART_COMPOSE`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/control/mode?mode=AUTO_TRACK"
```

#### `POST /api/v1/control/manual-move`

两种调用方式：

方式一，动作名：

- `action=w|a|s|d|up|down|left|right`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/control/manual-move?action=left"
```

方式二，相对角度：

- `pan_delta`
- `tilt_delta`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/control/manual-move?pan_delta=-3.0&tilt_delta=0.0"
```

#### `POST /api/v1/control/home`

作用：

- 云台回中

#### `POST /api/v1/control/follow-mode`

参数：

- `follow_mode`

当前支持值以代码为准，常用值：

- `shoulders`
- `face`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/control/follow-mode?follow_mode=shoulders"
```

#### `POST /api/v1/control/speed`

参数：

- `speed_mode`

当前支持值以代码为准，常用值：

- `slow`
- `normal`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/control/speed?speed_mode=normal"
```

### 5.6 抓拍接口

#### `POST /api/v1/capture/manual`

参数：

- `auto_analyze`：可选，默认 `false`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/capture/manual?auto_analyze=true"
```

返回示例：

```json
{
  "message": "手动抓拍已完成",
  "capture_path": "captures/2026-04-02/xxx.jpg",
  "analysis": {
    "score": 8.2,
    "summary": "主体完整，构图稳定",
    "suggestions": ["人物可略向左"]
  },
  "analysis_error": null
}
```

### 5.7 AI 接口

#### `POST /api/v1/ai/angle-search/start`

作用：

- 异步启动自动找角度

参数：

- `pan_range`
- `tilt_range`
- `pan_step`
- `tilt_step`
- `max_candidates`
- `settle_s`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/ai/angle-search/start?pan_range=6.0&tilt_range=3.0&pan_step=4.0&tilt_step=3.0&max_candidates=5&settle_s=0.35"
```

返回：

```json
{
  "message": "AI自动找角度已启动",
  "ai_angle_search_running": true
}
```

结果获取方式：

- 轮询 `GET /api/v1/status`
- 或轮询 `GET /api/v1/status/ai`

完成后读取：

- `last_angle_search_result`
- `last_angle_search_error`

#### `POST /api/v1/ai/background-lock/start`

作用：

- 异步启动背景扫描并进入锁机位模式

参数：

- `pan_range`
- `tilt_range`
- `pan_step`
- `tilt_step`
- `max_candidates`
- `settle_s`
- `delay_s`

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/ai/background-lock/start?pan_range=6.0&tilt_range=3.0&max_candidates=5&delay_s=3.0"
```

返回：

```json
{
  "message": "背景扫描锁机位已启动"
}
```

完成后读取：

- `ai_lock_mode_enabled`
- `ai_lock_target_box_norm`
- `ai_lock_fit_score`
- `last_background_lock_result`
- `last_background_lock_error`

#### `POST /api/v1/ai/background-lock/unlock`

作用：

- 解除锁机位模式

示例：

```bash
curl -X POST "http://localhost:8000/api/v1/ai/background-lock/unlock"
```

返回：

```json
{
  "message": "已解除AI机位锁定"
}
```

## 6. 首批推荐调用顺序

联调方首批建议按下面顺序接：

1. `POST /api/v1/session/open`
2. `POST /api/v1/templates/import`
3. `GET /api/v1/templates/`
4. `POST /api/v1/templates/select`
5. `POST /api/v1/control/mode`
6. `POST /api/v1/control/follow-mode`
7. `POST /api/v1/control/speed`
8. `GET /api/v1/status`
9. `POST /api/v1/capture/manual`
10. `POST /api/v1/ai/angle-search/start`
11. 轮询 `GET /api/v1/status`
12. `POST /api/v1/ai/background-lock/start`
13. 轮询 `GET /api/v1/status`
14. `POST /api/v1/ai/background-lock/unlock`
15. `POST /api/v1/session/close`

## 7. Python 联调示例

```python
import time
import requests

base = "http://localhost:8000"

requests.post(
    f"{base}/api/v1/session/open",
    params={
        "stream_url": "rtsp://your-stream-url",
        "mirror_view": True,
        "start_mode": "MANUAL",
    },
).raise_for_status()

with open("template.jpg", "rb") as f:
    resp = requests.post(
        f"{base}/api/v1/templates/import",
        files={"file": ("template.jpg", f, "image/jpeg")},
    )
    resp.raise_for_status()
    template_id = resp.json()["template_id"]

requests.post(
    f"{base}/api/v1/control/mode",
    params={"mode": "SMART_COMPOSE"},
).raise_for_status()

requests.post(
    f"{base}/api/v1/ai/angle-search/start",
    params={"max_candidates": 5},
).raise_for_status()

while True:
    status = requests.get(f"{base}/api/v1/status").json()
    if not status["ai_angle_search_running"]:
        break
    time.sleep(1.0)

print(status["last_angle_search_result"])

requests.post(f"{base}/api/v1/session/close").raise_for_status()
```

## 8. 错误处理

常见状态码：

- `200`：成功
- `400`：参数错误、无会话、当前状态不允许执行
- `404`：模板不存在等资源缺失

错误返回示例：

```json
{
  "detail": "没有活跃的会话"
}
```

## 9. 当前未提供的接口

下面这些接口当前文档里不应被当作已实现能力：

- 单图上传评分接口
- 单图背景分析接口
- 模板图 + 背景图联合指导接口
- WebSocket 事件推送接口
- 逐帧 push 视频接口

如果后续需要补这些能力，应以新接口文档为准，不应直接按旧草案联调。
