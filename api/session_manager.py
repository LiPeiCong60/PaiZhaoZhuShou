from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from app_core import RELIABLE_STREAK_FOR_TRACKING, TargetSelector, prepare_capture_frame, reliable_detection
from config import default_config
from detector import AsyncDetector, MediaPipeYoloVisionDetector
from gimbal_controller import GimbalController, MockServoDriver
from interfaces.ai_assistant import build_ai_assistant_from_env
from interfaces.capture_trigger import LocalFileCaptureTrigger
from interfaces.target_strategy import TargetPreset, build_target_strategy
from mode_manager import ControlMode, ModeManager
from repositories.local_template_repository import LocalTemplateRepository
from services.ai_orchestrator import AIOrchestrator
from services.capture_service import CaptureResult, CaptureService
from services.control_service import ControlService
from services.runtime_state import RuntimeState
from services.status_service import StatusService
from services.template_service import TemplateService
from template_compose import TemplateComposeEngine, TemplateLibrary
from tracking_controller import TrackingController
from utils.common_types import Point
from video_source import OpenCVVideoSource


@dataclass(slots=True)
class SessionOpenPayload:
    stream_url: str
    mirror_view: bool = True
    start_mode: str = "MANUAL"


class ApiSessionContext:
    def __init__(self, payload: SessionOpenPayload) -> None:
        self.session_id = f"sess_{uuid.uuid4().hex[:8]}"
        self.mirror_view = bool(payload.mirror_view)
        self._cfg = default_config(payload.stream_url)
        self._cfg.video.capture_buffer_size = 1

        self.runtime_state = RuntimeState()
        self.source = OpenCVVideoSource(self._cfg.video)
        self.detector = MediaPipeYoloVisionDetector(self._cfg.detection)
        self.async_detector = AsyncDetector(self.detector)
        self.tracking = TrackingController(self._cfg.tracking, build_target_strategy(TargetPreset.CENTER))
        self.mode_manager = ModeManager(initial_mode=ControlMode(payload.start_mode))
        self.capture_trigger = LocalFileCaptureTrigger()
        self.ai_assistant = build_ai_assistant_from_env()
        self.gimbal = GimbalController(self._cfg.gimbal, MockServoDriver())

        self._template_library = TemplateLibrary()
        template_repository = LocalTemplateRepository(self._template_library)

        self.control_service = ControlService(
            mode_manager=self.mode_manager,
            tracking=self.tracking,
            gimbal=self.gimbal,
            runtime_state=self.runtime_state,
            manual_step_deg=self._cfg.app.manual_step_deg,
        )
        self.capture_service = CaptureService(
            capture_trigger=self.capture_trigger,
            ai_assistant=self.ai_assistant,
            runtime_state=self.runtime_state,
        )
        self.template_service = TemplateService(
            repository=template_repository,
            runtime_state=self.runtime_state,
        )
        self.ai_orchestrator = AIOrchestrator(
            ai_assistant=self.ai_assistant,
            control_service=self.control_service,
            capture_service=self.capture_service,
            runtime_state=self.runtime_state,
            frame_provider=self.source.read,
            capture_frame_for_save=self._capture_frame_for_save,
        )
        self.status_service = StatusService(
            mode_manager=self.mode_manager,
            runtime_state=self.runtime_state,
        )

        self._target_selector = TargetSelector()
        self._template_engine = TemplateComposeEngine()
        self._detector_interval_s = 1.0 / max(1.0, self._cfg.detection.detector_fps)
        self._last_submit_ts = 0.0
        self._tracking_hold_until = 0.0
        self._stop_event = threading.Event()
        self._frame_thread: threading.Thread | None = None
        self._job_lock = threading.RLock()
        self._angle_job: threading.Thread | None = None
        self._background_job: threading.Thread | None = None
        self.last_angle_search_result: dict[str, Any] | None = None
        self.last_angle_search_error: str | None = None
        self.last_background_lock_result: dict[str, Any] | None = None
        self.last_background_lock_error: str | None = None

    def start(self) -> None:
        self.tracking.set_speed_mode(self.runtime_state.speed_mode)
        self.source.start()
        self._stop_event.clear()
        self._frame_thread = threading.Thread(
            target=self._frame_loop,
            name=f"api-session-{self.session_id}",
            daemon=True,
        )
        self._frame_thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._frame_thread is not None:
            self._frame_thread.join(timeout=2.0)
            self._frame_thread = None
        for worker in (self._angle_job, self._background_job):
            if worker is not None and worker.is_alive():
                worker.join()
        self.async_detector.close()
        self.source.stop()
        self.gimbal.close()

    def get_latest_frame(self):
        frame = self.source.read()
        if frame is not None:
            self.runtime_state.latest_frame = frame.copy()
            return frame
        return self.runtime_state.latest_frame.copy() if self.runtime_state.latest_frame is not None else None

    def build_ai_context(self) -> dict[str, Any]:
        compose_feedback = self.runtime_state.last_compose_feedback
        return {
            "mode": self.mode_manager.mode.value,
            "follow_mode": self.runtime_state.follow_mode,
            "speed_mode": self.runtime_state.speed_mode,
            "compose_score": compose_feedback.total_score if compose_feedback else None,
            "template_id": self.runtime_state.selected_template_id,
            "mirror_view": self.mirror_view,
        }

    def capture_manual(self, *, suffix: str = "手动拍照", auto_analyze: bool = False) -> CaptureResult:
        frame = self.get_latest_frame()
        if frame is None:
            raise RuntimeError("当前没有可用画面")
        save_frame = self._capture_frame_for_save(frame)
        return self.capture_service.capture(
            frame=save_frame,
            metadata={"source": "manual_command"},
            suffix=suffix,
            auto_analyze=auto_analyze,
            context=self.build_ai_context(),
        )

    def start_angle_search_async(self, scan_config: dict[str, Any]) -> None:
        with self._job_lock:
            if self.ai_orchestrator.angle_search_running:
                raise RuntimeError("AI自动找角度正在执行中")
            self.last_angle_search_result = None
            self.last_angle_search_error = None
            self._angle_job = threading.Thread(
                target=self._run_angle_search_job,
                args=(scan_config,),
                name=f"angle-search-{self.session_id}",
                daemon=True,
            )
            self._angle_job.start()

    def start_background_lock_async(self, scan_config: dict[str, Any], delay_s: float) -> None:
        with self._job_lock:
            if self._background_job is not None and self._background_job.is_alive():
                raise RuntimeError("背景锁机位任务正在执行中")
            self.last_background_lock_result = None
            self.last_background_lock_error = None
            self._background_job = threading.Thread(
                target=self._run_background_lock_job,
                args=(scan_config, delay_s),
                name=f"background-lock-{self.session_id}",
                daemon=True,
            )
            self._background_job.start()

    def _run_angle_search_job(self, scan_config: dict[str, Any]) -> None:
        try:
            self.last_angle_search_result = self.ai_orchestrator.start_angle_search(scan_config)
        except Exception as exc:
            self.last_angle_search_error = str(exc)

    def _run_background_lock_job(self, scan_config: dict[str, Any], delay_s: float) -> None:
        try:
            self.last_background_lock_result = self.ai_orchestrator.start_background_lock(
                scan_config,
                delay_s=delay_s,
            )
        except Exception as exc:
            self.last_background_lock_error = str(exc)

    def _frame_loop(self) -> None:
        while not self._stop_event.is_set():
            frame = self.source.read()
            if frame is None:
                time.sleep(0.02)
                continue

            self.runtime_state.latest_frame = frame.copy()
            now = time.time()
            if now - self._last_submit_ts >= self._detector_interval_s:
                self.async_detector.submit(frame)
                self._last_submit_ts = now

            _, vision = self.async_detector.latest()
            self.runtime_state.latest_vision = vision

            detection = self._target_selector.select(vision, self.runtime_state.follow_mode)
            stable = reliable_detection(detection, frame.shape)
            self.runtime_state.stable_detection = stable
            self.runtime_state.reliable_detection_streak = (
                0 if stable is None else self.runtime_state.reliable_detection_streak + 1
            )

            compose_feedback = None
            compose_target_override = None
            selected_template = self.template_service.get_selected_template()
            if (
                self.mode_manager.mode == ControlMode.SMART_COMPOSE
                and stable is not None
                and selected_template is not None
            ):
                compose_feedback = self._template_engine.evaluate(
                    selected_template,
                    stable,
                    frame.shape,
                    mirror_template=self.mirror_view,
                    follow_mode=self.runtime_state.follow_mode,
                )
                target_x_norm = float(compose_feedback.target_norm[0])
                target_y_norm = float(compose_feedback.target_norm[1])
                if self.mirror_view:
                    target_x_norm = 1.0 - target_x_norm
                compose_target_override = Point(
                    x=max(0.0, min(float(frame.shape[1] - 1), target_x_norm * frame.shape[1])),
                    y=max(0.0, min(float(frame.shape[0] - 1), target_y_norm * frame.shape[0])),
                )
                self.runtime_state.last_compose_feedback = compose_feedback
                if compose_feedback.ready:
                    if self.runtime_state.ready_since_ts <= 0:
                        self.runtime_state.ready_since_ts = now
                else:
                    self.runtime_state.ready_since_ts = 0.0
            else:
                self.runtime_state.last_compose_feedback = None
                self.runtime_state.ready_since_ts = 0.0

            if (
                self.mode_manager.mode in {ControlMode.AUTO_TRACK, ControlMode.SMART_COMPOSE}
                and stable is not None
                and self.runtime_state.reliable_detection_streak >= RELIABLE_STREAK_FOR_TRACKING
            ):
                should_auto_move = not self.runtime_state.ai_lock_mode_enabled
                if should_auto_move and now >= self._tracking_hold_until:
                    target_override = (
                        compose_target_override
                        if self.mode_manager.mode == ControlMode.SMART_COMPOSE
                        else None
                    )
                    command = self.tracking.compute_command(
                        frame.shape,
                        stable,
                        target_override=target_override,
                    )
                    if command is not None:
                        self.gimbal.move_relative(command.pan_delta, command.tilt_delta, smooth=True)
                        self._tracking_hold_until = now + self.tracking.settle_after_move_s

            if self.runtime_state.ai_lock_mode_enabled and stable is not None:
                self.ai_orchestrator.update_lock_fit_score(stable.bbox, frame.shape)
            elif self.runtime_state.ai_lock_mode_enabled:
                self.runtime_state.ai_lock_fit_score = 0.0

            time.sleep(0.01)

    def _capture_frame_for_save(self, frame):
        return prepare_capture_frame(frame, self.mirror_view)


class SessionManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session: ApiSessionContext | None = None

    def open_session(self, payload: SessionOpenPayload) -> ApiSessionContext:
        with self._lock:
            if self._session is not None:
                self._session.close()
            session = ApiSessionContext(payload)
            session.start()
            self._session = session
            return session

    def current_session(self) -> ApiSessionContext | None:
        with self._lock:
            return self._session

    def close_session(self) -> bool:
        with self._lock:
            if self._session is None:
                return False
            self._session.close()
            self._session = None
            return True


session_manager = SessionManager()
