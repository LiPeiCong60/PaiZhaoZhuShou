from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class Point:
    x: float
    y: float


@dataclass(slots=True)
class BBox:
    x: int
    y: int
    w: int
    h: int

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def center(self) -> Point:
        return Point(self.x + self.w / 2.0, self.y + self.h / 2.0)


@dataclass(slots=True)
class DetectionResult:
    bbox: BBox
    confidence: float
    label: str = "person"
    track_id: Optional[int] = None
    anchor_point: Optional[Point] = None
    pose_landmarks: dict[int, Point] | None = None


@dataclass(slots=True)
class LineSegment:
    start: Point
    end: Point


@dataclass(slots=True)
class VisionResult:
    tracking_detection: Optional[DetectionResult] = None
    tracking_candidates: list[DetectionResult] | None = None
    face_tracking_detection: Optional[DetectionResult] = None
    person_bbox: Optional[BBox] = None
    face_bbox: Optional[BBox] = None
    body_skeleton: list[LineSegment] | None = None
    face_mesh: list[LineSegment] | None = None
    hand_landmarks: list[list[Point]] | None = None
    hand_handedness: list[str] | None = None


@dataclass(slots=True)
class GimbalCommand:
    pan_delta: float
    tilt_delta: float
    reason: str = "tracking"
