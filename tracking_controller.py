from __future__ import annotations

import time
from math import hypot

from config import TrackingConfig
from interfaces.target_strategy import TargetStrategy
from utils.common_types import DetectionResult, GimbalCommand


class TrackingController:
    """Converts detection offsets to stable gimbal delta commands."""

    def __init__(self, config: TrackingConfig, target_strategy: TargetStrategy) -> None:
        self._config = config
        self._target_strategy = target_strategy
        self._off_center_frames = 0
        self._last_command_ts = 0.0
        self._last_anchor: tuple[float, float] | None = None
        self._last_pan_cmd = 0.0
        self._last_tilt_cmd = 0.0
        self._speed_scale = 1.0

    def set_target_strategy(self, strategy: TargetStrategy) -> None:
        self._target_strategy = strategy
        self._off_center_frames = 0

    def set_speed_mode(self, mode: str) -> None:
        # Unified table avoids scattered branch constants.
        speed_map = {"slow": 0.8, "normal": 1.0, "fast": 2.4, "turbo": 3.2}
        self._speed_scale = speed_map.get(mode, 1.0)

    def get_target_point(self, frame_shape: tuple[int, int, int]) -> tuple[int, int]:
        point = self._target_strategy.get_target_point(frame_shape)
        return int(point.x), int(point.y)

    def compute_command(
        self,
        frame_shape: tuple[int, int, int],
        detection: DetectionResult | None,
    ) -> GimbalCommand | None:
        if detection is None:
            self._off_center_frames = 0
            self._last_anchor = None
            self._last_pan_cmd = 0.0
            self._last_tilt_cmd = 0.0
            return None

        target = self._target_strategy.get_target_point(frame_shape)
        center = detection.anchor_point if detection.anchor_point is not None else detection.bbox.center
        cx, cy = center.x, center.y

        if self._last_anchor is not None:
            jump = hypot(cx - self._last_anchor[0], cy - self._last_anchor[1])
            if jump > self._config.max_anchor_jump_px:
                # Drop sudden outlier to avoid servo snapping.
                self._last_anchor = (cx, cy)
                return None
            a = self._config.command_smooth_alpha
            cx = self._last_anchor[0] * (1 - a) + cx * a
            cy = self._last_anchor[1] * (1 - a) + cy * a
        self._last_anchor = (cx, cy)

        offset_x = cx - target.x
        offset_y = cy - target.y

        in_deadzone = (
            abs(offset_x) <= self._config.deadzone_px
            and abs(offset_y) <= self._config.deadzone_px
        )
        if in_deadzone:
            self._off_center_frames = 0
            return None

        self._off_center_frames += 1
        if self._off_center_frames < self._config.debounce_frames:
            return None

        now = time.time()
        effective_interval = self._config.min_command_interval_s / max(0.5, self._speed_scale)
        if now - self._last_command_ts < effective_interval:
            return None
        self._last_command_ts = now

        raw_pan = self._clamp(offset_x * self._config.gain_x, self._config.max_delta_deg)
        raw_tilt = self._clamp(offset_y * self._config.gain_y, self._config.max_delta_deg)

        # Sign may vary by hardware mounting direction.
        if not self._config.invert_pan:
            raw_pan = -raw_pan
        if self._config.invert_tilt:
            raw_tilt = -raw_tilt

        sa = self._config.command_smooth_alpha
        pan_delta = self._last_pan_cmd * (1 - sa) + raw_pan * sa
        tilt_delta = self._last_tilt_cmd * (1 - sa) + raw_tilt * sa
        self._last_pan_cmd = pan_delta
        self._last_tilt_cmd = tilt_delta

        max_delta_limit = self._config.max_delta_deg * max(0.7, self._speed_scale)
        pan_delta = self._clamp(pan_delta * self._speed_scale, max_delta_limit)
        tilt_delta = self._clamp(tilt_delta * self._speed_scale, max_delta_limit)

        if (
            abs(pan_delta) < self._config.min_output_deg
            and abs(tilt_delta) < self._config.min_output_deg
        ):
            return None

        return GimbalCommand(pan_delta=pan_delta, tilt_delta=tilt_delta, reason="auto_track")

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        return min(limit, max(-limit, value))
