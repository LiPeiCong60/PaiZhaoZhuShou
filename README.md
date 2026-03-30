# 智能云台拍照助手 (DaDuoJi)

基于 Python 的智能云台拍摄助手——通过人体检测、姿态对齐和手势触发，实现 **构图跟随 + 模板引导 + 自动抓拍**。
基于先进的视觉大模型（如 SiliconFlow），实现真正的 AI 智能拍照引导、一键构图评分、机位自动筛选与最佳背景分析。

## 特色功能

- **实时云台追踪**: 自动追踪人物肩部/面部，支持平滑防抖算法和多种速度控制。
- **AI 模板联动引导**: 上传示范照片，系统通过骨架与画面比例对比，智能引导被摄者复刻最佳姿态。
- **全自动化智能抓拍**: 张开到握拳的手势触发、单手 OK 强制拍照，结合达标自动触发设定，精准定格瞬间。
- **云端大模型赋能**:
  - **批量选优与评分**: AI 极速批量分析生成的抓图，多视角、多姿势一键锁定最佳画面。
  - **环境扫描与自动锁机位**: 云台自动扫描周围环境，AI 获取现场所有候选背景后输出最佳机位与站位建议，并自动转动到最佳角度。
- **全 GUI 界面交互**: 彻底剔除复杂 CLI，所有参数（跟随速度、阈值调节、扫描范围等）均可通过滚动面板灵活调节。

## 快速开始

```powershell
# 1. 建立虚拟环境并激活
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 安装必要依赖库 (需事先准备 requirements)
pip install -r requirements.txt

# 3. 正常连接硬件启动 (连接手机IP摄像头和云台串口)
python main.py --stream-url "http://192.168.1.6:4747/video" --bus-serial-port COM10 --bus-baudrate 115200

# 若手头没有硬件云台，可使用模拟云台模式和电脑本地摄像头预览
python main.py --stream-url 0 --mock-gimbal
```

## 配置大模型能力 (强烈推荐)

系统深度集成了视觉大模型能力，推荐配置环境变量以激活：
```powershell
$env:SILICONFLOW_API_KEY="your-api-key"
# 推荐使用性能和识别准确度优秀的模型：
$env:SILICONFLOW_MODEL="Pro/OpenGVLab/InternVL2.5-73B" 
```

## 详细文档目录

- [一键上手：用户使用指南](docs/USER_GUIDE.md)
- [架构解析：项目开发技术文档](docs/PROJECT_DOCUMENTATION.md)
