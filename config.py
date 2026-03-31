from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class VideoSourceConfig:
    stream_url: str
    reconnect_interval_s: float = 2.0
    threaded_capture: bool = True
    read_sleep_s: float = 0.001
    capture_buffer_size: int = 1


@dataclass(slots=True)
class DetectionConfig:
    min_confidence: float = 0.4
    detector_fps: float = 12.0
    max_inference_side: int = 960
    yolo_every_n_frames: int = 2
    yolo_bbox_smooth_alpha: float = 0.4
    enable_face_landmarks: bool = True


@dataclass(slots=True)
class ServoAxisConfig:
    min_angle: float
    max_angle: float
    home_angle: float
    servo_id: int = 0
    max_step_deg: float = 3.0


@dataclass(slots=True)
class GimbalConfig:
    pan: ServoAxisConfig
    tilt: ServoAxisConfig
    use_mock: bool = True
    smooth_sleep_s: float = 0.01
    feedback_poll_interval_s: float = 0.05


@dataclass(slots=True)
class TrackingConfig:
    deadzone_px: int = 30
    debounce_frames: int = 2
    gain_x: float = 0.024
    gain_y: float = 0.024
    max_delta_deg: float = 2.8
    min_command_interval_s: float = 0.08
    command_smooth_alpha: float = 0.4
    min_output_deg: float = 0.1
    max_anchor_jump_px: float = 120.0
    settle_after_move_s: float = 0.18
    invert_pan: bool = False
    invert_tilt: bool = False


@dataclass(slots=True)
class AppConfig:
    manual_step_deg: float = 3.0
    ui_refresh_fps: float = 30.0
    preview_scale: float = 1.0
    enable_overlay: bool = True
    show_body_skeleton: bool = True
    show_face_mesh: bool = True


@dataclass(slots=True)
class SystemConfig:
    video: VideoSourceConfig
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    gimbal: GimbalConfig = field(
        default_factory=lambda: GimbalConfig(
            pan=ServoAxisConfig(min_angle=-135.0, max_angle=135.0, home_angle=0.0, servo_id=0),
            tilt=ServoAxisConfig(min_angle=-90.0, max_angle=130.0, home_angle=15.0, servo_id=1),
        )
    )
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    app: AppConfig = field(default_factory=AppConfig)


def default_config(stream_url: str) -> SystemConfig:
    return SystemConfig(video=VideoSourceConfig(stream_url=stream_url))
