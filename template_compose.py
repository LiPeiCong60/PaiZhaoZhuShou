from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from utils.common_types import DetectionResult, Point

POSE_INDICES: tuple[int, ...] = (11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)


@dataclass(slots=True)
class TemplateProfile:
    template_id: str
    name: str
    image_path: str
    created_at: str
    anchor_norm_x: float
    anchor_norm_y: float
    area_ratio: float
    facing_sign: float
    pose_points: dict[int, tuple[float, float]]
    pose_points_image: dict[int, tuple[float, float]]
    bbox_norm: tuple[float, float, float, float]


@dataclass(slots=True)
class ComposeFeedback:
    total_score: float
    pose_score: float
    compose_score: float
    ready: bool
    messages: list[str]
    target_norm: tuple[float, float]
    offset_norm: tuple[float, float]


class TemplateLibrary:
    def __init__(self, root: str = ".template_library") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._db_path = self._root / "templates.json"
        self._templates: list[TemplateProfile] = []
        self._load()

    def list_templates(self) -> list[TemplateProfile]:
        return list(self._templates)

    def get(self, template_id: str) -> TemplateProfile | None:
        for t in self._templates:
            if t.template_id == template_id:
                return t
        return None

    def add(self, profile: TemplateProfile) -> None:
        self._templates = [t for t in self._templates if t.template_id != profile.template_id]
        self._templates.append(profile)
        self._save()

    def remove(self, template_id: str) -> None:
        self._templates = [t for t in self._templates if t.template_id != template_id]
        self._save()

    def _load(self) -> None:
        if not self._db_path.exists():
            self._templates = []
            return
        try:
            payload = json.loads(self._db_path.read_text(encoding="utf-8"))
        except Exception:
            self._templates = []
            return
        loaded: list[TemplateProfile] = []
        for item in payload if isinstance(payload, list) else []:
            try:
                raw_pose = item.get("pose_points", {})
                pose_points = {int(k): (float(v[0]), float(v[1])) for k, v in raw_pose.items()}
                loaded.append(
                    TemplateProfile(
                        template_id=str(item["template_id"]),
                        name=str(item["name"]),
                        image_path=str(item["image_path"]),
                        created_at=str(item["created_at"]),
                        anchor_norm_x=float(item["anchor_norm_x"]),
                        anchor_norm_y=float(item["anchor_norm_y"]),
                        area_ratio=float(item["area_ratio"]),
                        facing_sign=float(item.get("facing_sign", 0.0)),
                        pose_points=pose_points,
                        pose_points_image={
                            int(k): (float(v[0]), float(v[1]))
                            for k, v in item.get("pose_points_image", {}).items()
                        },
                        bbox_norm=_safe_bbox_norm(item.get("bbox_norm")),
                    )
                )
            except Exception:
                continue
        self._templates = loaded

    def _save(self) -> None:
        dump_data: list[dict[str, object]] = []
        for t in self._templates:
            item = asdict(t)
            item["pose_points"] = {str(k): [v[0], v[1]] for k, v in t.pose_points.items()}
            item["pose_points_image"] = {str(k): [v[0], v[1]] for k, v in t.pose_points_image.items()}
            item["bbox_norm"] = [float(x) for x in t.bbox_norm]
            dump_data.append(item)
        self._db_path.write_text(
            json.dumps(dump_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class TemplateComposeEngine:
    SCORE_THRESHOLD = 68.0

    @staticmethod
    def create_profile(name: str, image_path: str, detection: DetectionResult, frame_shape: tuple[int, int, int]) -> TemplateProfile | None:
        if detection.anchor_point is None or not detection.pose_landmarks:
            return None
        h, w = frame_shape[:2]
        anchor_norm_x = float(detection.anchor_point.x) / float(max(1, w))
        anchor_norm_y = float(detection.anchor_point.y) / float(max(1, h))
        area_ratio = float(detection.bbox.area) / float(max(1, h * w))
        pose_points = _normalize_pose_points(detection.pose_landmarks)
        pose_points_image = {
            int(idx): (
                max(0.0, min(1.0, p.x / float(max(1, w)))),
                max(0.0, min(1.0, p.y / float(max(1, h)))),
            )
            for idx, p in (detection.pose_landmarks or {}).items()
        }
        bbox_norm = (
            max(0.0, min(1.0, detection.bbox.x / float(max(1, w)))),
            max(0.0, min(1.0, detection.bbox.y / float(max(1, h)))),
            max(0.0, min(1.0, detection.bbox.w / float(max(1, w)))),
            max(0.0, min(1.0, detection.bbox.h / float(max(1, h)))),
        )
        if len(pose_points) < 4:
            return None
        return TemplateProfile(
            template_id=str(uuid.uuid4()),
            name=name,
            image_path=image_path,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            anchor_norm_x=max(0.0, min(1.0, anchor_norm_x)),
            anchor_norm_y=max(0.0, min(1.0, anchor_norm_y)),
            area_ratio=max(1e-5, area_ratio),
            facing_sign=_facing_sign(detection.pose_landmarks),
            pose_points=pose_points,
            pose_points_image=pose_points_image,
            bbox_norm=bbox_norm,
        )

    @staticmethod
    def evaluate(
        profile: TemplateProfile,
        detection: DetectionResult,
        frame_shape: tuple[int, int, int],
        *,
        mirror_template: bool = False,
    ) -> ComposeFeedback:
        h, w = frame_shape[:2]
        anchor = detection.anchor_point if detection.anchor_point is not None else detection.bbox.center
        curr_x = float(anchor.x) / float(max(1, w))
        curr_y = float(anchor.y) / float(max(1, h))
        curr_area_ratio = float(detection.bbox.area) / float(max(1, h * w))

        target_anchor_x = profile.anchor_norm_x
        if mirror_template:
            target_anchor_x = 1.0 - target_anchor_x

        dx = target_anchor_x - curr_x
        dy = profile.anchor_norm_y - curr_y
        pos_err = math.hypot(dx, dy)
        pos_score = _clamp_score(100.0 * (1.0 - pos_err / 0.45))

        size_ratio = min(curr_area_ratio, profile.area_ratio) / max(curr_area_ratio, profile.area_ratio, 1e-6)
        size_score = _clamp_score(100.0 * size_ratio)

        facing = _facing_sign(detection.pose_landmarks)
        if abs(facing) < 0.05 or abs(profile.facing_sign) < 0.05:
            facing_score = 70.0
        else:
            facing_score = 100.0 if facing * profile.facing_sign > 0 else 50.0
        compose_score = 0.5 * pos_score + 0.3 * size_score + 0.2 * facing_score

        template_pose = profile.pose_points
        if mirror_template:
            template_pose = {idx: (-p[0], p[1]) for idx, p in profile.pose_points.items()}
        pose_score = _pose_similarity(template_pose, _normalize_pose_points(detection.pose_landmarks or {}))
        total = 0.7 * pose_score + 0.3 * compose_score

        messages = _build_messages(dx=dx, dy=dy, curr_area=curr_area_ratio, target_area=profile.area_ratio)
        ready = total >= TemplateComposeEngine.SCORE_THRESHOLD
        return ComposeFeedback(
            total_score=round(total, 1),
            pose_score=round(pose_score, 1),
            compose_score=round(compose_score, 1),
            ready=ready,
            messages=messages,
            target_norm=(target_anchor_x, profile.anchor_norm_y),
            offset_norm=(dx, dy),
        )


class GestureCaptureState:
    DEFAULT_STABLE_FRAMES = 10
    DEFAULT_OPEN_HOLD_MIN_S = 0.35

    def __init__(
        self,
        *,
        stable_frames: int | None = None,
        open_hold_min_s: float | None = None,
    ) -> None:
        self._stable_frames = max(
            2, int(stable_frames if stable_frames is not None else self.DEFAULT_STABLE_FRAMES)
        )
        self._open_hold_min_s = max(
            0.05, float(open_hold_min_s if open_hold_min_s is not None else self.DEFAULT_OPEN_HOLD_MIN_S)
        )
        self._phase = "idle"
        self._open_deadline = 0.0
        self._open_min_hold_until = 0.0
        self._cooldown_until = 0.0
        self._counts = {"open": 0, "fist": 0, "ok": 0}

    def set_sensitivity(self, *, stable_frames: int, open_hold_min_s: float) -> None:
        self._stable_frames = max(2, int(stable_frames))
        self._open_hold_min_s = max(0.05, float(open_hold_min_s))

    def reset(self) -> None:
        self._phase = "idle"
        self._open_deadline = 0.0
        self._open_min_hold_until = 0.0
        self._counts = {"open": 0, "fist": 0, "ok": 0}

    def update(
        self,
        hands: list[list[Point]] | None,
        now: float,
        *,
        ready_for_pose_capture: bool,
        force_ok_enabled: bool,
    ) -> str | None:
        if now < self._cooldown_until:
            return None
        gestures = _detect_hand_gestures(hands or [])
        for key in ("open", "fist", "ok"):
            if gestures[key]:
                self._counts[key] = min(10, self._counts[key] + 1)
            else:
                self._counts[key] = 0

        if force_ok_enabled and self._counts["ok"] >= self._stable_frames:
            self._cooldown_until = now + 1.2
            self.reset()
            return "force_capture"

        if not ready_for_pose_capture:
            self.reset()
            return None

        if self._phase == "idle":
            if self._counts["open"] >= self._stable_frames:
                self._phase = "open"
                self._open_deadline = now + 2.0
                self._open_min_hold_until = now + self._open_hold_min_s
            return None

        if now > self._open_deadline:
            self.reset()
            return None

        if now < self._open_min_hold_until:
            return None

        if self._phase == "open" and self._counts["fist"] >= self._stable_frames:
            self._cooldown_until = now + 1.2
            self.reset()
            return "capture"
        return None


def _normalize_pose_points(landmarks: dict[int, Point]) -> dict[int, tuple[float, float]]:
    if not landmarks:
        return {}
    left_sh = landmarks.get(11)
    right_sh = landmarks.get(12)
    left_hip = landmarks.get(23)
    right_hip = landmarks.get(24)
    if left_sh is None or right_sh is None or left_hip is None or right_hip is None:
        return {}

    center_x = (left_hip.x + right_hip.x) * 0.5
    center_y = (left_hip.y + right_hip.y) * 0.5
    sh_mid_x = (left_sh.x + right_sh.x) * 0.5
    sh_mid_y = (left_sh.y + right_sh.y) * 0.5
    torso_scale = math.hypot(sh_mid_x - center_x, sh_mid_y - center_y)
    shoulder_width = math.hypot(left_sh.x - right_sh.x, left_sh.y - right_sh.y)
    scale = max(18.0, torso_scale, shoulder_width * 0.7)

    normalized: dict[int, tuple[float, float]] = {}
    for idx in POSE_INDICES:
        p = landmarks.get(idx)
        if p is None:
            continue
        normalized[idx] = ((p.x - center_x) / scale, (p.y - center_y) / scale)
    return normalized


def _pose_similarity(template_pose: dict[int, tuple[float, float]], live_pose: dict[int, tuple[float, float]]) -> float:
    common = [idx for idx in POSE_INDICES if idx in template_pose and idx in live_pose]
    if len(common) < 4:
        return 0.0
    distances = []
    for idx in common:
        tx, ty = template_pose[idx]
        lx, ly = live_pose[idx]
        distances.append(math.hypot(tx - lx, ty - ly))
    mean_dist = sum(distances) / len(distances)
    return _clamp_score(100.0 * (1.0 - mean_dist / 0.9))


def _facing_sign(landmarks: dict[int, Point] | None) -> float:
    if not landmarks:
        return 0.0
    left_sh = landmarks.get(11)
    right_sh = landmarks.get(12)
    if left_sh is None or right_sh is None:
        return 0.0
    diff = right_sh.x - left_sh.x
    if abs(diff) < 1e-6:
        return 0.0
    return 1.0 if diff > 0 else -1.0


def _build_messages(*, dx: float, dy: float, curr_area: float, target_area: float) -> list[str]:
    messages: list[str] = []
    if dx > 0.06:
        messages.append("人物向画面右侧移动")
    elif dx < -0.06:
        messages.append("人物向画面左侧移动")

    if dy > 0.06:
        messages.append("人物向画面下方移动")
    elif dy < -0.06:
        messages.append("人物向画面上方移动")

    if curr_area < target_area * 0.86:
        messages.append("请向镜头靠近一些")
    elif curr_area > target_area * 1.18:
        messages.append("请离镜头远一点")

    if not messages:
        messages.append("构图接近模板，继续微调姿势")
    return messages


def _detect_hand_gestures(hands: list[list[Point]]) -> dict[str, bool]:
    has_open = False
    has_fist = False
    has_ok = False
    for hand in hands:
        if len(hand) < 21:
            continue
        finger_count = _extended_finger_count(hand)
        has_open = has_open or finger_count >= 4
        has_fist = has_fist or finger_count <= 1
        has_ok = has_ok or _is_ok_gesture(hand, finger_count)
    return {"open": has_open, "fist": has_fist, "ok": has_ok}


def _extended_finger_count(hand: list[Point]) -> int:
    tips = [4, 8, 12, 16, 20]
    pips = [3, 6, 10, 14, 18]
    wrist = hand[0]
    count = 0
    for tip, pip in zip(tips, pips):
        # Orientation-agnostic extension rule.
        if _dist(wrist, hand[tip]) > _dist(wrist, hand[pip]) * 1.08:
            count += 1
    return count


def _is_ok_gesture(hand: list[Point], finger_count: int) -> bool:
    palm_size = max(8.0, _dist(hand[0], hand[9]))
    # Use a looser circle threshold to improve right-hand usability across camera angles.
    thumb_index_close = _dist(hand[4], hand[8]) <= palm_size * 0.45
    wrist = hand[0]
    middle_ext = _dist(wrist, hand[12]) > _dist(wrist, hand[10]) * 1.08
    ring_ext = _dist(wrist, hand[16]) > _dist(wrist, hand[14]) * 1.08
    pinky_ext = _dist(wrist, hand[20]) > _dist(wrist, hand[18]) * 1.08
    ext_count = int(middle_ext) + int(ring_ext) + int(pinky_ext)
    return thumb_index_close and ext_count >= 1 and finger_count >= 2


def _clamp_score(score: float) -> float:
    return max(0.0, min(100.0, score))


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _safe_bbox_norm(raw: object) -> tuple[float, float, float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            x, y, w, h = [float(v) for v in raw]
            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            w = max(0.0, min(1.0, w))
            h = max(0.0, min(1.0, h))
            return (x, y, w, h)
        except Exception:
            pass
    return (0.0, 0.0, 0.0, 0.0)
