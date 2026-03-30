from __future__ import annotations

import queue
import threading
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from shutil import copyfileobj

import numpy as np

from config import DetectionConfig
from utils.common_types import BBox, DetectionResult, LineSegment, Point, VisionResult

POSE_EDGES: list[tuple[int, int]] = [
    (11, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (11, 23),
    (12, 24),
    (23, 24),
    (23, 25),
    (25, 27),
    (24, 26),
    (26, 28),
    (27, 31),
    (28, 32),
]

POSE_KEYPOINT_IDS: tuple[int, ...] = (11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)

FACE_POLYLINES: list[list[int]] = [
    [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109, 10],  # face oval
    [33, 160, 158, 133, 153, 144, 33],  # left eye
    [263, 387, 385, 362, 380, 373, 263],  # right eye
    [70, 63, 105, 66, 107],  # left eyebrow
    [336, 296, 334, 293, 300],  # right eyebrow
    [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95, 78, 61],  # lips
    [168, 6, 197, 195, 5, 4, 1, 19, 94, 2, 168],  # nose ridge and tip
]


class VisionDetector(ABC):
    @abstractmethod
    def detect(self, frame: np.ndarray) -> VisionResult:
        raise NotImplementedError


class MediaPipeVisionDetector(VisionDetector):
    """
    MediaPipe Tasks API detector for modern mediapipe package.
    Produces body skeleton, facial features, person/face bbox and shoulder anchor.
    """

    def __init__(self, config: DetectionConfig) -> None:
        self._config = config
        import logging

        self._logger = logging.getLogger(self.__class__.__name__)
        self._use_solutions_fallback = False
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency mediapipe. Please run: pip install mediapipe"
            ) from exc

        self._mp = mp
        self._mp_python = mp_python
        self._vision = vision
        try:
            model_dir = Path(".cache") / "mediapipe_models"
            model_dir.mkdir(parents=True, exist_ok=True)
            pose_model = self._ensure_model(
                model_dir / "pose_landmarker_lite.task",
                "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
            )
            pose_options = vision.PoseLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(pose_model)),
                running_mode=vision.RunningMode.IMAGE,
                num_poses=3,
                min_pose_detection_confidence=0.5,
                min_pose_presence_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self._pose = vision.PoseLandmarker.create_from_options(pose_options)
            hand_model = self._ensure_model(
                model_dir / "hand_landmarker.task",
                "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task",
            )
            hand_options = vision.HandLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(hand_model)),
                running_mode=vision.RunningMode.IMAGE,
                num_hands=2,
                min_hand_detection_confidence=0.45,
                min_hand_presence_confidence=0.45,
                min_tracking_confidence=0.45,
            )
            try:
                self._hands = vision.HandLandmarker.create_from_options(hand_options)
            except Exception as exc:
                self._hands = None
                self._logger.warning("HandLandmarker init failed, hand gesture features disabled: %s", exc)
            self._face = None
            if self._config.enable_face_landmarks:
                face_model = self._ensure_model(
                    model_dir / "face_landmarker.task",
                    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task",
                )
                face_options = vision.FaceLandmarkerOptions(
                    base_options=mp_python.BaseOptions(model_asset_path=str(face_model)),
                    running_mode=vision.RunningMode.IMAGE,
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False,
                )
                self._face = vision.FaceLandmarker.create_from_options(face_options)
        except Exception as exc:
            self._logger.warning("MediaPipe Tasks init failed, fallback to solutions API: %s", exc)
            self._use_solutions_fallback = True
            self._setup_solutions_fallback()

    def detect(self, frame: np.ndarray) -> VisionResult:
        if self._use_solutions_fallback:
            return self._detect_with_solutions(frame)
        import cv2

        frame_for_detect, scale = self._resize_for_inference(frame, self._config.max_inference_side)
        rgb = cv2.cvtColor(frame_for_detect, cv2.COLOR_BGR2RGB)
        mp_img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        pose_res = self._pose.detect(mp_img)
        face_res = self._face.detect(mp_img) if self._face is not None else None
        hand_res = self._hands.detect(mp_img) if self._hands is not None else None

        h, w = frame_for_detect.shape[:2]
        candidates, body_lines = self._pose_candidates_and_lines(pose_res, w, h)
        person_bbox = candidates[0].bbox if candidates else None
        face_bbox = self._bbox_from_face(face_res, w, h)
        face_lines = self._face_lines(face_res, w, h)
        hand_landmarks, hand_handedness = self._hand_points(hand_res, w, h)
        if scale < 1.0:
            inv = 1.0 / scale
            person_bbox = self._scale_bbox(person_bbox, inv, frame.shape[1], frame.shape[0])
            face_bbox = self._scale_bbox(face_bbox, inv, frame.shape[1], frame.shape[0])
            body_lines = self._scale_lines(body_lines, inv, frame.shape[1], frame.shape[0])
            face_lines = self._scale_lines(face_lines, inv, frame.shape[1], frame.shape[0])
            for candidate in candidates:
                candidate.bbox = self._scale_bbox(
                    candidate.bbox, inv, frame.shape[1], frame.shape[0]
                ) or candidate.bbox
                candidate.anchor_point = self._scale_point(
                    candidate.anchor_point, inv, frame.shape[1], frame.shape[0]
                )
                if candidate.pose_landmarks:
                    candidate.pose_landmarks = {
                        idx: self._scale_point(p, inv, frame.shape[1], frame.shape[0]) or p
                        for idx, p in candidate.pose_landmarks.items()
                    }
            scaled_hands: list[list[Point]] = []
            for hand in hand_landmarks:
                scaled_hands.append(
                    [
                        self._scale_point(p, inv, frame.shape[1], frame.shape[0]) or p
                        for p in hand
                    ]
                )
            hand_landmarks = scaled_hands

        tracking_detection: DetectionResult | None = candidates[0] if candidates else None
        face_tracking_detection: DetectionResult | None = None
        if face_bbox is not None:
            face_tracking_detection = DetectionResult(
                bbox=face_bbox,
                confidence=0.88,
                label="face_center",
                anchor_point=face_bbox.center,
            )

        return VisionResult(
            tracking_detection=tracking_detection,
            tracking_candidates=candidates,
            face_tracking_detection=face_tracking_detection,
            person_bbox=person_bbox,
            face_bbox=face_bbox,
            body_skeleton=body_lines,
            face_mesh=face_lines,
            hand_landmarks=hand_landmarks,
            hand_handedness=hand_handedness,
        )

    def _setup_solutions_fallback(self) -> None:
        mp = self._mp
        self._sol_pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            enable_segmentation=False,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._sol_hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=2,
            min_detection_confidence=0.45,
            min_tracking_confidence=0.45,
        )
        self._sol_face = None
        if self._config.enable_face_landmarks:
            self._sol_face = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,
                refine_landmarks=False,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )

    def _detect_with_solutions(self, frame: np.ndarray) -> VisionResult:
        import cv2

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pose_res = self._sol_pose.process(rgb)
        hand_res = self._sol_hands.process(rgb)
        face_res = self._sol_face.process(rgb) if self._sol_face is not None else None
        h, w = frame.shape[:2]

        candidates: list[DetectionResult] = []
        body_lines: list[LineSegment] = []
        if pose_res.pose_landmarks:
            lm = pose_res.pose_landmarks.landmark
            bbox = self._bbox_from_landmarks(lm, w, h)
            if bbox is not None:
                pose_landmarks: dict[int, Point] = {}
                for idx in POSE_KEYPOINT_IDS:
                    if idx >= len(lm):
                        continue
                    pose_landmarks[idx] = Point(x=lm[idx].x * w, y=lm[idx].y * h)
                anchor = None
                if len(lm) > 12:
                    anchor = Point(
                        x=(lm[11].x + lm[12].x) * 0.5 * w,
                        y=(lm[11].y + lm[12].y) * 0.5 * h,
                    )
                candidates.append(
                    DetectionResult(
                        bbox=bbox,
                        confidence=0.9,
                        label="person_pose",
                        anchor_point=anchor,
                        pose_landmarks=pose_landmarks or None,
                    )
                )
            for s, e in POSE_EDGES:
                if s >= len(lm) or e >= len(lm):
                    continue
                p1 = Point(x=lm[s].x * w, y=lm[s].y * h)
                p2 = Point(x=lm[e].x * w, y=lm[e].y * h)
                body_lines.append(LineSegment(start=p1, end=p2))

        face_bbox = None
        face_lines: list[LineSegment] = []
        if face_res is not None and face_res.multi_face_landmarks:
            lms = face_res.multi_face_landmarks[0].landmark
            xs = [int(p.x * w) for p in lms]
            ys = [int(p.y * h) for p in lms]
            x1, x2 = max(0, min(xs)), min(w - 1, max(xs))
            y1, y2 = max(0, min(ys)), min(h - 1, max(ys))
            face_bbox = BBox(x=x1, y=y1, w=max(2, x2 - x1), h=max(2, y2 - y1))
            for poly in FACE_POLYLINES:
                for i in range(1, len(poly)):
                    s = poly[i - 1]
                    e = poly[i]
                    if s >= len(lms) or e >= len(lms):
                        continue
                    face_lines.append(
                        LineSegment(
                            start=Point(x=lms[s].x * w, y=lms[s].y * h),
                            end=Point(x=lms[e].x * w, y=lms[e].y * h),
                        )
                    )

        hand_points: list[list[Point]] = []
        hand_handedness: list[str] = []
        if hand_res.multi_hand_landmarks:
            for i, hand in enumerate(hand_res.multi_hand_landmarks):
                hand_points.append([Point(x=p.x * w, y=p.y * h) for p in hand.landmark])
                label = "unknown"
                if hand_res.multi_handedness and i < len(hand_res.multi_handedness):
                    cls = hand_res.multi_handedness[i].classification
                    if cls:
                        label = cls[0].label.lower()
                hand_handedness.append(label)

        tracking_detection = candidates[0] if candidates else None
        face_tracking_detection = None
        if face_bbox is not None:
            face_tracking_detection = DetectionResult(
                bbox=face_bbox,
                confidence=0.85,
                label="face_center",
                anchor_point=face_bbox.center,
            )
        return VisionResult(
            tracking_detection=tracking_detection,
            tracking_candidates=candidates,
            face_tracking_detection=face_tracking_detection,
            person_bbox=tracking_detection.bbox if tracking_detection else None,
            face_bbox=face_bbox,
            body_skeleton=body_lines,
            face_mesh=face_lines,
            hand_landmarks=hand_points,
            hand_handedness=hand_handedness,
        )

    @staticmethod
    def _resize_for_inference(frame: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
        import cv2

        h, w = frame.shape[:2]
        if max_side <= 0 or max(h, w) <= max_side:
            return frame, 1.0
        scale = float(max_side) / float(max(h, w))
        nw = max(2, int(w * scale))
        nh = max(2, int(h * scale))
        return cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA), scale

    @staticmethod
    def _scale_bbox(bbox: BBox | None, factor: float, max_w: int, max_h: int) -> BBox | None:
        if bbox is None:
            return None
        x = max(0, min(max_w - 2, int(bbox.x * factor)))
        y = max(0, min(max_h - 2, int(bbox.y * factor)))
        w = max(2, int(bbox.w * factor))
        h = max(2, int(bbox.h * factor))
        return BBox(x=x, y=y, w=min(max_w - x, w), h=min(max_h - y, h))

    @staticmethod
    def _scale_point(p: Point | None, factor: float, max_w: int, max_h: int) -> Point | None:
        if p is None:
            return None
        return Point(
            x=max(0.0, min(float(max_w - 1), p.x * factor)),
            y=max(0.0, min(float(max_h - 1), p.y * factor)),
        )

    @staticmethod
    def _scale_lines(
        lines: list[LineSegment] | None, factor: float, max_w: int, max_h: int
    ) -> list[LineSegment]:
        if not lines:
            return []
        scaled: list[LineSegment] = []
        for seg in lines:
            p1 = MediaPipeVisionDetector._scale_point(seg.start, factor, max_w, max_h)
            p2 = MediaPipeVisionDetector._scale_point(seg.end, factor, max_w, max_h)
            if p1 is None or p2 is None:
                continue
            scaled.append(LineSegment(start=p1, end=p2))
        return scaled

    @staticmethod
    def _ensure_model(path: Path, url: str) -> Path:
        if MediaPipeVisionDetector._is_valid_task_file(path, try_repair=True):
            return path
        tmp = path.with_suffix(path.suffix + ".tmp")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        MediaPipeVisionDetector._download_file(url, tmp, timeout_s=30.0)
        if not MediaPipeVisionDetector._is_valid_task_file(tmp, try_repair=True):
            raise RuntimeError(f"Downloaded model is invalid: {tmp}")
        tmp.replace(path)
        return path

    @staticmethod
    def _download_file(url: str, dst: Path, timeout_s: float = 30.0) -> None:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            with dst.open("wb") as f:
                copyfileobj(resp, f, length=1024 * 256)

    @staticmethod
    def _is_valid_task_file(path: Path, try_repair: bool = False) -> bool:
        if not path.exists() or path.stat().st_size <= 0:
            return False
        try:
            with path.open("rb") as fh:
                header = fh.read(8)
            # MediaPipe task files are zip archives and should start with PK.
            if not header.startswith(b"PK"):
                if try_repair and MediaPipeVisionDetector._repair_task_file(path):
                    return zipfile.is_zipfile(path)
                return False
            return zipfile.is_zipfile(path)
        except OSError:
            return False

    @staticmethod
    def _repair_task_file(path: Path) -> bool:
        """Repairs task files that have a small binary prefix before the PK header."""
        try:
            data = path.read_bytes()
        except OSError:
            return False
        sig = b"PK\x03\x04"
        idx = data.find(sig)
        if idx <= 0 or idx > 8:
            return False
        repaired = data[idx:]
        try:
            path.write_bytes(repaired)
        except OSError:
            return False
        return True

    def _bbox_from_landmarks(self, landmarks, w: int, h: int) -> BBox | None:
        xs: list[int] = []
        ys: list[int] = []
        for lm in landmarks:
            if lm.visibility < 0.45:
                continue
            x = int(lm.x * w)
            y = int(lm.y * h)
            if 0 <= x < w and 0 <= y < h:
                xs.append(x)
                ys.append(y)
        if len(xs) < 6:
            return None
        x1, x2 = max(0, min(xs)), min(w - 1, max(xs))
        y1, y2 = max(0, min(ys)), min(h - 1, max(ys))
        pad_x = int((x2 - x1) * 0.12)
        pad_y = int((y2 - y1) * 0.12)
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(w - 1, x2 + pad_x)
        y2 = min(h - 1, y2 + pad_y)
        return BBox(x=x1, y=y1, w=max(2, x2 - x1), h=max(2, y2 - y1))

    def _bbox_from_face(self, face_res, w: int, h: int) -> BBox | None:
        if face_res is None:
            return None
        if not face_res.face_landmarks:
            return None
        landmarks = face_res.face_landmarks[0]
        xs = [int(lm.x * w) for lm in landmarks]
        ys = [int(lm.y * h) for lm in landmarks]
        x1, x2 = max(0, min(xs)), min(w - 1, max(xs))
        y1, y2 = max(0, min(ys)), min(h - 1, max(ys))
        return BBox(x=x1, y=y1, w=max(2, x2 - x1), h=max(2, y2 - y1))

    def _pose_candidates_and_lines(
        self, pose_res, w: int, h: int
    ) -> tuple[list[DetectionResult], list[LineSegment]]:
        if not pose_res.pose_landmarks:
            return [], []
        candidates: list[DetectionResult] = []

        lm_first = pose_res.pose_landmarks[0]
        body_lines: list[LineSegment] = []
        for s, e in POSE_EDGES:
            if lm_first[s].visibility < 0.45 or lm_first[e].visibility < 0.45:
                continue
            p1 = Point(x=lm_first[s].x * w, y=lm_first[s].y * h)
            p2 = Point(x=lm_first[e].x * w, y=lm_first[e].y * h)
            body_lines.append(LineSegment(start=p1, end=p2))

        for landmarks in pose_res.pose_landmarks:
            bbox = self._bbox_from_landmarks(landmarks, w, h)
            if bbox is None:
                continue
            l_sh = landmarks[11]
            r_sh = landmarks[12]
            anchor: Point | None = None
            if l_sh.visibility >= 0.45 and r_sh.visibility >= 0.45:
                anchor = Point(
                    x=(l_sh.x + r_sh.x) * 0.5 * w,
                    y=(l_sh.y + r_sh.y) * 0.5 * h,
                )
            pose_landmarks: dict[int, Point] = {}
            for idx in POSE_KEYPOINT_IDS:
                lm = landmarks[idx]
                if lm.visibility < 0.35:
                    continue
                pose_landmarks[idx] = Point(x=lm.x * w, y=lm.y * h)
            candidates.append(
                DetectionResult(
                    bbox=bbox,
                    confidence=0.93,
                    label="person_pose",
                    anchor_point=anchor,
                    pose_landmarks=pose_landmarks or None,
                )
            )
        candidates.sort(key=lambda d: d.bbox.area, reverse=True)
        return candidates, body_lines

    def _face_lines(self, face_res, w: int, h: int) -> list[LineSegment]:
        if face_res is None:
            return []
        if not face_res.face_landmarks:
            return []
        lms = face_res.face_landmarks[0]
        lines: list[LineSegment] = []
        for poly in FACE_POLYLINES:
            for i in range(1, len(poly)):
                s = poly[i - 1]
                e = poly[i]
                p1 = Point(x=lms[s].x * w, y=lms[s].y * h)
                p2 = Point(x=lms[e].x * w, y=lms[e].y * h)
                lines.append(LineSegment(start=p1, end=p2))
        return lines

    def _hand_points(self, hand_res, w: int, h: int) -> tuple[list[list[Point]], list[str]]:
        if hand_res is None or not hand_res.hand_landmarks:
            return [], []
        hand_points: list[list[Point]] = []
        hand_handedness: list[str] = []
        for idx, hand in enumerate(hand_res.hand_landmarks):
            hand_points.append([Point(x=p.x * w, y=p.y * h) for p in hand])
            label = "unknown"
            if hand_res.handedness and idx < len(hand_res.handedness):
                categories = hand_res.handedness[idx]
                if categories:
                    label = str(categories[0].category_name).lower()
            hand_handedness.append(label)
        return hand_points, hand_handedness


class YoloPersonDetector:
    """
    YOLO person detector helper (class 0 = person).
    """

    def __init__(self, model_path: str = "yolo11n.pt", conf: float = 0.45, device: str = "cpu") -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency ultralytics. Please run: pip install ultralytics"
            ) from exc
        self._model = YOLO(model_path)
        self._conf = conf
        self._device = device

    def detect_person_bbox(self, frame: np.ndarray) -> tuple[BBox | None, float]:
        result = self._model.predict(
            source=frame,
            conf=self._conf,
            classes=[0],
            device=self._device,
            verbose=False,
        )[0]
        if result.boxes is None or len(result.boxes) == 0:
            return None, 0.0
        boxes_xyxy = result.boxes.xyxy.cpu().numpy()
        confs = result.boxes.conf.cpu().numpy()
        idx = int(np.argmax(confs))
        x1, y1, x2, y2 = boxes_xyxy[idx]
        bbox = BBox(
            x=max(0, int(x1)),
            y=max(0, int(y1)),
            w=max(2, int(x2 - x1)),
            h=max(2, int(y2 - y1)),
        )
        return bbox, float(confs[idx])


class MediaPipeYoloVisionDetector(VisionDetector):
    """
    Combined detector:
    - MediaPipe: body skeleton, face mesh, shoulder anchor
    - YOLO: person bounding box
    """

    def __init__(
        self,
        config: DetectionConfig,
        *,
        yolo_model: str = "yolo11n.pt",
        yolo_conf: float = 0.45,
        yolo_device: str = "cpu",
    ) -> None:
        self._config = config
        self._mediapipe = MediaPipeVisionDetector(config)
        self._yolo = YoloPersonDetector(model_path=yolo_model, conf=yolo_conf, device=yolo_device)
        self._frame_idx = 0
        self._last_yolo_bbox: BBox | None = None
        self._last_yolo_conf: float = 0.0

    def detect(self, frame: np.ndarray) -> VisionResult:
        mp_result = self._mediapipe.detect(frame)
        self._frame_idx += 1
        should_run_yolo = (
            self._last_yolo_bbox is None
            or self._frame_idx % max(1, self._config.yolo_every_n_frames) == 0
        )
        if should_run_yolo:
            yolo_bbox, yolo_conf = self._yolo.detect_person_bbox(frame)
            if yolo_bbox is not None and self._last_yolo_bbox is not None:
                yolo_bbox = self._smooth_bbox(
                    self._last_yolo_bbox,
                    yolo_bbox,
                    alpha=self._config.yolo_bbox_smooth_alpha,
                    frame_w=frame.shape[1],
                    frame_h=frame.shape[0],
                )
            self._last_yolo_bbox = yolo_bbox
            self._last_yolo_conf = yolo_conf
        else:
            yolo_bbox = self._last_yolo_bbox
            yolo_conf = self._last_yolo_conf

        person_bbox = yolo_bbox if yolo_bbox is not None else mp_result.person_bbox
        tracking_detection = mp_result.tracking_detection
        if tracking_detection is not None and person_bbox is not None:
            tracking_detection = DetectionResult(
                bbox=person_bbox,
                confidence=max(tracking_detection.confidence, yolo_conf),
                label="person_mp_yolo",
                anchor_point=tracking_detection.anchor_point,
                pose_landmarks=tracking_detection.pose_landmarks,
            )

        return VisionResult(
            tracking_detection=tracking_detection,
            tracking_candidates=mp_result.tracking_candidates,
            face_tracking_detection=mp_result.face_tracking_detection,
            person_bbox=person_bbox,
            face_bbox=mp_result.face_bbox,
            body_skeleton=mp_result.body_skeleton,
            face_mesh=mp_result.face_mesh,
            hand_landmarks=mp_result.hand_landmarks,
            hand_handedness=mp_result.hand_handedness,
        )

    @staticmethod
    def _smooth_bbox(
        prev: BBox, curr: BBox, *, alpha: float, frame_w: int, frame_h: int
    ) -> BBox:
        a = min(1.0, max(0.0, alpha))
        x = int(prev.x * (1.0 - a) + curr.x * a)
        y = int(prev.y * (1.0 - a) + curr.y * a)
        w = int(prev.w * (1.0 - a) + curr.w * a)
        h = int(prev.h * (1.0 - a) + curr.h * a)
        x = max(0, min(frame_w - 2, x))
        y = max(0, min(frame_h - 2, y))
        w = max(2, min(frame_w - x, w))
        h = max(2, min(frame_h - y, h))
        return BBox(x=x, y=y, w=w, h=h)


class AsyncDetector:
    """
    Async wrapper to decouple heavy detection from UI/render loop.
    Keeps only the newest frame to minimize lag.
    """

    def __init__(self, detector: VisionDetector) -> None:
        self._detector = detector
        # Keep only the newest frame to prevent stale-lag accumulation.
        self._in_queue: queue.Queue[tuple[int, np.ndarray]] = queue.Queue(maxsize=1)
        self._result_lock = threading.Lock()
        self._last_result = VisionResult()
        self._last_seq = -1
        self._next_seq = 0
        self._stop_event = threading.Event()
        self._last_error: Exception | None = None
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, frame: np.ndarray) -> int:
        seq = self._next_seq
        self._next_seq += 1
        item = (seq, frame.copy())
        while True:
            try:
                self._in_queue.put_nowait(item)
                break
            except queue.Full:
                try:
                    self._in_queue.get_nowait()
                except queue.Empty:
                    pass
        return seq

    def latest(self) -> tuple[int, VisionResult]:
        with self._result_lock:
            return self._last_seq, self._last_result

    @property
    def last_error(self) -> Exception | None:
        with self._result_lock:
            return self._last_error

    def close(self) -> None:
        self._stop_event.set()
        self._worker.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                seq, frame = self._in_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                result = self._detector.detect(frame)
            except Exception as exc:
                # Never let the worker thread die silently; keep last good result.
                with self._result_lock:
                    self._last_error = exc
                continue
            with self._result_lock:
                self._last_seq = seq
                self._last_result = result
                self._last_error = None
