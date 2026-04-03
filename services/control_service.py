"""
控制服务模块
负责处理云台控制命令、模式切换等基础控制功能
"""

from typing import Callable, Optional

from gimbal_controller import GimbalController
from mode_manager import ControlMode, ModeManager
from tracking_controller import TrackingController
from services.runtime_state import RuntimeState
from utils.ui_text import FOLLOW_TEXT, SPEED_TEXT


class ControlService:
    """控制服务类，封装所有控制相关逻辑"""

    def __init__(
        self,
        mode_manager: ModeManager,
        tracking: TrackingController,
        gimbal: GimbalController,
        runtime_state: RuntimeState,
        manual_step_deg: float = 2.0,
    ) -> None:
        self._mode_manager = mode_manager
        self._tracking = tracking
        self._gimbal = gimbal
        self._runtime_state = runtime_state
        self._manual_step_deg = manual_step_deg

    def execute_command(
        self,
        command: str,
        *,
        notify: Optional[Callable[[str], None]] = None,
        set_follow_mode: Optional[Callable[[str], None]] = None,
        set_speed_mode: Optional[Callable[[str], None]] = None,
        stop_event=None,
    ) -> None:
        """执行控制命令"""
        if notify:
            notify(f"> {command}")

        if command.strip().lower() == "capture":
            # 抓拍命令由专门的 capture service 处理
            return

        # 将命令处理委托给 app_core 的 process_command
        from app_core import process_command
        try:
            process_command(
                command,
                mode_manager=self._mode_manager,
                tracking=self._tracking,
                gimbal=self._gimbal,
                capture_trigger=None,  # 由 capture service 处理
                manual_step_deg=self._manual_step_deg,
                stop_event=stop_event,
                notify=notify or (lambda x: None),
                set_follow_mode=set_follow_mode or self.set_follow_mode,
                set_speed_mode=set_speed_mode or self.set_speed_mode,
            )
        except Exception as exc:
            if notify:
                notify(f"命令执行失败: {exc}")

    def get_mode(self) -> ControlMode:
        """获取当前模式"""
        return self._mode_manager.mode

    def set_mode(self, mode: ControlMode) -> None:
        """设置模式"""
        self._mode_manager.set_mode(mode)

    def get_follow_mode(self) -> str:
        """获取跟随模式"""
        return self._runtime_state.follow_mode

    def set_follow_mode(self, mode: str) -> None:
        """设置跟随模式"""
        if mode not in FOLLOW_TEXT:
            raise ValueError(f"不支持的跟随模式: {mode}")
        self._runtime_state.follow_mode = mode

    def get_speed_mode(self) -> str:
        """获取速度模式"""
        return self._runtime_state.speed_mode

    def set_speed_mode(self, mode: str) -> None:
        """设置速度模式"""
        if mode not in SPEED_TEXT:
            raise ValueError(f"不支持的速度模式: {mode}")
        self._runtime_state.speed_mode = mode
        self._tracking.set_speed_mode(mode)

    def manual_move(self, action: str) -> None:
        """手动移动云台"""
        normalized = action.strip().lower()
        if normalized in {"w", "up"}:
            self._gimbal.move_relative(0.0, self._manual_step_deg)
            return
        if normalized in {"s", "down"}:
            self._gimbal.move_relative(0.0, -self._manual_step_deg)
            return
        if normalized in {"a", "left"}:
            self._gimbal.move_relative(-self._manual_step_deg, 0.0)
            return
        if normalized in {"d", "right"}:
            self._gimbal.move_relative(self._manual_step_deg, 0.0)
            return
        raise ValueError(f"不支持的手动控制动作: {action}")

    def move_relative(self, pan_delta: float, tilt_delta: float, smooth: bool = True) -> None:
        """相对移动"""
        self._gimbal.move_relative(pan_delta, tilt_delta, smooth)

    def set_absolute(self, pan: float, tilt: float, smooth: bool = True) -> None:
        """绝对定位"""
        self._gimbal.set_absolute(pan, tilt, smooth)

    def home(self) -> None:
        """回中"""
        self._gimbal.home()

    def get_current_angles(self, prefer_feedback: bool = True) -> tuple[float, float]:
        """获取当前角度"""
        return self._gimbal.get_current_angles(prefer_feedback=prefer_feedback)
