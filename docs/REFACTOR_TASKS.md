# 上一轮不完整联调重构修复任务状态（2026-04-02）

## 1. 文档目的

这份文档用于说明“上一轮不完整联调重构修复”目前已经做到哪里、哪些问题已经解除阻塞、哪些事项仍需要继续验证。

这不是新的重构规划，而是对当前代码实际状态的阶段性结论。

## 2. 本轮目标

本轮目标有两个：

- 不破坏根目录现有 GUI 主入口
- 让 API 能拿到真实会话、真实状态和真实服务，而不是占位接口

## 3. 已完成的核心修复

### 3.1 Service 契约修复

`ControlService` 和 `StatusService` 已经从“引用不存在字段”修正为“基于真实核心类接口工作”。

已修复的问题：

- 去掉了对不存在的 `TrackingController.follow_mode` 的依赖
- 去掉了对不存在的 `TrackingController.speed_mode` 的依赖
- 去掉了对不存在的 `TrackingController.set_follow_mode()` 的依赖
- 去掉了对不存在的 `GimbalController.manual_move()` 的依赖

当前实现：

- `follow_mode` 由 `RuntimeState` 统一保存
- `speed_mode` 由 `RuntimeState` 保存，并同步到 `TrackingController.set_speed_mode()`
- 手动控制通过 `GimbalController.move_relative()` 实现

### 3.2 GUI 与 service 状态统一

本轮已经把下面这些状态统一到 `services/runtime_state.py`：

- `follow_mode`
- `speed_mode`
- `selected_template_id`
- AI 自动找角度运行状态
- AI 锁机位状态
- AI lock fit score

结果是：

- GUI 不再维护一套，service 再维护一套
- API 查询到的是 GUI 和 AI 真正在使用的状态

### 3.3 `main.py` 与 `AIOrchestrator` 联动修复

`main.py` 已经不再只读旧字段，而是通过共享状态和 service 层和 `AIOrchestrator` 同步。

已修复：

- GUI 锁定框可读到真实锁机位框
- 状态栏可读到真实 AI 状态
- 锁机位模式会抑制自动跟随
- AI 搜索状态显示真实运行状态

### 3.4 模板仓储真正接入

`TemplateService` 已经改为依赖 `TemplateRepository`，不再直接依赖 `TemplateLibrary`。

当前结构：

- 抽象接口：`repositories/template_repository.py`
- 本地实现：`repositories/local_template_repository.py`
- 当前落地实现：`LocalTemplateRepository(TemplateLibrary)`

模板上传现在会真实调用 `TemplateService.import_template()`，不再返回伪造模板 ID。

另外，上传图片会复制到 `.template_library/images/`，避免模板指向已删除的临时文件。

### 3.5 API 结构已统一

API 已统一为下面这套结构：

- 入口：`api/app.py`
- 会话管理：`api/session_manager.py`
- 路由：`api/routes/*`

已注册的路由包括：

- 会话
- 控制
- 抓拍
- 模板
- AI
- 状态

不存在“路由文件写了但没有注册到 FastAPI”的情况。

### 3.6 真实 API 会话上下文

`open_session` 现在会创建真实 `ApiSessionContext`，后续接口会复用这份上下文。

会话中包含：

- 视频源
- 识别器
- 异步识别器
- 跟随控制器
- 云台控制器
- mode manager
- capture trigger
- AI assistant
- 所有 service

`close_session` 会做真实资源释放，而不是结束一个局部变量作用域就算完成。

当前限制也需要明确记录：

- API 会话默认使用 `MockServoDriver()`
- 因此 API 已经适合首轮联调和流程联通，但还不是“开箱即接真云台硬件”的最终形态

### 3.7 真接口已接通

下面这些接口已经接入真实 service，而不是占位返回：

- `POST /api/v1/session/open`
- `POST /api/v1/session/close`
- `GET /api/v1/status`
- 模板导入 / 查询 / 选择 / 删除
- 模式切换
- 手动控制
- 回中
- 手动抓拍
- `POST /api/v1/ai/angle-search/start`
- `POST /api/v1/ai/background-lock/start`
- `POST /api/v1/ai/background-lock/unlock`

### 3.8 抓拍与 AI 分析链路修复

`CaptureService` 现在会返回 `CaptureResult`，不再吞掉关键结果。

这意味着：

- GUI 手动抓拍还能保持原有体验
- API 抓拍接口可以返回真实 `capture_path`
- 如果启用自动分析，也能返回 `analysis` 或 `analysis_error`

### 3.9 依赖补齐

`requirements.txt` 已补齐 API 所需核心依赖：

- `fastapi`
- `uvicorn`
- `python-multipart`

## 4. 最小验证结果

### 4.1 已实际完成的验证

- `python -m compileall main.py services api repositories`
- GUI 启动验证
- API 启动验证
- 健康检查验证
- 状态接口验证
- 模板接口验证
- 手动抓拍接口验证
- AI 自动找角度接口验证
- AI 背景锁机位接口验证

### 4.2 验证方式说明

当前做的是“最小联调验证”，主要覆盖：

- 代码可导入、可启动
- API 路由能真实打通 service
- 状态字段能真实变化
- AI 任务不是纯占位返回

当前验证环境默认前提：

- API 会话使用 mock 云台驱动
- 视频源可使用测试流或样例图
- 未配置真实 AI Key 时走 Mock AI

## 5. 当前还没有完全闭环的事项

虽然主阻塞已经解除，但严格按“全部验收完成”来讲，下面几项还没有完全关掉：

### 5.1 GUI 完整人工回归未完成

目前完成的是 GUI 可启动和核心状态链路接通，尚未完成完整人工回归，例如：

- 全部按钮逐个点击验证
- 模板模式下的完整构图回归
- 锁机位叠框的长流程观察
- 长时间运行稳定性观察

### 5.2 真实直播流环境未完整验证

当前最小联调已覆盖接口流程，但还没有用真实直播流做一轮长时间验证。

### 5.3 真实 AI Key 环境未完整验证

如果环境中没有真实 `SILICONFLOW_API_KEY`，当前流程会走 Mock AI。

这意味着：

- 链路本身已经接通
- 但真实线上 AI 响应质量和异常处理，还需要在有 Key 的环境再跑一次

### 5.4 自动化测试仍待补齐

目前仍以手工 smoke 和最小脚本验证为主，缺少固定化自动测试。

## 6. 当前建议的结论

当前代码状态可以定性为：

- 主要联调阻塞问题已修复
- GUI 和 API 已经共享同一套状态和 service
- API 已具备首轮联调条件
- 但仍建议在真实流、真实 AI、完整 GUI 回归三方面补一轮验证

## 7. 后续建议

下面这些建议是后续优化项，本轮先不做：

- 增加 GUI 和 API 的自动化 smoke 测试
- 给 API 会话增加更明确的硬件配置参数
- 为 AI 后台任务增加取消和超时治理
- 继续收敛 `main.py` 内部体量，但不改变当前接口能力

## 8. 对应文档

- `docs/PROJECT_DOCUMENTATION.md`
  当前系统结构与真实运行方式
- `docs/API_QUICKSTART.md`
  联调方快速调用说明
- `docs/INTEGRATION_API_PLAN.md`
  API 化接入方案说明
- `docs/SMOKE_CHECKLIST.md`
  最小验证清单
