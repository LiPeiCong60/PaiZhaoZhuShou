# 智能云台拍照助手项目文档（2026-04）

## 1. 文档目的

这份文档描述的是当前根目录版本的真实实现，而不是早期的纯规划状态。

本轮工作的目标不是重写项目，而是把上一轮未完成的 service/API 拆分真正接回现有核心链路，达到下面两点：

- GUI 主入口 `main.py` 继续可用
- FastAPI 接口可以拿到真实会话、真实状态和真实服务能力

## 2. 当前版本说明

- 根目录代码树仍然是主版本，后续联调和维护以根目录源码为准
- `main.py` 仍然是 GUI 主入口
- `api/app.py` 是 FastAPI 主入口
- 视觉检测、模板评估、云台控制、AI 协议仍沿用现有项目实现，没有更换算法或替换协议
- 这轮新增的 service/repository/api 结构，是对现有核心类的封装和联调修复，不是独立重写的一套新系统

## 3. 当前关键目录

```text
DaDuoJi/
├─ main.py
├─ tracking_controller.py
├─ gimbal_controller.py
├─ template_compose.py
├─ services/
│  ├─ runtime_state.py
│  ├─ control_service.py
│  ├─ capture_service.py
│  ├─ template_service.py
│  ├─ ai_orchestrator.py
│  └─ status_service.py
├─ repositories/
│  ├─ template_repository.py
│  └─ local_template_repository.py
├─ api/
│  ├─ app.py
│  ├─ dependencies.py
│  ├─ session_manager.py
│  └─ routes/
│     ├─ session.py
│     ├─ control.py
│     ├─ capture.py
│     ├─ template.py
│     ├─ ai.py
│     └─ status.py
├─ docs/
│  ├─ PROJECT_DOCUMENTATION.md
│  ├─ REFACTOR_TASKS.md
│  ├─ INTEGRATION_API_PLAN.md
│  ├─ API_QUICKSTART.md
│  └─ SMOKE_CHECKLIST.md
└─ .template_library/
```

## 4. 当前运行时架构

当前系统已经形成两条入口、同一套核心能力的结构：

- GUI 入口：`main.py`
- API 入口：`api/app.py`

两条入口都复用根目录现有核心类：

- 视频源：`OpenCVVideoSource`
- 识别器：`MediaPipeYoloVisionDetector` + `AsyncDetector`
- 跟随控制：`TrackingController`
- 云台控制：`GimbalController`
- 模板构图：`TemplateComposeEngine`
- AI 接口：`build_ai_assistant_from_env()`

在这些核心类之上，这轮新增了一层 service 封装和共享状态：

- `RuntimeState` 负责统一运行时状态
- `ControlService` 负责模式、跟随模式、速度模式、手动控制、回中
- `CaptureService` 负责抓拍和抓拍后的 AI 分析返回
- `TemplateService` 负责模板导入、查询、选择、删除
- `AIOrchestrator` 负责自动找角度和背景扫描锁机位
- `StatusService` 负责聚合对外状态

这意味着 GUI 和 API 不再各自维护一套独立逻辑，而是共享同一套状态契约和 service 能力。

## 5. 单一状态源：`services/runtime_state.py`

`RuntimeState` 是当前 GUI、service、API 之间的单一状态源，主要字段如下：

- `follow_mode`
- `speed_mode`
- `selected_template_id`
- `reliable_detection_streak`
- `last_compose_feedback`
- `ready_since_ts`
- `latest_frame`
- `latest_vision`
- `stable_detection`
- `latest_capture_path`
- `latest_capture_analysis`
- `latest_capture_error`
- `ai_angle_search_running`
- `ai_lock_mode_enabled`
- `ai_lock_target_box_norm`
- `ai_lock_fit_score`

本轮明确统一了以下原本容易分裂的状态来源：

- 跟随模式 `follow_mode`
- 速度模式 `speed_mode`
- 当前模板 `selected_template_id`
- AI 自动找角度运行状态
- AI 锁机位状态
- AI 锁机位 fit score

GUI 侧通过 `main.py` 中的属性代理继续兼容旧字段访问；API 侧通过 `StatusService` 读取同一份状态；AI 侧通过 `AIOrchestrator` 写回同一份状态。

## 6. Service 层真实契约

### 6.1 `ControlService`

`ControlService` 现在已经修正为基于项目真实类接口工作，不再调用不存在的成员。

本轮修复点：

- 不再依赖不存在的 `TrackingController.follow_mode`
- 不再依赖不存在的 `TrackingController.speed_mode`
- 不再依赖不存在的 `TrackingController.set_follow_mode()`
- 不再依赖不存在的 `GimbalController.manual_move()`

当前真实契约：

- `follow_mode` 保存在 `RuntimeState`
- `speed_mode` 保存在 `RuntimeState`，并通过 `TrackingController.set_speed_mode()` 同步到跟随控制器
- `manual_move()` 通过 `GimbalController.move_relative()` 执行上下左右移动
- `home()` 通过 `GimbalController.home()` 回中

### 6.2 `CaptureService`

`CaptureService` 现在不再吞掉抓拍后的关键结果。

当前行为：

- 调用 `CaptureTrigger` 完成真实抓拍
- 如果启用 `auto_analyze=True`，会调用 `AIPhotoAssistant.analyze_capture()`
- 返回 `CaptureResult(path, analysis, analysis_error)`
- 同步更新 `RuntimeState.latest_capture_path`
- 同步更新 `RuntimeState.latest_capture_analysis`
- 同步更新 `RuntimeState.latest_capture_error`

这保证了 GUI 和 API 都能拿到同一份抓拍结果，而不是只有落盘、没有分析结果回传。

### 6.3 `TemplateService`

`TemplateService` 已经不再直接依赖 `TemplateLibrary`，而是依赖 `TemplateRepository` 抽象接口。

当前结构：

- 仓储接口：`repositories/template_repository.py`
- 本地实现：`repositories/local_template_repository.py`
- GUI 和 API 当前都接的是 `LocalTemplateRepository`

导入模板时的真实流程：

1. 读取上传图像
2. 调用检测器识别人像
3. 生成 `TemplateProfile`
4. 通过 `TemplateRepository.add()` 持久化模板元数据
5. 把源图片复制到 `.template_library/images/`

这样做的原因是 API 上传常用临时文件，如果不复制到模板目录，模板会指向已经删除的临时文件。

### 6.4 `AIOrchestrator`

`AIOrchestrator` 现在负责两条真实 AI 链路：

- 自动找角度 `start_angle_search()`
- 背景扫描锁机位 `start_background_lock()`

本轮修复重点：

- 不再只改内部私有状态，而是统一写回 `RuntimeState`
- 不再拿一张旧帧假装完成整轮扫描，而是通过 `frame_provider` 在每个扫描点重新取当前画面
- 背景锁机位的目标框、启用状态、fit score 都统一写入共享状态

### 6.5 `StatusService`

`StatusService` 现在是状态聚合出口，而不是各模块零散字段的临时拼装。

当前对外聚合的核心字段包括：

- 当前模式
- `follow_mode`
- `speed_mode`
- 模板得分和 ready 状态
- 当前模板 ID
- 跟踪是否稳定
- AI 自动找角度是否运行中
- AI 锁机位是否启用
- AI fit score
- AI 目标框
- 最近一次抓拍路径
- 最近一次抓拍错误

## 7. GUI 与 AI 联动修复点

`main.py` 仍然保留 GUI 主入口，但现在已经接到共享状态和 service 层，而不是继续读写完全分裂的旧字段。

当前已经完成的联动修复：

- GUI 锁定框读取共享的 `ai_lock_target_box_norm`
- GUI 状态栏读取共享的模式、模板和 AI 状态
- 锁机位开启时，会正确抑制自动跟随
- AI 搜索运行状态显示来自 `RuntimeState.ai_angle_search_running`
- 模板选择、清空、删除走 `TemplateService`
- 手动抓拍后，GUI 可以继续拿到 `CaptureResult` 中的分析结果

需要注意：

- GUI 这轮完成的是状态和 service 联通修复
- 还没有做完整的人工交互回归，后续仍建议按 `docs/SMOKE_CHECKLIST.md` 做一轮手工验证

## 8. API 会话模型

API 当前采用单活会话模型，由 `api/session_manager.py` 管理。

### 8.1 `open_session`

`POST /api/v1/session/open` 会创建一份真实会话上下文 `ApiSessionContext`，其中包含：

- `OpenCVVideoSource`
- `MediaPipeYoloVisionDetector`
- `AsyncDetector`
- `TrackingController`
- `ModeManager`
- `LocalFileCaptureTrigger`
- `AIPhotoAssistant`
- `GimbalController`
- `ControlService`
- `CaptureService`
- `TemplateService`
- `AIOrchestrator`
- `StatusService`

API 会话不是“路由里临时 new 一次就丢掉”的一次性对象，后续所有接口都复用这份上下文。

当前需要额外说明一点：

- API 会话里当前默认使用的是 `MockServoDriver()`
- 也就是说 API 已经接通了真实控制链路和状态链路，但默认不会直接驱动真硬件云台
- 如果后续要让 API 直接控制真硬件，需要在 `ApiSessionContext` 上再补一层明确的硬件配置入口

### 8.2 后台帧循环

`ApiSessionContext` 内部有独立的帧循环，用于在不启动 GUI 的情况下维持实时状态：

- 持续读取视频源
- 提交检测任务
- 读取最新视觉结果
- 更新 `stable_detection`
- 更新 `reliable_detection_streak`
- 在模板模式下更新 `last_compose_feedback`
- 在自动跟随允许时执行云台控制
- 在锁机位模式下更新 `ai_lock_fit_score`

这保证了 API 单独运行时也能看到真实实时状态，而不是只有 GUI 模式下才有效。

### 8.3 `close_session`

`POST /api/v1/session/close` 会：

- 停止后台帧循环
- 等待 AI 任务线程结束
- 关闭 `AsyncDetector`
- 关闭视频源
- 关闭云台控制器

## 9. API 路由结构

当前 API 已统一为 `api/app.py + api/routes/*` 结构，所有需要路由都已经注册到 FastAPI。

### 9.1 会话路由

- `POST /api/v1/session/open`
- `POST /api/v1/session/close`

### 9.2 控制路由

- `POST /api/v1/control/mode`
- `POST /api/v1/control/manual-move`
- `POST /api/v1/control/home`
- `POST /api/v1/control/follow-mode`
- `POST /api/v1/control/speed`

### 9.3 抓拍路由

- `POST /api/v1/capture/manual`

### 9.4 模板路由

- `POST /api/v1/templates/import`
- `GET /api/v1/templates/`
- `POST /api/v1/templates/select`
- `DELETE /api/v1/templates/{template_id}`

### 9.5 AI 路由

- `POST /api/v1/ai/angle-search/start`
- `POST /api/v1/ai/background-lock/start`
- `POST /api/v1/ai/background-lock/unlock`

### 9.6 状态路由

- `GET /api/v1/status`
- `GET /api/v1/status/mode`
- `GET /api/v1/status/compose`
- `GET /api/v1/status/tracking`
- `GET /api/v1/status/ai`

## 10. 启动方式

### 10.1 启动 GUI

```powershell
python main.py --stream-url 0 --mock-gimbal
```

### 10.2 启动 API

```powershell
python -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

依赖至少需要包含：

- `fastapi`
- `uvicorn`
- `python-multipart`

## 11. 已完成的最小验证

本轮已完成的最小验证包括：

- `python -m compileall main.py services api repositories`
- GUI 能启动且不会立即崩溃
- API 能启动并返回健康检查
- `GET /api/v1/status` 可用
- 模板导入、列表、选择、删除可用
- 手动抓拍接口可用
- AI 自动找角度接口能真实启动流程并产出结果
- AI 背景锁机位接口能真实启动流程并写回状态

本轮最小验证使用的是开发环境常见条件：

- 视频源可使用静态样例图或测试流
- API 会话默认使用 mock 云台驱动
- 若未配置 `SILICONFLOW_API_KEY`，AI 流程会走 Mock AI，但扫描、抓拍、状态写回链路仍是真实执行

## 12. 当前仍未闭环的事项

当前文档、代码和最小联调链路已经对齐，但下面几项仍建议后续继续补齐：

- GUI 的完整人工回归还没做完
- 真实直播流环境还没做一轮长时间验证
- 真实 SiliconFlow Key 环境还没做一轮完整实测
- 自动化测试仍为空，当前验证仍以 compileall 和 smoke 为主

## 13. 相关文档

- `docs/API_QUICKSTART.md`
  面向联调方的接口快速上手说明
- `docs/INTEGRATION_API_PLAN.md`
  面向联调改造的结构说明
- `docs/SMOKE_CHECKLIST.md`
  当前建议执行的最小冒烟检查项
- `docs/REFACTOR_TASKS.md`
  本轮“上一轮不完整联调重构修复”任务状态
