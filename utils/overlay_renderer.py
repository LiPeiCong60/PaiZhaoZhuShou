from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from mode_manager import ControlMode
from ui.cn_text import get_cn_font
from utils.common_types import VisionResult
from utils.ui_text import detection_label_to_text, mode_to_text

HAND_EDGES: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
)


class OverlayRenderer:
    """
    Draws tracking overlays on a frame.
    Uses PIL for non-ASCII text to avoid OpenCV Chinese-garbled text.
    """

    def __init__(
        self,
        *,
        show_body_skeleton: bool = True,
        show_face_mesh: bool = True,
        enable_overlay: bool = True,
    ) -> None:
        self._show_body_skeleton = show_body_skeleton
        self._show_face_mesh = show_face_mesh
        self._enable_overlay = enable_overlay

    def draw(
        self,
        frame: np.ndarray,
        mode: ControlMode,
        target_pt: tuple[int, int],
        vision: Optional[VisionResult],
    ) -> None:
        if not self._enable_overlay:
            return
        tx, ty = target_pt
        cv2.circle(frame, (tx, ty), 7, (0, 255, 255), 2)
        text_items: list[tuple[str, tuple[int, int], tuple[int, int, int], float, int]] = []
        text_items.append((f"模式: {mode_to_text(mode)}", (10, 28), (60, 255, 60), 0.75, 2))
        if vision is None or vision.tracking_detection is None:
            text_items.append(("检测: 无", (10, 58), (0, 160, 255), 0.6, 2))
            self._draw_texts(frame, text_items)
            return

        detection = vision.tracking_detection
        if self._show_body_skeleton:
            for seg in (vision.body_skeleton or []):
                cv2.line(
                    frame,
                    (int(seg.start.x), int(seg.start.y)),
                    (int(seg.end.x), int(seg.end.y)),
                    (50, 220, 255),
                    2,
                )
        if self._show_face_mesh:
            for seg in (vision.face_mesh or []):
                cv2.line(
                    frame,
                    (int(seg.start.x), int(seg.start.y)),
                    (int(seg.end.x), int(seg.end.y)),
                    (255, 160, 80),
                    1,
                )
        for hand in (vision.hand_landmarks or []):
            if len(hand) < 21:
                continue
            for s, e in HAND_EDGES:
                cv2.line(
                    frame,
                    (int(hand[s].x), int(hand[s].y)),
                    (int(hand[e].x), int(hand[e].y)),
                    (240, 110, 30),
                    2,
                )
            for p in hand:
                cv2.circle(frame, (int(p.x), int(p.y)), 2, (60, 240, 255), -1)

        if vision.person_bbox is not None:
            b = vision.person_bbox
            cv2.rectangle(frame, (b.x, b.y), (b.x + b.w, b.y + b.h), (0, 220, 0), 2)
            text_items.append(("人体", (b.x, max(15, b.y - 6)), (0, 220, 0), 0.55, 2))

        if vision.face_bbox is not None:
            fb = vision.face_bbox
            cv2.rectangle(frame, (fb.x, fb.y), (fb.x + fb.w, fb.y + fb.h), (255, 200, 0), 2)
            text_items.append(("人脸", (fb.x, max(15, fb.y - 6)), (255, 200, 0), 0.55, 2))

        if detection.anchor_point is not None:
            ax, ay = int(detection.anchor_point.x), int(detection.anchor_point.y)
            cv2.circle(frame, (ax, ay), 6, (0, 255, 0), -1)
            cv2.line(frame, (ax - 12, ay), (ax + 12, ay), (0, 255, 0), 2)
            cv2.line(frame, (ax, ay - 12), (ax, ay + 12), (0, 255, 0), 2)
            text_items.append(("构图锚点", (max(10, ax - 60), max(25, ay - 15)), (0, 255, 0), 0.5, 2))

        text_items.append(
            (
                f"{detection_label_to_text(detection.label)}:{detection.confidence:.2f}",
                (10, 58),
                (255, 255, 0),
                0.6,
                2,
            )
        )
        bbox = detection.bbox
        cv2.circle(frame, (int(bbox.center.x), int(bbox.center.y)), 4, (180, 180, 255), -1)
        self._draw_texts(frame, text_items)

    def _draw_texts(
        self,
        frame: np.ndarray,
        text_items: list[tuple[str, tuple[int, int], tuple[int, int, int], float, int]],
    ) -> None:
        if not text_items:
            return
        need_pil = any(not all(ord(ch) < 128 for ch in text) for text, _, _, _, _ in text_items)
        if not need_pil:
            for text, org, color, font_scale, thickness in text_items:
                cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness)
            return

        try:
            from PIL import Image, ImageDraw
        except Exception:
            for text, org, color, font_scale, thickness in text_items:
                cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness)
            return

        pil_image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        pil_draw = ImageDraw.Draw(pil_image)
        for text, org, color, font_scale, _ in text_items:
            font = get_cn_font(size=max(12, int(28 * font_scale)))
            b, g, r = color
            pil_draw.text(org, text, font=font, fill=(r, g, b))
        frame[:] = cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2BGR)
