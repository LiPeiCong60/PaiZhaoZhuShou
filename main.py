from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Callable

import cv2
import numpy as np

from app_core import (
    RELIABLE_STREAK_FOR_TRACKING,
    TEMPLATE_CORE_EDGES,
    EventRateCounter,
    TargetSelector,
    build_draw_vision,
    prepare_capture_frame,
    process_command,
    reliable_detection,
)
from config import default_config
from detector import AsyncDetector, MediaPipeVisionDetector, MediaPipeYoloVisionDetector
from gimbal_controller import (
    GimbalController,
    MockServoDriver,
    RaspberryPiPWMDriver,
    ServoDriver,
    TTLBusSerialDriver,
)
from interfaces.ai_assistant import AIPhotoAssistant, build_ai_assistant_from_env
from interfaces.capture_trigger import CaptureTrigger, LocalFileCaptureTrigger
from interfaces.target_strategy import TargetPreset, build_target_strategy
from mode_manager import ControlMode, ModeManager
from template_compose import GestureCaptureState, TemplateComposeEngine, TemplateLibrary
from tracking_controller import TrackingController
from ui.cn_text import draw_cn_text
from utils.common_types import DetectionResult, Point, VisionResult
from utils.overlay_renderer import OverlayRenderer
from utils.ui_text import (
    FOLLOW_TEXT,
    FOLLOW_TEXT_TO_KEY,
    SPEED_TEXT,
    SPEED_TEXT_TO_KEY,
    follow_to_text,
    mode_to_text,
    speed_to_text,
)
from video_source import OpenCVVideoSource

# 导入服务层
from services.control_service import ControlService
from services.template_service import TemplateService
from services.capture_service import CaptureService
from services.ai_orchestrator import AIOrchestrator
from services.status_service import StatusService
from services.runtime_state import RuntimeState
from repositories.local_template_repository import LocalTemplateRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smart Camera Assistant")
    parser.add_argument("--stream-url", type=str, required=True, help="Video stream URL or camera index")
    parser.add_argument("--mock-gimbal", action="store_true", help="Use mock gimbal driver")
    parser.add_argument(
        "--detector-backend",
        choices=["mediapipe", "mediapipe_yolo"],
        default="mediapipe_yolo",
        help="Vision backend",
    )
    parser.add_argument("--start-mode", choices=["MANUAL", "AUTO_TRACK", "SMART_COMPOSE"], default="MANUAL")
    parser.add_argument("--pan-servo-id", type=int, default=0, help="Pan servo id/channel")
    parser.add_argument("--tilt-servo-id", type=int, default=1, help="Tilt servo id/channel")
    parser.add_argument("--pca9685-address", default="0x40", help="PCA9685 I2C address, e.g. 0x40")
    parser.add_argument("--bus-serial-port", type=str, default="", help="TTL serial port")
    parser.add_argument("--bus-baudrate", type=int, default=115200, help="TTL serial baudrate")
    parser.add_argument("--bus-move-time-ms", type=int, default=120, help="TTL command move time ms")
    parser.add_argument("--yolo-model", default="yolo11n.pt", help="YOLO model path/name")
    parser.add_argument("--yolo-conf", type=float, default=0.45, help="YOLO confidence threshold")
    parser.add_argument("--yolo-device", default="cpu", help="YOLO device, e.g. cpu or 0")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    parser.add_argument("--detector-fps", type=float, default=12.0, help="Vision detector fps limit")
    parser.add_argument("--preview-fps", type=float, default=30.0, help="Preview fps limit")
    parser.add_argument("--preview-scale", type=float, default=1.0, help="Preview scale, 0.5 means half-size")
    parser.add_argument(
        "--mirror-view",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mirror preview/capture image horizontally (default: true)",
    )
    parser.add_argument("--max-inference-side", type=int, default=960, help="Resize long side before inference")
    parser.add_argument("--yolo-every-n-frames", type=int, default=2, help="Run YOLO every N detector frames")
    parser.add_argument("--yolo-bbox-smooth-alpha", type=float, default=0.4, help="YOLO bbox smoothing alpha")
    parser.add_argument("--rpi-mode", action="store_true", help="Apply Raspberry Pi performance defaults")
    parser.add_argument("--disable-face-landmarks", action="store_true", help="Disable face landmark detection")
    parser.add_argument("--disable-overlay", action="store_true", help="Disable overlay drawing")
    parser.add_argument("--hide-face-mesh", action="store_true", help="Hide face mesh lines in overlay")
    parser.add_argument("--hide-body-skeleton", action="store_true", help="Hide body skeleton lines in overlay")
    return parser.parse_args()


class GuiApp:
    def __init__(
        self,
        *,
        source: OpenCVVideoSource,
        detector: MediaPipeVisionDetector | MediaPipeYoloVisionDetector,
        tracking: TrackingController,
        mode_manager: ModeManager,
        gimbal: GimbalController,
        capture_trigger: CaptureTrigger,
        async_detector: AsyncDetector,
        manual_step_deg: float,
        detector_fps: float,
        preview_fps: float,
        preview_scale: float,
        mirror_view: bool,
        enable_overlay: bool,
        show_body_skeleton: bool,
        show_face_mesh: bool,
        ai_assistant: AIPhotoAssistant | None = None,
    ) -> None:
        self._source = source
        self._detector = detector
        self._tracking = tracking
        self._mode_manager = mode_manager
        self._gimbal = gimbal
        self._capture_trigger = capture_trigger
        self._async_detector = async_detector
        self._manual_step_deg = manual_step_deg
        self._detector_interval_s = 1.0 / max(1.0, detector_fps)
        self._preview_interval_s = 1.0 / max(1.0, preview_fps)
        self._preview_scale = min(1.0, max(0.2, preview_scale))
        self._mirror_view = mirror_view
        self._ai_assistant = ai_assistant or build_ai_assistant_from_env()
        self._runtime_state = RuntimeState()

        # 初始化服务层
        self._template_library = TemplateLibrary()
        template_repository = LocalTemplateRepository(self._template_library)

        self._control_service = ControlService(
            mode_manager=self._mode_manager,
            tracking=self._tracking,
            gimbal=self._gimbal,
            runtime_state=self._runtime_state,
            manual_step_deg=self._manual_step_deg,
        )

        self._capture_service = CaptureService(
            capture_trigger=self._capture_trigger,
            ai_assistant=self._ai_assistant,
            runtime_state=self._runtime_state,
        )

        self._template_service = TemplateService(
            repository=template_repository,
            runtime_state=self._runtime_state,
        )

        self._ai_orchestrator = AIOrchestrator(
            ai_assistant=self._ai_assistant,
            control_service=self._control_service,
            capture_service=self._capture_service,
            runtime_state=self._runtime_state,
            frame_provider=self._source.read,
            capture_frame_for_save=self._capture_frame_for_save,
        )

        self._status_service = StatusService(
            mode_manager=self._mode_manager,
            runtime_state=self._runtime_state,
        )

        # 状态变量
        self._stop_event = threading.Event()
        self._follow_mode = "shoulders"
        self._speed_mode = "normal"
        self._reliable_detection_streak = 0
        self._last_submit_ts = 0.0
        self._last_preview_ts = 0.0
        self._tracking_hold_until = 0.0
        self._last_detector_error_ts = 0.0
        self._render_rate = EventRateCounter()
        self._detect_rate = EventRateCounter()
        self._target_selector = TargetSelector()
        self._template_engine = TemplateComposeEngine()
        self._gesture_state = GestureCaptureState()
        self._selected_template_id: str | None = None
        self._last_compose_feedback = None
        self._ready_since_ts = 0.0
        self._latest_frame = None
        self._pending_capture_deadline = 0.0
        self._pending_capture_metadata: dict[str, object] | None = None
        self._last_countdown_log_s = -1
        self._template_preview_photo = None
        self._bg_task_queue: queue.Queue[tuple[Callable[[], Any], Callable[[Any], None] | None, Callable[[Exception], None] | None]] = queue.Queue()
        self._bg_task_count = 0
        self._bg_task_lock = threading.Lock()
        self._ai_control_buttons: list[tk.Widget] = []

        self._tracking.set_speed_mode(self._speed_mode)
        self._overlay = OverlayRenderer(
            enable_overlay=enable_overlay,
            show_body_skeleton=show_body_skeleton,
            show_face_mesh=show_face_mesh,
        )

        self._root = tk.Tk()
        self._root.title("智能云台助手")
        self._root.geometry("1280x800")
        self._root.minsize(960, 620)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._gesture_capture_enabled = tk.BooleanVar(master=self._root, value=True)
        self._force_ok_enabled = tk.BooleanVar(master=self._root, value=False)
        self._capture_auto_analyze_enabled = tk.BooleanVar(master=self._root, value=False)
        self._compose_auto_control = tk.BooleanVar(master=self._root, value=True)
        self._ai_angle_search_running = False
        self._ai_lock_mode_enabled = False
        self._ai_lock_target_box_norm: tuple[float, float, float, float] | None = None
        self._ai_lock_fit_score = 0.0
        self._show_ai_lock_box = tk.BooleanVar(master=self._root, value=True)
        self._ai_lock_fit_threshold = tk.DoubleVar(master=self._root, value=0.62)
        self._ai_lock_max_delta = tk.DoubleVar(master=self._root, value=12.0)
        self._ai_scan_pan_range = tk.DoubleVar(master=self._root, value=6.0)
        self._ai_scan_tilt_range = tk.DoubleVar(master=self._root, value=3.0)
        self._ai_scan_pan_step = tk.DoubleVar(master=self._root, value=4.0)
        self._ai_scan_tilt_step = tk.DoubleVar(master=self._root, value=3.0)
        self._ai_scan_max_candidates = tk.IntVar(master=self._root, value=9)
        self._ai_scan_settle_s = tk.DoubleVar(master=self._root, value=0.35)
        self._ai_angle_countdown_s = tk.IntVar(master=self._root, value=0)
        self._bg_capture_delay_s = tk.DoubleVar(master=self._root, value=3.0)
        self._gesture_countdown_s = tk.DoubleVar(master=self._root, value=3.0)
        self._gesture_stable_frames_var = tk.IntVar(master=self._root, value=10)
        self._gesture_open_hold_s_var = tk.DoubleVar(master=self._root, value=0.35)
        self._compose_score_threshold_var = tk.DoubleVar(
            master=self._root, value=TemplateComposeEngine.SCORE_THRESHOLD
        )
        self._mirror_view_var = tk.BooleanVar(master=self._root, value=self._mirror_view)
        self._show_live_lines = tk.BooleanVar(master=self._root, value=True)
        self._show_live_bbox = tk.BooleanVar(master=self._root, value=True)
        self._show_template_lines = tk.BooleanVar(master=self._root, value=False)
        self._show_template_bbox = tk.BooleanVar(master=self._root, value=False)
        self._live_overlay_alpha = tk.DoubleVar(master=self._root, value=0.85)
        self._template_overlay_alpha = tk.DoubleVar(master=self._root, value=0.7)
        self._ui_settings_path = os.path.join(".cache", "ui_settings.json")

        try:
            from PIL import Image, ImageTk
        except ImportError as exc:
            raise RuntimeError("GUI 模式需要 Pillow，请先安装 requirements.txt 依赖。") from exc
        self._pil_image = Image
        self._pil_image_tk = ImageTk

        self._build_ui()
        self._load_ui_settings()
        self._bg_worker = threading.Thread(target=self._bg_worker_loop, daemon=True, name="ai-bg-worker")
        self._bg_worker.start()
        self._schedule_tick()

    @property
    def _follow_mode(self) -> str:
        return self._runtime_state.follow_mode

    @_follow_mode.setter
    def _follow_mode(self, value: str) -> None:
        self._runtime_state.follow_mode = value

    @property
    def _speed_mode(self) -> str:
        return self._runtime_state.speed_mode

    @_speed_mode.setter
    def _speed_mode(self, value: str) -> None:
        self._runtime_state.speed_mode = value

    @property
    def _selected_template_id(self) -> str | None:
        return self._runtime_state.selected_template_id

    @_selected_template_id.setter
    def _selected_template_id(self, value: str | None) -> None:
        self._runtime_state.selected_template_id = value

    @property
    def _reliable_detection_streak(self) -> int:
        return self._runtime_state.reliable_detection_streak

    @_reliable_detection_streak.setter
    def _reliable_detection_streak(self, value: int) -> None:
        self._runtime_state.reliable_detection_streak = value

    @property
    def _last_compose_feedback(self):
        return self._runtime_state.last_compose_feedback

    @_last_compose_feedback.setter
    def _last_compose_feedback(self, value) -> None:
        self._runtime_state.last_compose_feedback = value

    @property
    def _ready_since_ts(self) -> float:
        return self._runtime_state.ready_since_ts

    @_ready_since_ts.setter
    def _ready_since_ts(self, value: float) -> None:
        self._runtime_state.ready_since_ts = value

    @property
    def _latest_frame(self):
        return self._runtime_state.latest_frame

    @_latest_frame.setter
    def _latest_frame(self, value) -> None:
        self._runtime_state.latest_frame = value

    @property
    def _ai_angle_search_running(self) -> bool:
        return self._runtime_state.ai_angle_search_running

    @_ai_angle_search_running.setter
    def _ai_angle_search_running(self, value: bool) -> None:
        self._runtime_state.ai_angle_search_running = value

    @property
    def _ai_lock_mode_enabled(self) -> bool:
        return self._runtime_state.ai_lock_mode_enabled

    @_ai_lock_mode_enabled.setter
    def _ai_lock_mode_enabled(self, value: bool) -> None:
        self._runtime_state.ai_lock_mode_enabled = value

    @property
    def _ai_lock_target_box_norm(self) -> tuple[float, float, float, float] | None:
        return self._runtime_state.ai_lock_target_box_norm

    @_ai_lock_target_box_norm.setter
    def _ai_lock_target_box_norm(self, value: tuple[float, float, float, float] | None) -> None:
        self._runtime_state.ai_lock_target_box_norm = value

    @property
    def _ai_lock_fit_score(self) -> float:
        return self._runtime_state.ai_lock_fit_score

    @_ai_lock_fit_score.setter
    def _ai_lock_fit_score(self, value: float) -> None:
        self._runtime_state.ai_lock_fit_score = value

    def _build_ui(self) -> None:
        self._root.grid_columnconfigure(0, weight=4)
        self._root.grid_columnconfigure(1, weight=1)
        self._root.grid_rowconfigure(0, weight=3)
        self._root.grid_rowconfigure(1, weight=2)

        video_panel = ttk.Frame(self._root, padding=8)
        video_panel.grid(row=0, column=0, sticky="nsew")
        video_panel.grid_columnconfigure(0, weight=1)
        video_panel.grid_rowconfigure(0, weight=1)
        self._video_label = ttk.Label(video_panel)
        self._video_label.grid(row=0, column=0, sticky="nsew")

        control_host = ttk.Frame(self._root)
        control_host.grid(row=0, column=1, sticky="nsew")
        control_host.grid_columnconfigure(0, weight=1)
        control_host.grid_rowconfigure(0, weight=1)

        self._control_canvas = tk.Canvas(control_host, highlightthickness=0)
        self._control_canvas.grid(row=0, column=0, sticky="nsew")
        control_scrollbar = ttk.Scrollbar(control_host, orient="vertical", command=self._control_canvas.yview)
        control_scrollbar.grid(row=0, column=1, sticky="ns")
        self._control_canvas.configure(yscrollcommand=control_scrollbar.set)

        control_panel = ttk.Frame(self._control_canvas, padding=10)
        self._control_window_id = self._control_canvas.create_window((0, 0), window=control_panel, anchor="nw")
        control_panel.grid_columnconfigure(0, weight=1)
        control_panel.bind("<Configure>", self._on_control_frame_configure)
        self._control_canvas.bind("<Configure>", self._on_control_canvas_configure)

        mode_box = ttk.LabelFrame(control_panel, text="模式", padding=8)
        mode_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(mode_box, text="手动", command=lambda: self._run_cmd("mode manual")).grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Button(mode_box, text="自动跟随", command=lambda: self._run_cmd("mode auto")).grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Button(mode_box, text="模板引导", command=lambda: self._run_cmd("mode compose")).grid(row=2, column=0, sticky="ew", pady=2)

        manual_box = ttk.LabelFrame(control_panel, text="手动控制", padding=8)
        manual_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(manual_box, text="上", command=lambda: self._run_cmd("w")).grid(row=0, column=1, padx=4, pady=4)
        ttk.Button(manual_box, text="左", command=lambda: self._run_cmd("a")).grid(row=1, column=0, padx=4, pady=4)
        ttk.Button(manual_box, text="右", command=lambda: self._run_cmd("d")).grid(row=1, column=2, padx=4, pady=4)
        ttk.Button(manual_box, text="下", command=lambda: self._run_cmd("s")).grid(row=2, column=1, padx=4, pady=4)
        ttk.Button(manual_box, text="回中", command=lambda: self._run_cmd("home")).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        util_box = ttk.LabelFrame(control_panel, text="工具", padding=8)
        util_box.grid(row=2, column=0, sticky="ew")
        ttk.Button(util_box, text="状态", command=lambda: self._run_cmd("state")).grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Button(util_box, text="抓拍", command=lambda: self._run_cmd("capture")).grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Button(util_box, text="上传模板", command=self._upload_template).grid(row=2, column=0, sticky="ew", pady=2)
        ttk.Button(util_box, text="删除模板", command=self._delete_template).grid(row=3, column=0, sticky="ew", pady=2)
        btn_upload_score = ttk.Button(util_box, text="上传评分", command=self._upload_and_score_photo)
        btn_upload_score.grid(row=4, column=0, sticky="ew", pady=2)
        btn_bg_upload = ttk.Button(util_box, text="背景分析(上传)", command=self._upload_and_analyze_background)
        btn_bg_upload.grid(row=5, column=0, sticky="ew", pady=2)
        btn_template_bg = ttk.Button(util_box, text="模板+背景指导", command=self._guide_with_template_background)
        btn_template_bg.grid(row=6, column=0, sticky="ew", pady=2)
        btn_ai_angle = ttk.Button(util_box, text="AI自动找角度", command=self._start_ai_angle_search)
        btn_ai_angle.grid(row=7, column=0, sticky="ew", pady=2)
        btn_bg_lock = ttk.Button(util_box, text="现场背景并锁机位", command=self._analyze_background_and_lock)
        btn_bg_lock.grid(row=8, column=0, sticky="ew", pady=2)
        self._ai_control_buttons.extend([btn_upload_score, btn_bg_upload, btn_template_bg, btn_ai_angle, btn_bg_lock])
        ttk.Button(util_box, text="解除机位锁定", command=self._unlock_ai_lock_mode).grid(row=9, column=0, sticky="ew", pady=2)
        ttk.Checkbutton(
            util_box,
            text="启用手势拍照(张开->握拳)",
            variable=self._gesture_capture_enabled,
            onvalue=True,
            offvalue=False,
        ).grid(row=10, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            util_box,
            text="启用强制拍照(单手OK)",
            variable=self._force_ok_enabled,
            onvalue=True,
            offvalue=False,
        ).grid(row=11, column=0, sticky="w", pady=(4, 0))
        ttk.Checkbutton(
            util_box,
            text="抓拍后自动AI分析",
            variable=self._capture_auto_analyze_enabled,
            onvalue=True,
            offvalue=False,
        ).grid(row=12, column=0, sticky="w", pady=(4, 0))
        ttk.Button(util_box, text="保存选项", command=self._save_ui_settings_manual).grid(
            row=13, column=0, sticky="ew", pady=(6, 0)
        )

        tune_box = ttk.LabelFrame(control_panel, text="跟随调参", padding=8)
        tune_box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(tune_box, text="跟随点").grid(row=0, column=0, sticky="w")
        self._follow_var = tk.StringVar(value=follow_to_text(self._follow_mode))
        follow_combo = ttk.Combobox(tune_box, textvariable=self._follow_var, values=list(FOLLOW_TEXT_TO_KEY.keys()), state="readonly")
        follow_combo.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        follow_combo.bind("<<ComboboxSelected>>", lambda _e: self._set_follow_mode(FOLLOW_TEXT_TO_KEY.get(self._follow_var.get(), "shoulders")))

        ttk.Label(tune_box, text="速度").grid(row=2, column=0, sticky="w")
        self._speed_var = tk.StringVar(value=speed_to_text(self._speed_mode))
        speed_combo = ttk.Combobox(tune_box, textvariable=self._speed_var, values=list(SPEED_TEXT_TO_KEY.keys()), state="readonly")
        speed_combo.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        speed_combo.bind("<<ComboboxSelected>>", lambda _e: self._set_speed_mode(SPEED_TEXT_TO_KEY.get(self._speed_var.get(), "normal")))
        ttk.Checkbutton(
            tune_box,
            text="模板引导自动转动",
            variable=self._compose_auto_control,
            onvalue=True,
            offvalue=False,
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            tune_box,
            text="显示锁定框",
            variable=self._show_ai_lock_box,
            onvalue=True,
            offvalue=False,
        ).grid(row=5, column=0, sticky="w", pady=(4, 0))
        tune_box.grid_columnconfigure(0, weight=1)
        self._add_labeled_scale(tune_box, row=6, text="手势倒计时(秒)", variable=self._gesture_countdown_s, from_=1.0, to=5.0, formatter=lambda value: f"{value:.1f}")
        self._add_labeled_scale(tune_box, row=8, text="手势稳定帧", variable=self._gesture_stable_frames_var, from_=6, to=16, formatter=lambda value: f"{int(round(value))}")
        self._add_labeled_scale(tune_box, row=10, text="张手最短保持(秒)", variable=self._gesture_open_hold_s_var, from_=0.1, to=1.0, formatter=lambda value: f"{value:.2f}")
        self._add_labeled_scale(tune_box, row=12, text="模板达标阈值", variable=self._compose_score_threshold_var, from_=55.0, to=90.0, formatter=lambda value: f"{value:.1f}")
        self._add_labeled_scale(tune_box, row=14, text="入框判定阈值", variable=self._ai_lock_fit_threshold, from_=0.4, to=0.9, formatter=lambda value: f"{value:.2f}")
        self._add_labeled_scale(tune_box, row=16, text="扫描Pan范围(°)", variable=self._ai_scan_pan_range, from_=2.0, to=16.0, formatter=lambda value: f"{value:.1f}")
        self._add_labeled_scale(tune_box, row=18, text="扫描Tilt范围(°)", variable=self._ai_scan_tilt_range, from_=1.0, to=10.0, formatter=lambda value: f"{value:.1f}")
        self._add_labeled_scale(tune_box, row=20, text="扫描Pan步长(°)", variable=self._ai_scan_pan_step, from_=1.0, to=8.0, formatter=lambda value: f"{value:.1f}")
        self._add_labeled_scale(tune_box, row=22, text="扫描Tilt步长(°)", variable=self._ai_scan_tilt_step, from_=1.0, to=6.0, formatter=lambda value: f"{value:.1f}")
        self._add_labeled_scale(tune_box, row=24, text="扫描候选数量", variable=self._ai_scan_max_candidates, from_=2, to=9, formatter=lambda value: f"{int(round(value))}")
        self._add_labeled_scale(tune_box, row=26, text="扫描候选等待(秒)", variable=self._ai_scan_settle_s, from_=0.5, to=3.0, formatter=lambda value: f"{value:.2f}")
        self._add_labeled_scale(tune_box, row=28, text="AI找角度倒计时(秒)", variable=self._ai_angle_countdown_s, from_=0, to=10, formatter=lambda value: f"{int(round(value))}")
        self._add_labeled_scale(tune_box, row=30, text="背景抓取延时(秒)", variable=self._bg_capture_delay_s, from_=0.0, to=3.0, formatter=lambda value: f"{value:.2f}")


        template_box = ttk.LabelFrame(control_panel, text="模板库", padding=8)
        template_box.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self._template_var = tk.StringVar(value="未选择模板")
        self._template_combo = ttk.Combobox(template_box, textvariable=self._template_var, state="readonly")
        self._template_combo.grid(row=0, column=0, sticky="ew")
        self._template_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_template_selected())

        display_box = ttk.LabelFrame(control_panel, text="显示选项", padding=8)
        display_box.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(display_box, text="显示摄像头线", variable=self._show_live_lines).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(display_box, text="显示摄像头框", variable=self._show_live_bbox).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(display_box, text="显示模板线", variable=self._show_template_lines).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(display_box, text="显示模板框", variable=self._show_template_bbox).grid(row=3, column=0, sticky="w")
        ttk.Label(display_box, text="摄像头叠加透明度").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Scale(display_box, from_=0.1, to=1.0, variable=self._live_overlay_alpha, orient="horizontal").grid(row=5, column=0, sticky="ew")
        ttk.Label(display_box, text="模板叠加透明度").grid(row=6, column=0, sticky="w", pady=(6, 0))
        ttk.Scale(display_box, from_=0.1, to=1.0, variable=self._template_overlay_alpha, orient="horizontal").grid(row=7, column=0, sticky="ew")
        ttk.Checkbutton(
            display_box,
            text="镜像摄像头预览与抓拍",
            variable=self._mirror_view_var,
            onvalue=True,
            offvalue=False,
        ).grid(row=8, column=0, sticky="w", pady=(6, 0))

        preview_box = ttk.LabelFrame(control_panel, text="模板预览", padding=8)
        preview_box.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        self._template_preview_label = ttk.Label(preview_box, text="未选择模板", anchor="center")
        self._template_preview_label.grid(row=0, column=0, sticky="ew")

        self._status_var = tk.StringVar(value="模式=手动 | 跟随点=肩部 | 速度=正常 | 渲染=0.0帧/秒 | 检测=0.0帧/秒")
        ttk.Label(control_panel, textvariable=self._status_var, justify="left", wraplength=280).grid(row=7, column=0, sticky="ew", pady=(10, 0))

        cmd_panel = ttk.Frame(self._root, padding=(8, 0, 8, 8))
        cmd_panel.grid(row=1, column=0, columnspan=2, sticky="nsew")
        cmd_panel.grid_columnconfigure(0, weight=1)
        cmd_panel.grid_rowconfigure(0, weight=1)
        self._bottom_pane = ttk.Panedwindow(cmd_panel, orient=tk.HORIZONTAL)
        self._bottom_pane.grid(row=0, column=0, sticky="nsew")

        command_box = ttk.LabelFrame(self._bottom_pane, text="系统命令与日志", padding=6)
        command_box.grid_columnconfigure(0, weight=1)
        command_box.grid_rowconfigure(0, weight=1)
        self._log = tk.Text(command_box, state="disabled")
        self._log.grid(row=0, column=0, sticky="nsew")
        cmd_scroll = ttk.Scrollbar(command_box, orient="vertical", command=self._log.yview)
        cmd_scroll.grid(row=0, column=1, sticky="ns")
        self._log.configure(yscrollcommand=cmd_scroll.set)
        cmd_entry_row = ttk.Frame(command_box)
        cmd_entry_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        cmd_entry_row.grid_columnconfigure(1, weight=1)
        ttk.Label(cmd_entry_row, text="命令>").grid(row=0, column=0, padx=(0, 6))
        self._entry = ttk.Entry(cmd_entry_row)
        self._entry.grid(row=0, column=1, sticky="ew")
        self._entry.bind("<Return>", self._on_enter)
        ttk.Button(cmd_entry_row, text="发送", command=self._send_from_entry).grid(row=0, column=2, padx=(6, 0))

        chat_box = ttk.LabelFrame(self._bottom_pane, text="AI 聊天区", padding=6)
        chat_box.grid_columnconfigure(0, weight=1)
        chat_box.grid_rowconfigure(0, weight=1)
        self._chat_log = tk.Text(chat_box, state="disabled")
        self._chat_log.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ttk.Scrollbar(chat_box, orient="vertical", command=self._chat_log.yview)
        chat_scroll.grid(row=0, column=1, sticky="ns")
        self._chat_log.configure(yscrollcommand=chat_scroll.set)
        chat_entry_row = ttk.Frame(chat_box)
        chat_entry_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        chat_entry_row.grid_columnconfigure(1, weight=1)
        ttk.Label(chat_entry_row, text="聊天>").grid(row=0, column=0, padx=(0, 6))
        self._chat_entry = ttk.Entry(chat_entry_row)
        self._chat_entry.grid(row=0, column=1, sticky="ew")
        self._chat_entry.bind("<Return>", self._on_chat_enter)
        ttk.Button(chat_entry_row, text="发送", command=self._send_chat_from_entry).grid(row=0, column=2, padx=(6, 0))

        self._bottom_pane.add(command_box, weight=1)
        self._bottom_pane.add(chat_box, weight=1)

        self._apply_modern_theme()
        self._bind_mousewheel_to_all(control_panel)

        self._append_log("界面已就绪。输入 help 查看命令。")
        self._append_chat("AI聊天区已就绪。当前为可替换接口，后续可接入真实模型。")
        self._refresh_template_combo()

    def _add_labeled_scale(
        self,
        parent: ttk.LabelFrame | ttk.Frame,
        *,
        row: int,
        text: str,
        variable: tk.Variable,
        from_: float,
        to: float,
        formatter: Callable[[float], str],
    ) -> ttk.Scale:
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=(6, 0))
        value_var = tk.StringVar(master=self._root, value=self._format_scale_value(variable, formatter))
        ttk.Label(parent, textvariable=value_var, width=6, anchor="e").grid(row=row, column=1, sticky="e", pady=(6, 0))
        scale = ttk.Scale(parent, from_=from_, to=to, variable=variable, orient="horizontal")
        scale.grid(row=row + 1, column=0, columnspan=2, sticky="ew")
        variable.trace_add("write", lambda *_args: value_var.set(self._format_scale_value(variable, formatter)))
        return scale

    def _format_scale_value(self, variable: tk.Variable, formatter: Callable[[float], str]) -> str:
        return formatter(float(variable.get()))

    def _apply_modern_theme(self) -> None:
        style = ttk.Style(self._root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        bg_color = "#FFFFFF"
        text_color = "#333333"
        accent_color = "#0078D4"
        hover_bg = "#F3F2F1"
        border_color = "#E1DFDD"
        font_default = ("Microsoft YaHei", 10)
        font_bold = ("Microsoft YaHei", 10, "bold")

        self._root.configure(bg=bg_color)
        
        style.configure(".", background=bg_color, foreground=text_color, font=font_default)
        style.configure("TFrame", background=bg_color)
        
        style.configure("TLabelframe", background=bg_color, bordercolor=border_color, borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", background=bg_color, foreground=accent_color, font=font_bold)
        
        style.configure("TButton", background=bg_color, foreground=text_color, bordercolor=border_color,
                        borderwidth=1, focusthickness=0, padding=(8, 4))
        style.map("TButton", 
                  background=[("active", hover_bg), ("hover", hover_bg)],
                  bordercolor=[("focus", accent_color), ("hover", accent_color)])
                  
        style.configure("TEntry", fieldbackground=bg_color, bordercolor=border_color, borderwidth=1, padding=(4, 2))
        style.map("TEntry", bordercolor=[("focus", accent_color)])
        
        style.configure("TCombobox", fieldbackground=bg_color, background=bg_color, bordercolor=border_color, 
                        arrowcolor=text_color, padding=(4, 2))
        style.map("TCombobox", fieldbackground=[("focus", hover_bg)], bordercolor=[("focus", accent_color)])

        style.configure("TCheckbutton", background=bg_color)
        style.map("TCheckbutton", background=[("active", hover_bg)])
        
        style.configure("Horizontal.TScale", background=bg_color, troughcolor=border_color)
        style.configure("TPanedwindow", background=bg_color)

        self._control_canvas.configure(bg=bg_color, highlightthickness=0)
        if hasattr(self, "_log"):
            self._log.configure(bg=bg_color, fg=text_color, font=("Consolas", 10), 
                                relief="flat", highlightbackground=border_color, highlightthickness=1)
        if hasattr(self, "_chat_log"):
            self._chat_log.configure(bg=bg_color, fg=text_color, font=("Microsoft YaHei", 10), 
                                     relief="flat", highlightbackground=border_color, highlightthickness=1)

    def _bind_mousewheel_to_all(self, root_widget: tk.Widget) -> None:
        def _on_mousewheel(event) -> None:
            if event.delta:
                self._control_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        
        def _bind_recursive(w: tk.Widget) -> None:
            if not isinstance(w, (tk.Text, ttk.Scrollbar, ttk.Combobox)):
                w.bind("<MouseWheel>", _on_mousewheel)
            for child in w.winfo_children():
                _bind_recursive(child)
                
        self._control_canvas.bind("<MouseWheel>", _on_mousewheel)
        _bind_recursive(root_widget)

    def run(self) -> None:
        self._root.mainloop()

    def _schedule_tick(self) -> None:
        self._root.after(max(5, int(self._preview_interval_s * 1000.0)), self._tick)

    def _tick(self) -> None:
        if self._stop_event.is_set():
            return
        frame = self._source.read()
        if frame is not None:
            self._latest_frame = frame.copy()
            now = time.time()

            # 检测是重计算路径，用时间节流降低 CPU/GPU 压力。
            if now - self._last_submit_ts >= self._detector_interval_s:
                self._async_detector.submit(frame)
                self._last_submit_ts = now
                self._detect_rate.mark(now)

            _, vision = self._async_detector.latest()
            self._runtime_state.latest_vision = vision
            detector_error = self._async_detector.last_error
            if detector_error is not None and now - self._last_detector_error_ts > 2.0:
                self._append_log(f"检测线程异常: {detector_error}")
                self._last_detector_error_ts = now
            detection = self._target_selector.select(vision, self._follow_mode)
            stable = reliable_detection(detection, frame.shape)
            self._runtime_state.stable_detection = stable
            self._reliable_detection_streak = 0 if stable is None else self._reliable_detection_streak + 1
            self._gesture_state.set_sensitivity(
                stable_frames=int(self._gesture_stable_frames_var.get()),
                open_hold_min_s=float(self._gesture_open_hold_s_var.get()),
            )
            TemplateComposeEngine.SCORE_THRESHOLD = float(self._compose_score_threshold_var.get())

            compose_feedback = None
            compose_target_override = None
            ready_for_gesture = False
            allow_open_fist_capture = False
            selected_template = self._template_service.get_selected_template()
            if (
                self._mode_manager.mode == ControlMode.SMART_COMPOSE
                and stable is not None
                and selected_template is not None
            ):
                compose_feedback = self._template_engine.evaluate(
                    selected_template,
                    stable,
                    frame.shape,
                    mirror_template=self._is_mirror_view_enabled(),
                    follow_mode=self._follow_mode,
                )
                target_x_norm = float(compose_feedback.target_norm[0])
                target_y_norm = float(compose_feedback.target_norm[1])
                if self._is_mirror_view_enabled():
                    target_x_norm = 1.0 - target_x_norm
                compose_target_override = Point(
                    x=max(0.0, min(float(frame.shape[1] - 1), target_x_norm * frame.shape[1])),
                    y=max(0.0, min(float(frame.shape[0] - 1), target_y_norm * frame.shape[0])),
                )
                self._last_compose_feedback = compose_feedback
                if compose_feedback.ready:
                    if self._ready_since_ts <= 0:
                        self._ready_since_ts = now
                    ready_for_gesture = (now - self._ready_since_ts) >= 0.6
                    allow_open_fist_capture = ready_for_gesture
                else:
                    self._ready_since_ts = 0.0
                    self._gesture_state.reset_pose_capture()
            elif self._mode_manager.mode == ControlMode.SMART_COMPOSE and selected_template is None:
                # Allow gesture capture even when no template is selected.
                self._last_compose_feedback = None
                self._ready_since_ts = 0.0
                allow_open_fist_capture = True
            else:
                self._ready_since_ts = 0.0
                self._last_compose_feedback = None
                allow_open_fist_capture = True

            if (
                self._mode_manager.mode in {ControlMode.AUTO_TRACK, ControlMode.SMART_COMPOSE}
                and stable is not None
                and self._reliable_detection_streak >= RELIABLE_STREAK_FOR_TRACKING
            ):
                should_auto_move = (
                    self._mode_manager.mode == ControlMode.AUTO_TRACK
                    or bool(self._compose_auto_control.get())
                )
                if self._ai_lock_mode_enabled:
                    should_auto_move = False
                if should_auto_move and now >= self._tracking_hold_until:
                    target_override = (
                        compose_target_override
                        if self._mode_manager.mode == ControlMode.SMART_COMPOSE
                        else None
                    )
                    command = self._tracking.compute_command(
                        frame.shape,
                        stable,
                        target_override=target_override,
                    )
                    if command is not None:
                        self._gimbal.move_relative(command.pan_delta, command.tilt_delta, smooth=True)
                        self._tracking_hold_until = now + self._tracking.settle_after_move_s

            if self._ai_orchestrator.background_lock_enabled and stable is not None:
                self._ai_orchestrator.update_lock_fit_score(stable.bbox, frame.shape)
            elif self._ai_orchestrator.background_lock_enabled:
                self._ai_lock_fit_score = 0.0

            if not bool(self._gesture_capture_enabled.get()):
                allow_open_fist_capture = False

            gesture_event = self._gesture_state.update(
                vision.hand_landmarks,
                now,
                ready_for_pose_capture=allow_open_fist_capture,
                force_ok_enabled=bool(self._force_ok_enabled.get()),
            )
            if gesture_event is not None:
                self._pending_capture_metadata = {
                    "source": "gesture",
                    "event": gesture_event,
                    "score": self._last_compose_feedback.total_score if self._last_compose_feedback else None,
                }
                self._pending_capture_deadline = now + float(self._gesture_countdown_s.get())
                self._last_countdown_log_s = -1
                if gesture_event == "force_capture":
                    self._append_log(f"检测到强制拍照手势，{float(self._gesture_countdown_s.get()):.1f}秒后拍照")
                else:
                    if self._mode_manager.mode == ControlMode.SMART_COMPOSE and selected_template is not None:
                        self._append_log(f"姿势达标，手势确认成功，{float(self._gesture_countdown_s.get()):.1f}秒后拍照")
                    else:
                        self._append_log(f"检测到手势拍照，{float(self._gesture_countdown_s.get()):.1f}秒后拍照")

            if self._pending_capture_deadline > 0:
                remain = self._pending_capture_deadline - now
                if remain <= 0:
                    if self._latest_frame is not None:
                        if self._pending_capture_metadata and self._pending_capture_metadata.get("event") == "force_capture":
                            capture_suffix = "强制手势拍照"
                        elif self._mode_manager.mode == ControlMode.SMART_COMPOSE:
                            capture_suffix = "模板手势拍照"
                        else:
                            capture_suffix = "手势拍照"
                        self._capture_and_analyze(
                            frame=self._capture_frame_for_save(self._latest_frame),
                            metadata=self._pending_capture_metadata or {"source": "gesture"},
                            log_on_success=False,
                            suffix=capture_suffix,
                        )
                        self._append_log("倒计时结束，已完成拍照")
                    else:
                        self._append_log("倒计时结束，但当前无可用画面，拍照取消")
                    self._pending_capture_deadline = 0.0
                    self._pending_capture_metadata = None
                    self._last_countdown_log_s = -1
                else:
                    sec = int(remain) + 1
                    max_countdown_log = max(3, int(math.ceil(float(self._gesture_countdown_s.get()))))
                    if sec != self._last_countdown_log_s and sec <= max_countdown_log:
                        self._append_log(f"拍照倒计时: {sec}")
                        self._last_countdown_log_s = sec

            # 渲染单独节流，避免预览帧率把检测拖慢。
            if now - self._last_preview_ts >= self._preview_interval_s:
                target_pt = self._tracking.get_target_point(frame.shape)
                draw_vision = build_draw_vision(vision, stable)
                if not bool(self._show_live_lines.get()):
                    draw_vision.body_skeleton = None
                    draw_vision.face_mesh = None
                if not bool(self._show_live_bbox.get()):
                    draw_vision.person_bbox = None
                    draw_vision.face_bbox = None

                if self._is_mirror_view_enabled():
                    frame = cv2.flip(frame, 1)
                    target_pt = (frame.shape[1] - 1 - target_pt[0], target_pt[1])
                    draw_vision = self._mirror_vision_for_display(draw_vision, frame.shape)

                base_frame = frame.copy()
                live_layer = frame.copy()
                self._overlay.draw(live_layer, self._mode_manager.mode, target_pt, draw_vision)
                frame = self._blend_layer(base_frame, live_layer, float(self._live_overlay_alpha.get()))
                # Draw template overlay after camera mirroring so template pose stays non-mirrored.
                if compose_feedback is not None:
                    template_layer = frame.copy()
                    self._draw_compose_overlay(
                        template_layer,
                        compose_feedback,
                        selected_template,
                        mirror_view=False,
                    )
                    frame = self._blend_layer(frame, template_layer, float(self._template_overlay_alpha.get()))
                if bool(self._show_ai_lock_box.get()) and self._ai_lock_target_box_norm is not None:
                    self._draw_ai_lock_overlay(frame)
                if self._preview_scale < 0.99:
                    w = max(2, int(frame.shape[1] * self._preview_scale))
                    h = max(2, int(frame.shape[0] * self._preview_scale))
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                self._render_frame(frame)
                self._render_rate.mark(now)
                self._last_preview_ts = now
                self._update_status(now)

        self._schedule_tick()

    def _render_frame(self, frame) -> None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = self._pil_image.fromarray(rgb)
        photo = self._pil_image_tk.PhotoImage(image=image)
        self._video_label.configure(image=photo)
        self._video_label.image = photo

    def _update_status(self, now: float) -> None:
        score_text = ""
        if self._last_compose_feedback is not None:
            score_text = f" | 综合分={self._last_compose_feedback.total_score:.1f}"
        compose_ctrl_text = ""
        if self._mode_manager.mode == ControlMode.SMART_COMPOSE:
            compose_ctrl_text = f" | 模板控制={'自动' if self._compose_auto_control.get() else '手动'}"
        ai_lock_text = ""
        if self._ai_lock_mode_enabled:
            ai_lock_text = f" | 锁机位=开 fit={self._ai_lock_fit_score:.2f}"
        ai_scan_text = " | AI扫描中" if self._ai_angle_search_running else ""
        self._status_var.set(
            f"模式={mode_to_text(self._mode_manager.mode)} | 跟随点={follow_to_text(self._follow_mode)} | "
            f"速度={speed_to_text(self._speed_mode)} | 渲染={self._render_rate.value(now):.1f}帧/秒 | "
            f"检测={self._detect_rate.value(now):.1f}帧/秒{score_text}{compose_ctrl_text}{ai_lock_text}{ai_scan_text}"
        )

    def _run_cmd(self, command: str) -> None:
        self._append_log(f"> {command}")
        if command.strip().lower() == "capture":
            if self._latest_frame is None:
                self._append_log("抓拍失败: 当前没有可用画面")
                return
            self._capture_and_analyze(
                frame=self._capture_frame_for_save(self._latest_frame),
                metadata={"source": "manual_command"},
                log_on_success=False,
                suffix="手动拍照",
            )
            self._append_log("已手动抓拍")
            return
        try:
            self._control_service.execute_command(
                command,
                notify=self._append_log,
                set_follow_mode=self._set_follow_mode,
                set_speed_mode=self._set_speed_mode,
                stop_event=self._stop_event,
            )
        except Exception as exc:
            self._append_log(f"命令执行失败: {exc}")

    def _on_enter(self, _event) -> None:
        self._send_from_entry()

    def _send_from_entry(self) -> None:
        cmd = self._entry.get().strip()
        if not cmd:
            return
        self._entry.delete(0, tk.END)
        self._run_cmd(cmd)

    def _on_chat_enter(self, _event) -> None:
        self._send_chat_from_entry()

    def _send_chat_from_entry(self) -> None:
        text = self._chat_entry.get().strip()
        if not text:
            return
        self._chat_entry.delete(0, tk.END)
        self._append_chat(f"你: {text}")
        self._append_chat("AI: 正在思考中...")
        self._run_in_bg(
            lambda: self._ai_assistant.reply(text, context=self._build_ai_context()),
            on_success=lambda reply: self._replace_last_chat_line(f"AI: {reply}"),
            on_error=lambda exc: self._replace_last_chat_line(f"AI: 调用失败: {exc}"),
        )

    def _append_chat(self, message: str) -> None:
        self._chat_log.configure(state="normal")
        self._chat_log.insert("end", message + "\n")
        self._chat_log.see("end")
        self._chat_log.configure(state="disabled")

    def _replace_last_chat_line(self, message: str) -> None:
        self._chat_log.configure(state="normal")
        try:
            self._chat_log.delete("end-2l linestart", "end-1l lineend+1c")
        except Exception:
            pass
        self._chat_log.insert("end", message + "\n")
        self._chat_log.see("end")
        self._chat_log.configure(state="disabled")

    def _build_ai_context(self) -> dict[str, Any]:
        score = self._last_compose_feedback.total_score if self._last_compose_feedback else None
        return {
            "mode": self._mode_manager.mode.value,
            "follow_mode": self._follow_mode,
            "speed_mode": self._speed_mode,
            "compose_score": score,
            "template_id": self._selected_template_id,
            "mirror_view": self._is_mirror_view_enabled(),
        }

    def _capture_and_analyze(
        self,
        *,
        frame,
        metadata: dict[str, Any] | None,
        log_on_success: bool,
        suffix: str = "",
    ) -> None:
        result = self._capture_service.capture(
            frame=frame,
            metadata=metadata,
            suffix=suffix,
            auto_analyze=bool(self._capture_auto_analyze_enabled.get()),
            context=self._build_ai_context(),
        )

        if result.path and log_on_success:
            self._append_log(f"已保存抓拍: {result.path}")

        if result.analysis is not None:
            self._append_chat(
                "AI抓拍分析: "
                f"评分={result.analysis.score:.1f} | {result.analysis.summary} | 建议: "
                f"{'；'.join(result.analysis.suggestions[:3]) if result.analysis.suggestions else '暂无建议'}"
            )
        elif result.analysis_error:
            self._append_log(f"抓拍后自动AI分析失败: {result.analysis_error}")

    def _upload_and_score_photo(self) -> None:
        path = self._pick_image_file("上传照片进行AI评分")
        if not path:
            return
        self._append_log(f"开始AI评分: {path}")
        self._append_chat(f"AI: 正在评分上传图片 {os.path.basename(path)} ...")
        self._run_in_bg(
            lambda: self._ai_assistant.analyze_capture(path, context=self._build_ai_context()),
            on_success=lambda analysis: self._append_chat(
                "AI上传评分: "
                f"评分={analysis.score:.1f} | {analysis.summary} | 建议: "
                f"{'；'.join(analysis.suggestions[:3]) if analysis.suggestions else '暂无建议'}"
            ),
            on_error=lambda exc: self._append_chat(f"AI: 上传评分失败: {exc}"),
        )

    def _upload_and_analyze_background(self) -> None:
        path = self._pick_image_file("上传背景图进行AI分析")
        if not path:
            return
        self._append_log(f"开始背景分析: {path}")
        self._append_chat(f"AI: 正在分析背景图 {os.path.basename(path)} ...")
        self._run_in_bg(
            lambda: self._ai_assistant.analyze_background(path, context=self._build_ai_context()),
            on_success=lambda analysis: self._append_chat(
                "AI背景分析: "
                f"评分={analysis.score:.1f} | {analysis.summary} | "
                f"站位={analysis.placement} | 机位={analysis.camera_angle} | 光线={analysis.lighting} | 建议: "
                f"{'；'.join(analysis.suggestions[:3]) if analysis.suggestions else '暂无建议'}"
            ),
            on_error=lambda exc: self._append_chat(f"AI: 背景分析失败: {exc}"),
        )

    def _analyze_background_and_lock(self) -> None:
        if self._latest_frame is None:
            self._append_log("现场背景锁机位失败: 当前没有可用画面")
            return
        scan_config = {
            "pan_range": float(self._ai_scan_pan_range.get()),
            "tilt_range": float(self._ai_scan_tilt_range.get()),
            "pan_step": float(self._ai_scan_pan_step.get()),
            "tilt_step": float(self._ai_scan_tilt_step.get()),
            "max_candidates": int(self._ai_scan_max_candidates.get()),
            "settle_s": float(self._ai_scan_settle_s.get()),
        }
        delay_s = max(0.0, float(self._bg_capture_delay_s.get()))
        if delay_s > 0.05:
            self._append_log(f"现场背景抓取倒计时: {delay_s:.1f}s")
            self._append_chat(f"AI: 请暂时离开画面，{delay_s:.1f}秒后开始扫描背景")
        else:
            self._append_chat("AI: 正在扫描现场背景并生成锁机位方案...")

        def run_scan():
            return self._ai_orchestrator.start_background_scan_and_lock(
                scan_config, delay_s, self._latest_frame
            )

        def on_success(data):
            from interfaces.ai_assistant import BatchBackgroundPickResult
            result: BatchBackgroundPickResult = data["result"]
            self._append_chat(
                "AI锁机位已启用: "
                f"评分={result.score:.1f} | "
                f"{result.summary} | "
                f"站位={result.placement} | "
                f"机位={result.camera_angle} | "
                f"光线={result.lighting} | "
                f"扫描了{data['num_scanned']}个角度"
            )
            self._append_log("机位已锁定，自动转到最佳背景角度，请按框内站位拍摄")

        self._run_in_bg(
            run_scan,
            on_success=on_success,
            on_error=lambda exc: self._append_chat(f"AI: 锁机位分析失败: {exc}"),
        )

    def _unlock_ai_lock_mode(self) -> None:
        self._ai_orchestrator.unlock_background_lock()
        self._append_log("已解除AI机位锁定")
        self._append_chat("AI: 已解除机位锁定，恢复常规模式")

    def _guide_with_template_background(self) -> None:
        template_path = self._pick_image_file("选择模板图")
        if not template_path:
            return
        background_path = self._pick_image_file("选择背景图")
        if not background_path:
            return
        self._append_log(f"开始模板+背景联合指导: template={template_path} background={background_path}")
        self._append_chat(
            f"AI: 正在做模板+背景联合指导 "
            f"(模板={os.path.basename(template_path)}, 背景={os.path.basename(background_path)}) ..."
        )
        self._run_in_bg(
            lambda: self._ai_assistant.guide_with_template_and_background(
                template_image_path=template_path,
                background_image_path=background_path,
                context=self._build_ai_context(),
            ),
            on_success=lambda guidance: self._append_chat(
                "AI模板背景指导: "
                f"可复刻度={guidance.reproducibility_score:.1f} | 可行性={guidance.feasibility} | {guidance.summary} | "
                f"站位={guidance.placement} | 机位={guidance.camera_angle} | 姿势要点={guidance.pose_tip} | 建议: "
                + ('；'.join(guidance.suggestions[:3]) if guidance.suggestions else '暂无建议')
            ),
            on_error=lambda exc: self._append_chat(f"AI: 模板+背景指导失败: {exc}"),
        )

    def _start_ai_angle_search(self) -> None:
        if self._ai_orchestrator.angle_search_running:
            self._append_log("AI自动找角度正在执行中，请稍候")
            return
        if self._latest_frame is None:
            self._append_log("AI自动找角度失败: 当前没有可用画面")
            return

        scan_config = {
            "pan_range": float(self._ai_scan_pan_range.get()),
            "tilt_range": float(self._ai_scan_tilt_range.get()),
            "pan_step": float(self._ai_scan_pan_step.get()),
            "tilt_step": float(self._ai_scan_tilt_step.get()),
            "max_candidates": int(self._ai_scan_max_candidates.get()),
            "settle_s": float(self._ai_scan_settle_s.get()),
        }
        countdown_s = max(0, min(10, int(self._ai_angle_countdown_s.get())))

        if countdown_s > 0:
            self._append_log(f"AI自动找角度倒计时: {countdown_s}秒后开始")
            self._append_chat(f"AI: {countdown_s}秒后开始自动找角度，请保持姿势和位置尽量不动")
        else:
            self._append_log("开始AI自动找角度，将拍摄全部候选后一次性提交AI分析")
            self._append_chat("AI: 开始自动找角度，请保持姿势和位置尽量不动")

        def run_search():
            if countdown_s > 0:
                self._run_bg_countdown(countdown_s, "AI自动找角度倒计时")
                self._safe_ui_call(lambda: self._append_log("开始AI自动找角度，将拍摄全部候选后一次性提交AI分析"))
                self._safe_ui_call(lambda: self._append_chat("AI: 开始自动找角度，请保持姿势和位置尽量不动"))
            return self._ai_orchestrator.start_angle_search(scan_config, self._latest_frame)

        def on_success(result):
            self._append_chat(
                "AI自动找角度结果: "
                f"最佳评分={result.get('best_score', 0.0):.1f} | "
                f"{result.get('summary', '')} | "
                f"角度=pan {result.get('best_pan', 0.0):.1f}, tilt {result.get('best_tilt', 0.0):.1f} | "
                f"候选数={result.get('num_scanned', 0)} | "
                f"已保存最佳照片"
            )

        def on_error(exc):
            self._append_chat(f"AI: 自动找角度失败: {exc}")
            self._append_log(f"AI自动找角度失败: {exc}")

        self._run_in_bg(run_search, on_success=on_success, on_error=on_error)

    def _run_bg_countdown(self, total_seconds: int, label: str) -> None:
        seconds = max(0, int(total_seconds))
        for remaining in range(seconds, 0, -1):
            if self._stop_event.is_set():
                raise RuntimeError(f"{label}已取消")
            self._safe_ui_call(lambda sec=remaining: self._append_log(f"{label}: {sec}"))
            time.sleep(1.0)

    def _build_scan_offsets(self, scan_config: dict[str, Any]) -> list[tuple[float, float]]:
        pan_range = max(1.0, float(scan_config.get("pan_range", 6.0)))
        tilt_range = max(1.0, float(scan_config.get("tilt_range", 3.0)))
        pan_step = max(0.8, float(scan_config.get("pan_step", 4.0)))
        tilt_step = max(0.8, float(scan_config.get("tilt_step", 3.0)))
        max_candidates = max(2, min(9, int(scan_config.get("max_candidates", 5))))

        pan_values = [0.0]
        p = pan_step
        while p <= pan_range + 1e-6:
            pan_values.extend([p, -p])
            p += pan_step

        tilt_values = [0.0]
        t = tilt_step
        while t <= tilt_range + 1e-6:
            tilt_values.extend([t, -t])
            t += tilt_step

        offsets: list[tuple[float, float]] = []
        for dp in pan_values:
            for dt in tilt_values:
                offsets.append((dp, dt))
        offsets.sort(key=lambda it: (abs(it[0]) + abs(it[1]), abs(it[0]), abs(it[1])))
        dedup: list[tuple[float, float]] = []
        seen: set[tuple[int, int]] = set()
        for dp, dt in offsets:
            key = (int(round(dp * 100)), int(round(dt * 100)))
            if key in seen:
                continue
            seen.add(key)
            dedup.append((dp, dt))
            if len(dedup) >= max_candidates:
                break
        return dedup

    def _run_in_bg(
        self,
        work: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        with self._bg_task_lock:
            self._bg_task_count += 1
        self._set_ai_controls_enabled(False)
        self._bg_task_queue.put((work, on_success, on_error))

    def _bg_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                work, on_success, on_error = self._bg_task_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = work()
            except Exception as exc:
                if on_error is not None:
                    self._safe_ui_call(lambda e=exc: on_error(e))
            else:
                if on_success is not None:
                    self._safe_ui_call(lambda r=result: on_success(r))
            finally:
                with self._bg_task_lock:
                    self._bg_task_count = max(0, self._bg_task_count - 1)
                    no_pending = self._bg_task_count == 0
                if no_pending:
                    self._safe_ui_call(lambda: self._set_ai_controls_enabled(True))
                self._bg_task_queue.task_done()

    def _safe_ui_call(self, callback: Callable[[], None]) -> None:
        try:
            self._root.after(0, callback)
        except Exception:
            pass

    def _set_ai_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in self._ai_control_buttons:
            try:
                btn.configure(state=state)
            except Exception:
                continue

    @staticmethod
    def _pick_image_file(title: str) -> str:
        return filedialog.askopenfilename(
            title=title,
            filetypes=[("Image Files", "*.jpg *.jpeg *.png *.bmp *.webp"), ("All Files", "*.*")],
        )

    def _on_control_frame_configure(self, _event) -> None:
        self._control_canvas.configure(scrollregion=self._control_canvas.bbox("all"))

    def _on_control_canvas_configure(self, event) -> None:
        self._control_canvas.itemconfigure(self._control_window_id, width=event.width)

    def _append_log(self, message: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", message + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _refresh_template_combo(self) -> None:
        templates = self._template_service.list_templates()
        if not templates:
            self._template_combo["values"] = ["未选择模板"]
            self._template_var.set("未选择模板")
            self._template_service.clear_selected_template()
            self._update_template_preview(None)
            return
        values = [f"{t.name} ({t.created_at})" for t in templates]
        self._template_combo["values"] = values
        if self._selected_template_id is None:
            self._template_service.select_template(templates[-1].template_id)
        selected_idx = next(
            (i for i, t in enumerate(templates) if t.template_id == self._selected_template_id),
            len(templates) - 1,
        )
        self._template_var.set(values[selected_idx])
        self._template_service.select_template(templates[selected_idx].template_id)
        template = templates[selected_idx]
        self._update_template_preview(template.image_path)

    def _on_template_selected(self) -> None:
        templates = self._template_service.list_templates()
        current = self._template_var.get().strip()
        for t in templates:
            if current.startswith(f"{t.name} ("):
                self._template_service.select_template(t.template_id)
                self._update_template_preview(t.image_path)
                self._append_log(f"已选择模板: {t.name}")
                return

    def _upload_template(self) -> None:
        path = self._pick_image_file("选择模板照片")
        if not path:
            return

        try:
            profile = self._template_service.import_template(path)
            self._template_service.select_template(profile.template_id)
            self._refresh_template_combo()
            self._append_log(f"模板已添加: {profile.name}")
        except ValueError as exc:
            self._append_log(str(exc))

    def _delete_template(self) -> None:
        if self._selected_template_id is None:
            return

        if self._template_service.delete_template(self._selected_template_id):
            self._refresh_template_combo()
            self._append_log("模板已删除 (未删除原图)")
        else:
            self._append_log("模板删除失败")

    def _update_template_preview(self, image_path: str | None) -> None:
        if image_path is None or not os.path.exists(image_path):
            self._template_preview_photo = None
            self._template_preview_label.configure(text="未选择模板", image="")
            return
        image = cv2.imread(image_path)
        if image is None:
            self._template_preview_photo = None
            self._template_preview_label.configure(text="模板图读取失败", image="")
            return
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil_image = self._pil_image.fromarray(rgb)
        pil_image.thumbnail((260, 160))
        photo = self._pil_image_tk.PhotoImage(image=pil_image)
        self._template_preview_photo = photo
        self._template_preview_label.configure(text="", image=photo)

    def _draw_compose_overlay(self, frame, feedback, template_profile, *, mirror_view: bool = False) -> None:
        h, w = frame.shape[:2]
        tx = int(feedback.target_norm[0] * w)
        ty = int(feedback.target_norm[1] * h)
        cv2.circle(frame, (tx, ty), 11, (0, 255, 255), 2)
        ox = int(tx - feedback.offset_norm[0] * w)
        oy = int(ty - feedback.offset_norm[1] * h)
        cv2.arrowedLine(frame, (ox, oy), (tx, ty), (255, 80, 30), 2, tipLength=0.16)
        if template_profile is not None and template_profile.pose_points:
            projected = self._project_template_pose(template_profile, frame.shape, mirror_view=mirror_view)
            if bool(self._show_template_lines.get()):
                for s, e in TEMPLATE_CORE_EDGES:
                    if s in projected and e in projected:
                        p1 = projected[s]
                        p2 = projected[e]
                        cv2.line(frame, p1, p2, (230, 120, 255), 3)
                for p in projected.values():
                    cv2.circle(frame, p, 3, (245, 170, 255), -1)
            if bool(self._show_template_bbox.get()):
                bbox_norm = getattr(template_profile, "bbox_norm", (0.0, 0.0, 0.0, 0.0))
                if len(bbox_norm) == 4 and bbox_norm[2] > 0 and bbox_norm[3] > 0:
                    bx = float(bbox_norm[0])
                    by = float(bbox_norm[1])
                    bw_norm = float(bbox_norm[2])
                    bh_norm = float(bbox_norm[3])
                    if mirror_view:
                        bx = 1.0 - bx - bw_norm
                    x = int(max(0, min(w - 2, bx * w)))
                    y = int(max(0, min(h - 2, by * h)))
                    bw = int(max(2, min(w - x, bw_norm * w)))
                    bh = int(max(2, min(h - y, bh_norm * h)))
                    cv2.rectangle(frame, (x, y), (x + bw, y + bh), (230, 120, 255), 2)
                    draw_cn_text(frame, "模板姿态", (max(8, x), max(20, y - 6)), (230, 120, 255), 16)
                else:
                    xs = [p[0] for p in projected.values()]
                    ys = [p[1] for p in projected.values()]
                    if xs and ys:
                        cv2.rectangle(frame, (min(xs), min(ys)), (max(xs), max(ys)), (230, 120, 255), 2)
                        draw_cn_text(frame, "模板姿态", (max(8, min(xs)), max(20, min(ys) - 6)), (230, 120, 255), 16)
        cv2.putText(
            frame,
            f"S:{feedback.total_score:.1f} P:{feedback.pose_score:.1f} C:{feedback.compose_score:.1f}",
            (10, max(20, h - 50)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 255),
            2,
        )
        if feedback.messages:
            draw_cn_text(
                frame,
                feedback.messages[0],
                (10, max(20, h - 28)),
                (60, 220, 60) if feedback.ready else (0, 180, 255),
                18,
            )

    def _draw_ai_lock_overlay(self, frame) -> None:
        h, w = frame.shape[:2]
        box = self._ai_lock_target_box_norm
        if box is None:
            return
        bx, by, bw_norm, bh_norm = box
        if self._is_mirror_view_enabled():
            bx = 1.0 - bx - bw_norm
        x = int(max(0, min(w - 2, bx * w)))
        y = int(max(0, min(h - 2, by * h)))
        bw = int(max(2, min(w - x, bw_norm * w)))
        bh = int(max(2, min(h - y, bh_norm * h)))
        fit_ok = self._ai_lock_fit_score >= float(self._ai_lock_fit_threshold.get())
        color = (60, 220, 80) if fit_ok else (0, 200, 255)
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), color, 2)
        status = "已入框" if fit_ok else "请移动到框内"
        draw_cn_text(
            frame,
            f"AI锁机位 {status} | fit={self._ai_lock_fit_score:.2f}",
            (max(8, x), max(22, y - 8)),
            color,
            18,
        )

    def _compute_lock_fit_score(self, bbox, frame_shape: tuple[int, int, int]) -> float:
        box = self._ai_lock_target_box_norm
        if box is None:
            return 0.0
        h, w = frame_shape[:2]
        tx, ty, tw, th = box
        if self._is_mirror_view_enabled():
            tx = 1.0 - tx - tw
        target = (
            tx * w,
            ty * h,
            max(2.0, tw * w),
            max(2.0, th * h),
        )
        live = (float(bbox.x), float(bbox.y), float(max(1, bbox.w)), float(max(1, bbox.h)))
        return self._bbox_iou(target, live)

    @staticmethod
    def _bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
        ax1, ay1, aw, ah = a
        bx1, by1, bw, bh = b
        ax2, ay2 = ax1 + aw, ay1 + ah
        bx2, by2 = bx1 + bw, by1 + bh
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        union = aw * ah + bw * bh - inter
        if union <= 1e-6:
            return 0.0
        return max(0.0, min(1.0, inter / union))

    def _project_template_pose(
        self, template_profile, frame_shape: tuple[int, int, int], *, mirror_view: bool = False
    ) -> dict[int, tuple[int, int]]:
        h, w = frame_shape[:2]
        points: dict[int, tuple[int, int]] = {}
        bbox_norm = getattr(template_profile, "bbox_norm", (0.0, 0.0, 0.0, 0.0))
        src_bbox = getattr(template_profile, "pose_points_bbox", None) or {}
        if src_bbox and len(bbox_norm) == 4 and bbox_norm[2] > 0 and bbox_norm[3] > 0:
            bx = float(bbox_norm[0])
            by = float(bbox_norm[1])
            bw_norm = float(bbox_norm[2])
            bh_norm = float(bbox_norm[3])
            if mirror_view:
                bx = 1.0 - bx - bw_norm
            x0 = bx * w
            y0 = by * h
            bw = bw_norm * w
            bh = bh_norm * h
            for idx, (nx, ny) in src_bbox.items():
                px = 1.0 - float(nx) if mirror_view else float(nx)
                x = int(max(0, min(w - 1, x0 + px * bw)))
                y = int(max(0, min(h - 1, y0 + float(ny) * bh)))
                points[int(idx)] = (x, y)
            return points
        src = getattr(template_profile, "pose_points_image", None) or {}
        if src:
            for idx, (nx, ny) in src.items():
                px = 1.0 - float(nx) if mirror_view else float(nx)
                x = int(max(0, min(w - 1, px * w)))
                y = int(max(0, min(h - 1, float(ny) * h)))
                points[int(idx)] = (x, y)
            return points
        # Backward compatibility for old templates without image-normalized points.
        cx = template_profile.anchor_norm_x * w
        cy = template_profile.anchor_norm_y * h
        approx_size = math.sqrt(max(1.0, template_profile.area_ratio * w * h))
        scale = max(40.0, approx_size * 2.1)
        for idx, (nx, ny) in template_profile.pose_points.items():
            px = -float(nx) if mirror_view else float(nx)
            x = int(max(0, min(w - 1, cx + px * scale)))
            y = int(max(0, min(h - 1, cy + ny * scale)))
            points[int(idx)] = (x, y)
        return points



    @staticmethod
    def _blend_layer(base, overlay, alpha: float):
        a = max(0.0, min(1.0, float(alpha)))
        if a <= 0.001:
            return base
        if a >= 0.999:
            return overlay
        return cv2.addWeighted(overlay, a, base, 1.0 - a, 0)

    def _capture_frame_for_save(self, frame):
        return prepare_capture_frame(frame, self._is_mirror_view_enabled())

    def _is_mirror_view_enabled(self) -> bool:
        return bool(self._mirror_view_var.get())

    @staticmethod
    def _mirror_vision_for_display(vision: VisionResult, frame_shape: tuple[int, int, int]) -> VisionResult:
        h, w = frame_shape[:2]

        def mirror_point(p: Point | None) -> Point | None:
            if p is None:
                return None
            return Point(x=float(w - 1 - p.x), y=float(p.y))

        def mirror_bbox(b):
            if b is None:
                return None
            return type(b)(x=max(0, int(w - b.x - b.w)), y=b.y, w=b.w, h=b.h)

        def mirror_lines(lines):
            if not lines:
                return lines
            return [type(seg)(start=mirror_point(seg.start), end=mirror_point(seg.end)) for seg in lines]

        td = vision.tracking_detection
        mirrored_td = None
        if td is not None:
            mirrored_td = DetectionResult(
                bbox=mirror_bbox(td.bbox),
                confidence=td.confidence,
                label=td.label,
                track_id=td.track_id,
                anchor_point=mirror_point(td.anchor_point),
                pose_landmarks={idx: mirror_point(p) for idx, p in (td.pose_landmarks or {}).items()} or None,
            )

        fd = vision.face_tracking_detection
        mirrored_fd = None
        if fd is not None:
            mirrored_fd = DetectionResult(
                bbox=mirror_bbox(fd.bbox),
                confidence=fd.confidence,
                label=fd.label,
                track_id=fd.track_id,
                anchor_point=mirror_point(fd.anchor_point),
                pose_landmarks=fd.pose_landmarks,
            )

        mirrored_hands = None
        if vision.hand_landmarks is not None:
            mirrored_hands = [[mirror_point(p) for p in hand] for hand in vision.hand_landmarks]

        return VisionResult(
            tracking_detection=mirrored_td,
            tracking_candidates=vision.tracking_candidates,
            face_tracking_detection=mirrored_fd,
            person_bbox=mirror_bbox(vision.person_bbox),
            face_bbox=mirror_bbox(vision.face_bbox),
            body_skeleton=mirror_lines(vision.body_skeleton),
            face_mesh=mirror_lines(vision.face_mesh),
            hand_landmarks=mirrored_hands,
            hand_handedness=vision.hand_handedness,
        )

    def _set_follow_mode(self, mode: str) -> None:
        if mode not in FOLLOW_TEXT:
            return
        self._follow_mode = mode
        self._reliable_detection_streak = 0
        self._follow_var.set(follow_to_text(mode))

    def _set_speed_mode(self, mode: str) -> None:
        if mode not in SPEED_TEXT:
            return
        self._speed_mode = mode
        self._tracking.set_speed_mode(mode)
        self._speed_var.set(speed_to_text(mode))

    def _save_ui_settings_manual(self) -> None:
        self._save_ui_settings(quiet=False)

    def _save_ui_settings(self, *, quiet: bool) -> None:
        payload = {
            "version": 1,
            "selected_template_id": self._selected_template_id,
            "follow_mode": self._follow_mode,
            "speed_mode": self._speed_mode,
            "gesture_capture_enabled": bool(self._gesture_capture_enabled.get()),
            "force_ok_enabled": bool(self._force_ok_enabled.get()),
            "capture_auto_analyze_enabled": bool(self._capture_auto_analyze_enabled.get()),
            "compose_auto_control": bool(self._compose_auto_control.get()),
            "show_ai_lock_box": bool(self._show_ai_lock_box.get()),
            "ai_lock_fit_threshold": float(self._ai_lock_fit_threshold.get()),
            "ai_lock_max_delta": float(self._ai_lock_max_delta.get()),
            "ai_scan_pan_range": float(self._ai_scan_pan_range.get()),
            "ai_scan_tilt_range": float(self._ai_scan_tilt_range.get()),
            "ai_scan_pan_step": float(self._ai_scan_pan_step.get()),
            "ai_scan_tilt_step": float(self._ai_scan_tilt_step.get()),
            "ai_scan_max_candidates": int(self._ai_scan_max_candidates.get()),
            "ai_scan_settle_s": float(self._ai_scan_settle_s.get()),
            "ai_angle_countdown_s": int(self._ai_angle_countdown_s.get()),
            "bg_capture_delay_s": float(self._bg_capture_delay_s.get()),
            "gesture_countdown_s": float(self._gesture_countdown_s.get()),
            "gesture_stable_frames": int(self._gesture_stable_frames_var.get()),
            "gesture_open_hold_s": float(self._gesture_open_hold_s_var.get()),
            "compose_score_threshold": float(self._compose_score_threshold_var.get()),
            "mirror_view": bool(self._mirror_view_var.get()),
            "show_live_lines": bool(self._show_live_lines.get()),
            "show_live_bbox": bool(self._show_live_bbox.get()),
            "show_template_lines": bool(self._show_template_lines.get()),
            "show_template_bbox": bool(self._show_template_bbox.get()),
            "live_overlay_alpha": float(self._live_overlay_alpha.get()),
            "template_overlay_alpha": float(self._template_overlay_alpha.get()),
        }
        try:
            os.makedirs(os.path.dirname(self._ui_settings_path), exist_ok=True)
            with open(self._ui_settings_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if not quiet:
                self._append_log(f"选项已保存: {self._ui_settings_path}")
        except Exception as exc:
            if not quiet:
                self._append_log(f"保存选项失败: {exc}")

    def _load_ui_settings(self) -> None:
        if not os.path.exists(self._ui_settings_path):
            return
        try:
            with open(self._ui_settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
        except Exception as exc:
            self._append_log(f"读取选项失败，已忽略: {exc}")
            return

        bool_vars = {
            "gesture_capture_enabled": self._gesture_capture_enabled,
            "force_ok_enabled": self._force_ok_enabled,
            "capture_auto_analyze_enabled": self._capture_auto_analyze_enabled,
            "compose_auto_control": self._compose_auto_control,
            "show_ai_lock_box": self._show_ai_lock_box,
            "mirror_view": self._mirror_view_var,
            "show_live_lines": self._show_live_lines,
            "show_live_bbox": self._show_live_bbox,
            "show_template_lines": self._show_template_lines,
            "show_template_bbox": self._show_template_bbox,
        }
        for key, var in bool_vars.items():
            if key in data:
                var.set(bool(data[key]))

        float_vars = {
            "ai_lock_fit_threshold": self._ai_lock_fit_threshold,
            "ai_lock_max_delta": self._ai_lock_max_delta,
            "ai_scan_pan_range": self._ai_scan_pan_range,
            "ai_scan_tilt_range": self._ai_scan_tilt_range,
            "ai_scan_pan_step": self._ai_scan_pan_step,
            "ai_scan_tilt_step": self._ai_scan_tilt_step,
            "ai_scan_settle_s": self._ai_scan_settle_s,
            "bg_capture_delay_s": self._bg_capture_delay_s,
            "gesture_countdown_s": self._gesture_countdown_s,
            "gesture_open_hold_s": self._gesture_open_hold_s_var,
            "compose_score_threshold": self._compose_score_threshold_var,
            "live_overlay_alpha": self._live_overlay_alpha,
            "template_overlay_alpha": self._template_overlay_alpha,
        }
        for key, var in float_vars.items():
            if key in data:
                try:
                    var.set(float(data[key]))
                except Exception:
                    pass

        int_vars = {
            "ai_scan_max_candidates": self._ai_scan_max_candidates,
            "ai_angle_countdown_s": self._ai_angle_countdown_s,
            "gesture_stable_frames": self._gesture_stable_frames_var,
        }
        for key, var in int_vars.items():
            if key in data:
                try:
                    var.set(int(round(float(data[key]))))
                except Exception:
                    pass

        follow_mode = data.get("follow_mode")
        if isinstance(follow_mode, str) and follow_mode in FOLLOW_TEXT:
            self._set_follow_mode(follow_mode)
        speed_mode = data.get("speed_mode")
        if isinstance(speed_mode, str) and speed_mode in SPEED_TEXT:
            self._set_speed_mode(speed_mode)

        selected_template_id = data.get("selected_template_id")
        if isinstance(selected_template_id, str) and self._template_library.get(selected_template_id) is not None:
            self._selected_template_id = selected_template_id
        self._refresh_template_combo()
        self._append_log(f"已加载上次选项: {self._ui_settings_path}")

    def _on_close(self) -> None:
        self._save_ui_settings(quiet=True)
        self._stop_event.set()
        self._root.destroy()








def build_servo_driver(args: argparse.Namespace) -> ServoDriver:
    if args.mock_gimbal:
        return MockServoDriver()
    if args.bus_serial_port:
        return TTLBusSerialDriver(
            port=args.bus_serial_port,
            baudrate=args.bus_baudrate,
            move_time_ms=args.bus_move_time_ms,
        )
    try:
        pca9685_address = int(args.pca9685_address, 0)
    except ValueError as exc:
        raise ValueError(f"Invalid --pca9685-address '{args.pca9685_address}'. Example: 0x40") from exc
    return RaspberryPiPWMDriver(pca9685_address=pca9685_address)


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logger = logging.getLogger("main")

    cfg = default_config(args.stream_url)
    cfg.gimbal.use_mock = args.mock_gimbal
    cfg.gimbal.pan.servo_id = args.pan_servo_id
    cfg.gimbal.tilt.servo_id = args.tilt_servo_id
    cfg.detection.detector_fps = max(1.0, args.detector_fps)
    cfg.detection.max_inference_side = max(320, args.max_inference_side)
    cfg.detection.yolo_every_n_frames = max(1, args.yolo_every_n_frames)
    cfg.detection.yolo_bbox_smooth_alpha = min(1.0, max(0.0, args.yolo_bbox_smooth_alpha))
    cfg.detection.enable_face_landmarks = not args.disable_face_landmarks
    cfg.app.ui_refresh_fps = max(1.0, args.preview_fps)
    cfg.app.preview_scale = min(1.0, max(0.2, args.preview_scale))
    cfg.app.enable_overlay = not args.disable_overlay
    cfg.app.show_face_mesh = not args.hide_face_mesh
    cfg.app.show_body_skeleton = not args.hide_body_skeleton
    cfg.video.capture_buffer_size = 1

    if args.rpi_mode:
        cfg.detection.detector_fps = min(cfg.detection.detector_fps, 8.0)
        cfg.app.ui_refresh_fps = min(cfg.app.ui_refresh_fps, 20.0)
        cfg.detection.max_inference_side = min(cfg.detection.max_inference_side, 640)
        cfg.detection.yolo_every_n_frames = max(cfg.detection.yolo_every_n_frames, 3)
        cfg.app.preview_scale = min(cfg.app.preview_scale, 0.85)
        cfg.detection.enable_face_landmarks = False
        cfg.app.show_face_mesh = False

    source = OpenCVVideoSource(cfg.video)
    if args.detector_backend == "mediapipe_yolo":
        detector = MediaPipeYoloVisionDetector(
            cfg.detection,
            yolo_model=args.yolo_model,
            yolo_conf=args.yolo_conf,
            yolo_device=args.yolo_device,
        )
    else:
        detector = MediaPipeVisionDetector(cfg.detection)

    async_detector = AsyncDetector(detector)
    tracking = TrackingController(cfg.tracking, build_target_strategy(TargetPreset.CENTER))
    mode_manager = ModeManager(initial_mode=ControlMode(args.start_mode))
    capture_trigger = LocalFileCaptureTrigger()
    ai_assistant = build_ai_assistant_from_env()
    gimbal = GimbalController(cfg.gimbal, build_servo_driver(args))

    logger.info("Starting Smart Camera Assistant.")
    source.start()
    try:
        GuiApp(
            source=source,
            detector=detector,
            tracking=tracking,
            mode_manager=mode_manager,
            gimbal=gimbal,
            capture_trigger=capture_trigger,
            async_detector=async_detector,
            manual_step_deg=cfg.app.manual_step_deg,
            detector_fps=cfg.detection.detector_fps,
            preview_fps=cfg.app.ui_refresh_fps,
            preview_scale=cfg.app.preview_scale,
            mirror_view=args.mirror_view,
            enable_overlay=cfg.app.enable_overlay,
            show_body_skeleton=cfg.app.show_body_skeleton,
            show_face_mesh=cfg.app.show_face_mesh,
            ai_assistant=ai_assistant,
        ).run()
    finally:
        async_detector.close()
        source.stop()
        gimbal.close()
        logger.info("Exited cleanly.")


if __name__ == "__main__":
    main()
