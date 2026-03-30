# 智能云台拍照助手 — 项目技术架构文档

## 1. 项目概览

本项目是基于 Python 的全 GUI 化智能云台拍摄辅助系统，通过视觉反馈实现以下核心能力的跨平台结合：

- **深度实时视频识别**：接入外置 USB 摄像头或手机 IP 实时视频流串接，使用融合了 MediaPipe 和 YOLO 的追踪系统探测人物与姿态。
- **动态云台闭环跟随**：实时指令集输出到串口（硬件级）或模拟器，实现追踪锁定、防抖云台联动。
- **模板指导评分构架**：保存姿态和画幅构图锚点比例，支持相似骨架识别。
- **系统高集成手势触发**：张合手、强制 OK 手势拍照，提供极佳单体互动倒计时抓拍体验。
- **SiliconFlow API 大模型赋能**：实现深度场景环境结合分析：
  - **基于 AI 的一次批量化选优（角度搜寻与背景机位判定）**
  - **基于 "Detail: Low" 和定制 `max_tokens` 强约束条件的安全防错云端交互，具备完整网络容灾能力**
- **全要素落盘与后缀标记系统**：记录所有重要关键数据，生成完整摄影资料集。

## 2. 工程目录结构说明

```
├── main.py                  # Tkinter UI 主程序编排：视频源轮询、UI构建与事件钩子、云台与 AI 批量命令分发
├── app_core.py              # 共享引擎核心：动作解析过滤置信度算法和目标多路优选算法
├── config.py                # System / Runtime 基础配置参数载体
├── detector.py              # 高鲁棒性双驱探测器：MediaPipe(追踪基础+精确定位) & YOLO(大面积追踪补充)
├── template_compose.py      # ComposeFeedback反馈和 Template 相似性打分引擎、拍照动态手势状态机
├── tracking_controller.py   # Tracker 控制环：利用缓冲指数平滑算法 (EMA) 转化偏移距至舵机转动限值内
├── gimbal_controller.py     # 驱动代理工厂：提供 TTL 总线/PCA9685/Mock 三端云台硬件解耦驱动
├── video_source.py          # 基于 OpenCV 的双端缓存视频采集模块及断流自动重启
├── mode_manager.py          # 控制模式枚举系统 (Manual, Auto_Track, Smart_Compose)
├── ui/
│   └── cn_text.py           # 中英多国语字符 PIL-cv2 混构内存池模块
├── utils/
│   ├── common_types.py      # Point/BBox/Detection 复合基础声明，严格强校验
│   ├── overlay_renderer.py  # GUI视觉框体与透明多图层融合渲染工厂
│   └── ui_text.py           # 固定键值对状态中文反解
├── interfaces/
│   ├── ai_assistant.py      # 【大模型大脑层】SiliconFlowAIPhotoAssistant 具体实施：批量发送候选图片列至服务器分析及强校验JSON转换
│   ├── capture_trigger.py   # 【磁盘写入代理】处理保存与自动附加智能拍照说明后缀（如 _AI分析最佳结果）
│   └── target_strategy.py   # Follow anchor 位置偏移点提取
├── docs/                    # 【项目技术文档说明目录】
├── captures/                # 【落盘结果输出集合】：系统强制按日（YYYY-MM-DD）分配文件夹
└── .template_library/       # 模板库元数据结构
```

## 3. 工作流与通讯架构

```mermaid
graph TD
    A[OpenCV_Worker] -->|拉取源画面| B[AsyncDetector (多模型处理层)]
    B -->|合并与容差| C[TargetSelector]
    C -->|分析定位框| D{工作模式层管理器}
    D -->|模式: Auto| E[Tracking Controller \n 偏移计算与防抖动]
    D -->|模式: Compose| F[Compose Engine \n 手势状态评估与动作反馈]
    D -->|模式: AI扫描搜索| G[云台主动搜集图像列表]
    
    G -->|全量候选数组| H[SiliconFlow Batch Analyst \n 联合多背景/角度挑选]
    
    E -->|度数增量| I[Gimbal 驱动代理]
    H -->|JSON结果| J[Gimbal锁定位与抓拍执行]
    F -->|事件激活| K[Capture Trigger \n 追加后缀处理]
    
    K -.->|图片绝对路径| L[SiliconFlow 评分层异步队列]
    L ==>|UI更新与排版| M[Tkinter 主系统]
```

### 多线程安全通信

本项目彻底放弃繁杂 CLI 环境和死锁隐患。通过建立规范队列以响应交互性能请求：

| 数据流 | 线程机制 |
|--------|----------|
| Tkinter 主界面更新循环 | 单一主线程安全托管，`self._root.after()` 防碰撞。所有检测更新与图像拼装限频执行。 |
| OpenCV视频流采集 | 后台 `_reader_worker` 无锁缓存共享循环。 |
| 高计算双探测器(Yolo/Mp) | 内聚化 Threading 子队列处理识别，输出 Result 打包引用，并抛弃失效帧防积压。 |
| AI API 调用及延迟阻塞 | `.run_in_bg` 构建专门守护线程发送 request 并捕获任意异常网络，最终以 callback 安全反写回 UI Log。 |

## 4. 重点设计说明

### 4.1. SiliconFlow AI 分析接口体系优化 (`ai_assistant.py`)
系统利用 `interfaces/ai_assistant.py` 完全接管所有对外通信。最新设计包含了：
1. **防止幻觉与请求污染**：将每次 HTTP Request 物件和 Exception Object 进行循环隔离。
2. **极速推理（Token和网络开销极速优化）**：固定 Payload 中加入 `"detail": "low"` 及 `"max_tokens": 250~400`。
3. **强大的并行批处理 API (`pick_best_from_batch` 和 `pick_best_background_from_batch`)**：
   - 使用包含多图片的组合式 Prompt，实现 9张图片甚至更多组合一次性上传 AI 服务器进行并行计算选取。
4. **稳定数据清洗反解**：定制化了强大的 `_extract_json_obj` 以应对模型带有废话的情况，强制反构 `BatchPickResult` 等对象类型返回。

### 4.2. 云台自动操作锁控机制 (`main.py` Batch Logic)
由于手动操作费时费力，引入以下完整主动行为控制逻辑：
- `_run_batch_angle_search`: 在执行后主动接管云台，利用设置好的平摇步长，等待步进时间稳定后存储每个镜头的候选集。随后统一打包，通过调用 API 分析完成后，云台**回退定位到该最佳候选集方位**，最后调用封装了“照片说明后缀”的抓拍进行结果落地。
- `_run_batch_background_scan`: 用于执行最佳背景锁定机位。此过程包含倒计时规避摄影师机制（让系统记录全量纯净背景帧）并完成锁定供模特配合。

### 4.3. 多驱动的设备支持 (`gimbal_controller.py`)
- 高级隔离硬件： `ServoDriver` 抽象类保证了不限于总线舵机 TTL (`TTLBusSerialDriver`) 或者板卡 PWM (`RaspberryPiPWMDriver`) 可以随时注入主 Controller，且附带完全隔离的 `MockServoDriver` 方便进行系统算法独立测试。

### 4.4 模板系统算法 (`template_compose.py`)
评分完全定制化：
- **姿态与关节吻合占 70%** (采用角度差分运算)。
- **位置长宽比例构画占 30%**。
- 支持倒计时容错机制和强制取消机制（张开手保持时间过短自动剔除等安全验证）。

## 5. 项目核心配置说明

| UI 参数滑轨或项 | 对应底层结构 | 技术提示与性能阈值 |
|-----------------|--------------|--------------------|
| 扫描候选数量 | `max_candidates` (2~9) | 多于10会导致 Token 剧烈膨胀及 API 拒收；最佳设置为 5左右以权衡出图数量。 |
| 显示覆盖叠加度 | `live_overlay_alpha` | CV2的 `addWeighted` 操作。在嵌入版上，过度复杂的渲染会轻微引发掉帧。 |
| 手势验证保留期 / 缓冲帧 | `gesture_stable_frames` | 抑制探测器在握拳瞬间产生的帧丢失识别突跳，通过队列过滤实现。 |
| `max_side`压缩比 | AI Payload Resize | `_encode_image_as_data_url` 函数最高限制 720px 高宽及 `quality=70` 大幅减少流量。 |

## 6. 后续维护预留

因目前的架构实现了底层 API 的无状态请求解耦。若未来 SiliconFlow 增加高阶函数调用 (Function Calling) 甚至视频端点直接交互分析，仅需要替换或增加在 `ai_assistant.py` 内部的 `_chat` 装载模块。若有需要扩展其它检测后端如 RT-DETR 等，仅需继承并重写 `detector.py`。
