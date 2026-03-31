"""Core application logic shared across modules."""

from __future__ import annotations

import collections
import threading
import time
from typing import Callable

import cv2

from gimbal_controller import GimbalController
from interfaces.capture_trigger import CaptureTrigger
from interfaces.target_strategy import TargetPreset, build_target_strategy
from mode_manager import ControlMode, ModeManager
from tracking_controller import TrackingController
from utils.common_types import DetectionResult, Point, VisionResult
from utils.ui_text import FOLLOW_TEXT, SPEED_TEXT, follow_to_text, speed_to_text

# ---------------------------------------------------------------------------
# Detection thresholds
# ---------------------------------------------------------------------------
MIN_DETECTION_AREA_RATIO = 0.015
MIN_DETECTION_CONFIDENCE = 0.60
RELIABLE_STREAK_FOR_TRACKING = 3

# ---------------------------------------------------------------------------
# Template overlay skeleton edges
# ---------------------------------------------------------------------------
TEMPLATE_CORE_EDGES: tuple[tuple[int, int], ...] = (
    (11, 12),
    (11, 23), (12, 24),
    (23, 24),
    (11, 13), (13, 15),
    (12, 14), (14, 16),
    (23, 25), (25, 27),
    (24, 26), (26, 28),
)


# ---------------------------------------------------------------------------
# Utility classes
# ---------------------------------------------------------------------------
class EventRateCounter:
    """Counts event rate in a rolling time window."""

    def __init__(self, window_s: float = 1.0) -> None:
        self._window_s = max(0.2, window_s)
        self._events: collections.deque[float] = collections.deque()

    def mark(self, ts: float | None = None) -> None:
        now = ts if ts is not None else time.time()
        self._events.append(now)
        self._trim(now)

    def value(self, ts: float | None = None) -> float:
        now = ts if ts is not None else time.time()
        self._trim(now)
        return len(self._events) / self._window_s

    def _trim(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._events and self._events[0] < cutoff:
            self._events.popleft()


class TargetSelector:
    """Selects one target from multi-person candidates with nearest-first policy."""

    def __init__(self) -> None:
        self._last_center: tuple[float, float] | None = None

    def select(self, vision: VisionResult, follow_mode: str) -> DetectionResult | None:
        if follow_mode == "face":
            candidates = vision.tracking_candidates or []
            if not candidates:
                base = vision.tracking_detection
                if base is None:
                    self._last_center = None
                    return None
                det = self._with_head_anchor(base)
                self._last_center = (det.bbox.center.x, det.bbox.center.y)
                return det
            head_candidates = [self._with_head_anchor(c) for c in candidates]
            if self._last_center is None:
                best = max(head_candidates, key=lambda d: d.bbox.area)
            else:
                best = min(
                    head_candidates,
                    key=lambda d: (
                        (d.anchor_point.x - self._last_center[0]) ** 2
                        + (d.anchor_point.y - self._last_center[1]) ** 2,
                        -d.bbox.area,
                    ),
                )
            self._last_center = (best.anchor_point.x, best.anchor_point.y)
            return best
        candidates = vision.tracking_candidates or []
        if not candidates:
            det = vision.tracking_detection
            self._last_center = (det.bbox.center.x, det.bbox.center.y) if det is not None else None
            return det
        if self._last_center is None:
            best = max(candidates, key=lambda d: d.bbox.area)
        else:
            best = min(
                candidates,
                key=lambda d: (
                    (d.bbox.center.x - self._last_center[0]) ** 2
                    + (d.bbox.center.y - self._last_center[1]) ** 2,
                    -d.bbox.area,
                ),
            )
        self._last_center = (best.bbox.center.x, best.bbox.center.y)
        return best

    @staticmethod
    def _with_head_anchor(base: DetectionResult) -> DetectionResult:
        anchor = _head_anchor_from_detection(base)
        if anchor is None:
            anchor = base.bbox.center
        return DetectionResult(
            bbox=base.bbox,
            confidence=base.confidence,
            label="head_pose_fallback",
            track_id=base.track_id,
            anchor_point=anchor,
            pose_landmarks=base.pose_landmarks,
        )


def _head_anchor_from_detection(detection: DetectionResult) -> Point | None:
    pose = detection.pose_landmarks or {}
    nose = pose.get(0)
    left_ear = pose.get(7)
    right_ear = pose.get(8)
    if nose is not None:
        return Point(x=float(nose.x), y=float(nose.y))
    if left_ear is not None and right_ear is not None:
        return Point(
            x=(float(left_ear.x) + float(right_ear.x)) * 0.5,
            y=(float(left_ear.y) + float(right_ear.y)) * 0.5,
        )
    if left_ear is not None:
        return Point(x=float(left_ear.x), y=float(left_ear.y))
    if right_ear is not None:
        return Point(x=float(right_ear.x), y=float(right_ear.y))
    b = detection.bbox
    return Point(x=float(b.center.x), y=float(b.y + b.h * 0.18))


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------
def reliable_detection(
    detection: DetectionResult | None, frame_shape: tuple[int, int, int]
) -> DetectionResult | None:
    """Filter out unreliable detections by area ratio and confidence."""
    if detection is None:
        return None
    if detection.label not in {"person_pose", "person_mp_yolo", "face_center", "head_pose_fallback"}:
        return None
    h, w = frame_shape[:2]
    area_ratio = detection.bbox.area / float(max(1, h * w))
    if area_ratio < MIN_DETECTION_AREA_RATIO:
        return None
    if detection.confidence < MIN_DETECTION_CONFIDENCE:
        return None
    if detection.anchor_point is not None:
        return detection
    # Fallback anchor to keep tracking stable when shoulders/nose are briefly lost.
    return DetectionResult(
        bbox=detection.bbox,
        confidence=detection.confidence,
        label=detection.label,
        track_id=detection.track_id,
        anchor_point=detection.bbox.center,
        pose_landmarks=detection.pose_landmarks,
    )


def build_draw_vision(vision: VisionResult, reliable: DetectionResult | None) -> VisionResult:
    """Build a VisionResult for overlay rendering with the reliable detection."""
    return VisionResult(
        tracking_detection=reliable,
        tracking_candidates=vision.tracking_candidates,
        face_tracking_detection=vision.face_tracking_detection,
        person_bbox=vision.person_bbox,
        face_bbox=vision.face_bbox,
        body_skeleton=vision.body_skeleton,
        face_mesh=vision.face_mesh,
        hand_landmarks=vision.hand_landmarks,
        hand_handedness=vision.hand_handedness,
    )


def prepare_capture_frame(frame, mirror_view: bool):
    """Copy frame and optionally mirror for saving."""
    out = frame.copy()
    if mirror_view:
        out = cv2.flip(out, 1)
    return out


# ---------------------------------------------------------------------------
# Command processor
# ---------------------------------------------------------------------------
def process_command(
    command: str,
    *,
    mode_manager: ModeManager,
    tracking: TrackingController,
    gimbal: GimbalController,
    capture_trigger: CaptureTrigger,
    manual_step_deg: float,
    stop_event: threading.Event,
    notify: Callable[[str], None] | None = None,
    set_follow_mode: Callable[[str], None] | None = None,
    set_speed_mode: Callable[[str], None] | None = None,
) -> None:
    """Shared command processor for GUI command input."""
    parts = command.split()
    if not parts:
        return
    head = parts[0].lower()

    def say(text: str) -> None:
        if notify is None:
            print(text)
        else:
            notify(text)

    def parse_float_arg(index: int, name: str) -> float | None:
        try:
            return float(parts[index])
        except (ValueError, IndexError):
            say(f"参数错误: {name} 需要数字")
            return None

    if head in {"quit", "exit", "q"}:
        stop_event.set()
        return

    if head == "help":
        say(
            "命令: help | mode manual|auto|compose | a/d/w/s | rel <pan> <tilt> | "
            "abs <pan> <tilt> | home | speed slow|normal|fast|turbo | "
            "follow shoulders|face | strategy center|left_third|lower_left|custom <x> <y> | "
            "capture | state | quit"
        )
        return

    if head == "speed" and len(parts) >= 2:
        mode = parts[1].lower()
        if mode in SPEED_TEXT:
            if set_speed_mode is not None:
                set_speed_mode(mode)
            say(f"速度 => {speed_to_text(mode)}")
        return

    if head == "follow" and len(parts) >= 2:
        mode = parts[1].lower()
        if mode in FOLLOW_TEXT:
            if set_follow_mode is not None:
                set_follow_mode(mode)
            say(f"跟随点 => {follow_to_text(mode)}")
        return

    if head == "mode" and len(parts) >= 2:
        val = parts[1].lower()
        if val == "manual":
            mode_manager.set_mode(ControlMode.MANUAL)
            say("模式 => 手动")
        elif val == "auto":
            mode_manager.set_mode(ControlMode.AUTO_TRACK)
            say("模式 => 自动跟随")
        elif val == "compose":
            mode_manager.set_mode(ControlMode.SMART_COMPOSE)
            say("模式 => 模板引导")
        return

    if head in {"a", "left"}:
        gimbal.move_relative(-manual_step_deg, 0.0)
        return
    if head in {"d", "right"}:
        gimbal.move_relative(manual_step_deg, 0.0)
        return
    if head in {"w", "up"}:
        gimbal.move_relative(0.0, -manual_step_deg)
        return
    if head in {"s", "down"}:
        gimbal.move_relative(0.0, manual_step_deg)
        return

    if head == "rel":
        pan = parse_float_arg(1, "pan")
        tilt = parse_float_arg(2, "tilt")
        if pan is not None and tilt is not None:
            gimbal.move_relative(pan, tilt)
        return

    if head == "abs":
        pan = parse_float_arg(1, "pan")
        tilt = parse_float_arg(2, "tilt")
        if pan is not None and tilt is not None:
            gimbal.set_absolute(pan, tilt)
        return

    if head == "home":
        gimbal.home()
        return

    if head == "capture":
        capture_trigger.trigger_capture(metadata={"source": "manual_command"})
        return

    if head == "state":
        state = gimbal.refresh_feedback()
        say(
            "云台状态 => "
            f"指令(横={state.pan_command_angle:.2f}, 纵={state.tilt_command_angle:.2f}) "
            f"反馈(横={state.pan_feedback_angle:.2f}, 纵={state.tilt_feedback_angle:.2f}, 有效={state.feedback_valid})"
        )
        return

    if head == "strategy" and len(parts) >= 2:
        val = parts[1].lower()
        if val == "center":
            tracking.set_target_strategy(build_target_strategy(TargetPreset.CENTER))
        elif val == "left_third":
            tracking.set_target_strategy(build_target_strategy(TargetPreset.LEFT_THIRD))
        elif val == "lower_left":
            tracking.set_target_strategy(build_target_strategy(TargetPreset.LOWER_LEFT))
        elif val == "custom":
            x = parse_float_arg(2, "x")
            y = parse_float_arg(3, "y")
            if x is not None and y is not None:
                tracking.set_target_strategy(build_target_strategy(TargetPreset.CUSTOM_POINT, custom=(x, y)))
        return

    say(f"未知命令: {command}")
