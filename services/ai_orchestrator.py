"""
AI编排服务模块
负责AI相关的复杂操作流程，如自动找角度、背景扫描锁机位等
"""

from __future__ import annotations

import tempfile
import time
from typing import Any, Callable, Dict, Optional

from interfaces.ai_assistant import AIPhotoAssistant, BatchBackgroundPickResult
from services.capture_service import CaptureService
from services.control_service import ControlService
from services.runtime_state import RuntimeState


class AIOrchestrator:
    """AI编排服务类，负责协调复杂的AI操作流程"""

    def __init__(
        self,
        ai_assistant: AIPhotoAssistant,
        control_service: ControlService,
        capture_service: CaptureService,
        runtime_state: RuntimeState,
        frame_provider: Optional[Callable[[], Any]] = None,
        capture_frame_for_save: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._ai_assistant = ai_assistant
        self._control_service = control_service
        self._capture_service = capture_service
        self._runtime_state = runtime_state
        self._frame_provider = frame_provider
        self._capture_frame_for_save = capture_frame_for_save or (lambda frame: frame.copy())

    @property
    def angle_search_running(self) -> bool:
        """获取角度搜索状态"""
        return self._runtime_state.ai_angle_search_running

    @property
    def background_lock_enabled(self) -> bool:
        """获取背景锁状态"""
        return self._runtime_state.ai_lock_mode_enabled

    @property
    def background_lock_target_box(self) -> tuple[float, float, float, float] | None:
        """获取背景锁推荐站位框"""
        return self._runtime_state.ai_lock_target_box_norm

    @property
    def background_lock_fit_score(self) -> float:
        """获取背景锁匹配分数"""
        return self._runtime_state.ai_lock_fit_score

    def start_angle_search(
        self,
        scan_config: Dict[str, Any],
        latest_frame=None,
    ) -> Dict[str, Any]:
        """开始自动找角度"""
        if self._runtime_state.ai_angle_search_running:
            raise RuntimeError("AI自动找角度正在执行中")
        if self._get_frame(fallback=latest_frame) is None:
            raise ValueError("当前没有可用画面")

        self._runtime_state.ai_angle_search_running = True
        try:
            return self._run_batch_angle_search(scan_config, latest_frame)
        finally:
            self._runtime_state.ai_angle_search_running = False

    def start_background_lock(
        self,
        scan_config: Dict[str, Any],
        delay_s: float = 0.0,
        latest_frame=None,
    ) -> Dict[str, Any]:
        """开始背景扫描并锁机位"""
        if self._get_frame(fallback=latest_frame) is None:
            raise ValueError("当前没有可用画面")
        result = self._run_batch_background_scan(scan_config, delay_s, latest_frame)
        return self._apply_background_lock(result)

    def start_background_scan_and_lock(
        self,
        scan_config: Dict[str, Any],
        delay_s: float,
        latest_frame=None,
    ) -> Dict[str, Any]:
        """兼容旧调用名"""
        return self.start_background_lock(scan_config, delay_s=delay_s, latest_frame=latest_frame)

    def unlock_background_lock(self) -> None:
        """解除背景锁"""
        self._runtime_state.ai_lock_mode_enabled = False
        self._runtime_state.ai_lock_target_box_norm = None
        self._runtime_state.ai_lock_fit_score = 0.0

    def update_lock_fit_score(self, bbox, frame_shape) -> float:
        """更新锁机位匹配分数"""
        if not self._runtime_state.ai_lock_mode_enabled:
            self._runtime_state.ai_lock_fit_score = 0.0
            return 0.0

        box = self._runtime_state.ai_lock_target_box_norm
        if box is None:
            self._runtime_state.ai_lock_fit_score = 0.0
            return 0.0

        h, w = frame_shape[:2]
        tx, ty, tw, th = box

        target = (
            tx * w,
            ty * h,
            max(2.0, tw * w),
            max(2.0, th * h),
        )
        live = (float(bbox.x), float(bbox.y), float(max(1, bbox.w)), float(max(1, bbox.h)))

        ax1, ay1, aw, ah = target
        bx1, by1, bw, bh = live
        ax2, ay2 = ax1 + aw, ay1 + ah
        bx2, by2 = bx1 + bw, by1 + bh
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
        inter = iw * ih
        union = aw * ah + bw * bh - inter
        score = max(0.0, min(1.0, inter / union)) if union > 1e-6 else 0.0

        self._runtime_state.ai_lock_fit_score = score
        return score

    def _run_batch_angle_search(
        self,
        scan_config: Dict[str, Any],
        latest_frame=None,
    ) -> Dict[str, Any]:
        """执行批量角度搜索"""
        import cv2
        import os

        start_pan, start_tilt = self._control_service.get_current_angles(prefer_feedback=True)
        scan_offsets = self._build_scan_offsets(scan_config)
        settle_s = max(0.5, float(scan_config.get("settle_s", 1.0)))

        candidates = []
        tmp_paths = []

        try:
            for dpan, dtilt in scan_offsets:
                pan = start_pan + dpan
                tilt = start_tilt + dtilt
                self._control_service.set_absolute(pan, tilt, smooth=True)
                time.sleep(settle_s)

                frame = self._get_frame(fallback=latest_frame)
                if frame is None:
                    continue

                with tempfile.NamedTemporaryFile(prefix="ai_scan_", suffix=".jpg", delete=False) as tmp:
                    tmp_path = tmp.name

                if not cv2.imwrite(tmp_path, frame):
                    continue

                tmp_paths.append(tmp_path)
                candidates.append({"pan": pan, "tilt": tilt, "path": tmp_path, "frame": frame})

            if not candidates:
                self._control_service.set_absolute(start_pan, start_tilt, smooth=True)
                raise RuntimeError("无有效候选角度")

            image_paths = [c["path"] for c in candidates]
            pick_result = self._ai_assistant.pick_best_from_batch(image_paths)
            best = candidates[pick_result.best_index]

            self._control_service.set_absolute(best["pan"], best["tilt"], smooth=True)
            time.sleep(settle_s)

            save_frame = self._capture_frame_for_save(best["frame"])
            capture_result = self._capture_service.capture(
                frame=save_frame,
                metadata={
                    "source": "ai_angle_search_best",
                    "score": pick_result.score,
                    "pan": best["pan"],
                    "tilt": best["tilt"],
                },
                suffix="AI分析最佳结果",
            )

            return {
                "best_score": float(pick_result.score),
                "summary": pick_result.summary,
                "best_pan": float(best["pan"]),
                "best_tilt": float(best["tilt"]),
                "num_scanned": len(candidates),
                "capture_path": capture_result.path,
            }
        finally:
            for p in tmp_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _run_batch_background_scan(
        self,
        scan_config: Dict[str, Any],
        delay_s: float,
        latest_frame=None,
    ) -> Dict[str, Any]:
        """执行批量背景扫描"""
        import cv2
        import os

        if delay_s > 0.05:
            time.sleep(delay_s)

        start_pan, start_tilt = self._control_service.get_current_angles(prefer_feedback=True)
        scan_offsets = self._build_scan_offsets(scan_config)
        settle_s = max(0.5, float(scan_config.get("settle_s", 1.0)))

        candidates = []
        tmp_paths = []

        try:
            for dpan, dtilt in scan_offsets:
                pan = start_pan + dpan
                tilt = start_tilt + dtilt
                self._control_service.set_absolute(pan, tilt, smooth=True)
                time.sleep(settle_s)

                frame = self._get_frame(fallback=latest_frame)
                if frame is None:
                    continue

                with tempfile.NamedTemporaryFile(prefix="bg_scan_", suffix=".jpg", delete=False) as tmp:
                    tmp_path = tmp.name

                if not cv2.imwrite(tmp_path, frame):
                    continue

                tmp_paths.append(tmp_path)
                candidates.append({"pan": pan, "tilt": tilt, "path": tmp_path, "frame": frame})

            if not candidates:
                self._control_service.set_absolute(start_pan, start_tilt, smooth=True)
                raise RuntimeError("无有效候选背景")

            result = self._ai_assistant.pick_best_background_from_batch([c["path"] for c in candidates])
            best = candidates[result.best_index]

            self._control_service.set_absolute(best["pan"], best["tilt"], smooth=True)
            time.sleep(settle_s)

            return {
                "result": result,
                "best_pan": best["pan"],
                "best_tilt": best["tilt"],
                "num_scanned": len(candidates),
            }
        finally:
            for p in tmp_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _apply_background_lock(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """应用背景锁"""
        result: BatchBackgroundPickResult = data["result"]
        max_delta = 12.0

        pan_delta = max(-max_delta, min(max_delta, result.recommended_pan_delta))
        tilt_delta = max(-max_delta, min(max_delta, result.recommended_tilt_delta))

        curr_pan, curr_tilt = self._control_service.get_current_angles(prefer_feedback=True)
        self._control_service.set_absolute(curr_pan + pan_delta, curr_tilt + tilt_delta, smooth=True)

        box = result.target_box_norm
        if isinstance(box, tuple) and len(box) == 4:
            self._runtime_state.ai_lock_target_box_norm = (
                float(box[0]),
                float(box[1]),
                float(box[2]),
                float(box[3]),
            )
        else:
            self._runtime_state.ai_lock_target_box_norm = (0.38, 0.18, 0.24, 0.66)

        self._runtime_state.ai_lock_mode_enabled = True
        self._runtime_state.ai_lock_fit_score = 0.0
        return data

    def _build_scan_offsets(self, scan_config: Dict[str, Any]) -> list[tuple[float, float]]:
        """构建扫描偏移量"""
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

        offsets = []
        for dp in pan_values:
            for dt in tilt_values:
                offsets.append((dp, dt))

        offsets.sort(key=lambda it: (abs(it[0]) + abs(it[1]), abs(it[0]), abs(it[1])))

        dedup = []
        seen = set()
        for dp, dt in offsets:
            key = (int(round(dp * 100)), int(round(dt * 100)))
            if key in seen:
                continue
            seen.add(key)
            dedup.append((dp, dt))
            if len(dedup) >= max_candidates:
                break

        return dedup

    def _get_frame(self, fallback=None):
        if self._frame_provider is not None:
            frame = self._frame_provider()
            if frame is not None:
                return frame.copy() if hasattr(frame, "copy") else frame
        if fallback is not None:
            return fallback.copy() if hasattr(fallback, "copy") else fallback
        return None
