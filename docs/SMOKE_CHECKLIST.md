# 冒烟检查清单

## 🎯 目的

确保重构后项目核心功能正常工作，GUI和API都能正常启动和运行。

补充说明：

- API 采用单活会话模式，测试控制/模板/抓拍/AI接口前，先调用一次 `POST /api/v1/session/open`。
- `AI自动找角度`、`背景扫描锁机位` 为异步真实任务，启动后需要通过 `GET /api/v1/status` 轮询结果。

## 📋 启动验证

### GUI启动测试

- [ ] 能以 `--mock-gimbal` 正常启动
  ```bash
  python main.py --stream-url 0 --mock-gimbal
  ```

- [ ] 能在无AI Key时使用Mock AI启动

- [ ] 能打开视频源并持续刷新画面

- [ ] 能正常关闭窗口，无卡死

### API启动测试

- [ ] 能正常启动API服务
  ```bash
  uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload
  ```

- [ ] Swagger UI 可访问 (http://localhost:8000/docs)

- [ ] 健康检查接口正常
  ```bash
  curl http://localhost:8000/api/v1/health
  ```

## 🎮 模式切换验证

### GUI验证
- [ ] 手动模式可进入并正常工作
- [ ] 自动跟随模式可进入并正常工作
- [ ] 模板引导模式可进入并正常工作
- [ ] 模式间切换流畅无错误

### API验证
- [ ] 能通过API切换模式
  ```bash
  curl -X POST "http://localhost:8000/api/v1/control/mode?mode=AUTO_TRACK"
  ```

## 🎛️ 手动控制验证

### GUI验证
- [ ] `w/a/s/d` 按钮有响应
- [ ] `home` 回中按钮可执行
- [ ] 跟随点切换可执行
- [ ] 速度切换可执行

### API验证
- [ ] 能通过API手动控制
  ```bash
  curl -X POST "http://localhost:8000/api/v1/control/manual-move?action=w"
  ```

- [ ] 能通过API设置跟随模式
  ```bash
  curl -X POST "http://localhost:8000/api/v1/control/follow-mode?follow_mode=face"
  ```

- [ ] 能通过API设置速度模式
  ```bash
  curl -X POST "http://localhost:8000/api/v1/control/speed?speed_mode=slow"
  ```

## 📸 抓拍验证

### GUI验证
- [ ] 手动抓拍能保存文件
- [ ] 模板手势抓拍链路不报错
- [ ] 抓拍后自动分析开关不报错

### API验证
- [ ] 能触发手动抓拍
  ```bash
  curl -X POST "http://localhost:8000/api/v1/capture/manual"
  ```

- [ ] 抓拍响应里能拿到真实 `capture_path`

## 📋 模板验证

### GUI验证
- [ ] 可上传模板
- [ ] 可切换模板
- [ ] 可删除模板
- [ ] 模板叠加可显示

### API验证
- [ ] 能上传模板
  ```bash
  curl -X POST "http://localhost:8000/api/v1/templates/import" \
    -F "file=@template.jpg"
  ```

- [ ] 能查询模板列表
  ```bash
  curl "http://localhost:8000/api/v1/templates/"
  ```

## 🤖 AI功能验证

### GUI验证
- [ ] 上传单图评分不报错
- [ ] 背景分析不报错
- [ ] 自动找角度流程能完整跑完
- [ ] 背景扫描锁机位流程能完整跑完

### API验证
- [ ] 能启动自动找角度
  ```bash
  curl -X POST "http://localhost:8000/api/v1/ai/angle-search/start"
  ```

- [ ] 状态接口能看到 `ai_angle_search_running=true`

- [ ] 能启动背景扫描锁机位
  ```bash
  curl -X POST "http://localhost:8000/api/v1/ai/background-lock/start"
  ```

- [ ] 状态接口最终能看到 `ai_lock_mode_enabled=true`

- [ ] 能解除锁机位
  ```bash
  curl -X POST "http://localhost:8000/api/v1/ai/background-lock/unlock"
  ```

## 📊 状态查询验证

### API验证
- [ ] 能获取完整状态
  ```bash
  curl "http://localhost:8000/api/v1/status"
  ```

- [ ] 能查询特定状态
  ```bash
  curl "http://localhost:8000/api/v1/status/mode"
  ```

## 🔧 技术验证

### 代码结构验证
- [ ] `main.py` 业务耦合降低
- [ ] 服务层代码不依赖Tkinter
- [ ] API层能独立调用服务层
- [ ] 模板仓储接口清晰

### 性能验证
- [ ] 渲染帧率稳定
- [ ] 检测帧率正常
- [ ] 无明显内存泄漏

## 📝 文档验证

- [ ] README包含API使用说明
- [ ] API文档清晰完整
- [ ] 启动说明准确

## 🚨 问题记录

| 问题 | 严重程度 | 状态 | 备注 |
|------|---------|------|------|
|      |         |      |      |

## ✅ 完成标准

- [ ] 所有GUI功能正常
- [ ] 所有API接口可调用
- [ ] 无重大bug
- [ ] 文档完整准确

---

**检查完成后，在对应项打勾 ✓**
