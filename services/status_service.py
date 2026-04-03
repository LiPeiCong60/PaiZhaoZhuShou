"""
状态服务模块
负责系统状态查询和聚合
"""

from __future__ import annotations

import time
from typing import Any, Dict

from mode_manager import ControlMode
from services.runtime_state import RuntimeState


class StatusService:
    """状态服务类，提供系统状态查询"""

    def __init__(
        self,
        mode_manager,
        runtime_state: RuntimeState,
    ) -> None:
        self._mode_manager = mode_manager
        self._runtime_state = runtime_state

    def update_detection_streak(self, stable_detection) -> None:
        """兼容旧调用：更新检测连续计数"""
        self._runtime_state.reliable_detection_streak = (
            0 if stable_detection is None else self._runtime_state.reliable_detection_streak + 1
        )
        self._runtime_state.stable_detection = stable_detection

    def update_compose_feedback(self, feedback) -> None:
        """兼容旧调用：更新模板反馈"""
        self._runtime_state.last_compose_feedback = feedback
        if feedback and feedback.ready:
            if self._runtime_state.ready_since_ts <= 0:
                self._runtime_state.ready_since_ts = time.time()
        else:
            self._runtime_state.ready_since_ts = 0.0

    def update_capture_path(self, path: str | None) -> None:
        """兼容旧调用：更新抓拍路径"""
        self._runtime_state.latest_capture_path = path

    def get_status(self) -> Dict[str, Any]:
        """获取完整系统状态"""
        compose_feedback = self._runtime_state.last_compose_feedback
        compose_score = compose_feedback.total_score if compose_feedback else 0.0
        compose_ready = compose_feedback.ready if compose_feedback else False

        return {
            "mode": self._mode_manager.mode.value if self._mode_manager.mode else ControlMode.MANUAL.value,
            "follow_mode": self._runtime_state.follow_mode,
            "speed_mode": self._runtime_state.speed_mode,
            "compose_score": compose_score,
            "compose_ready": compose_ready,
            "selected_template_id": self._runtime_state.selected_template_id,
            "tracking_stable": self._runtime_state.reliable_detection_streak >= 3,
            "ai_angle_search_running": self._runtime_state.ai_angle_search_running,
            "ai_lock_mode_enabled": self._runtime_state.ai_lock_mode_enabled,
            "ai_lock_fit_score": self._runtime_state.ai_lock_fit_score,
            "ai_lock_target_box_norm": self._runtime_state.ai_lock_target_box_norm,
            "latest_capture_path": self._runtime_state.latest_capture_path,
            "latest_capture_error": self._runtime_state.latest_capture_error,
        }
