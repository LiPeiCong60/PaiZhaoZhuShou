from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from utils.common_types import Point


class TargetPreset(str, Enum):
    CENTER = "CENTER"
    LEFT_THIRD = "LEFT_THIRD"
    LOWER_LEFT = "LOWER_LEFT"
    CUSTOM_POINT = "CUSTOM_POINT"


class TargetStrategy(ABC):
    @abstractmethod
    def get_target_point(self, frame_shape: tuple[int, int, int]) -> Point:
        raise NotImplementedError


class CenterTargetStrategy(TargetStrategy):
    def get_target_point(self, frame_shape: tuple[int, int, int]) -> Point:
        h, w = frame_shape[:2]
        return Point(x=w / 2.0, y=h / 2.0)


class LeftThirdTargetStrategy(TargetStrategy):
    def get_target_point(self, frame_shape: tuple[int, int, int]) -> Point:
        h, w = frame_shape[:2]
        return Point(x=w / 3.0, y=h / 2.0)


class LowerLeftTargetStrategy(TargetStrategy):
    def get_target_point(self, frame_shape: tuple[int, int, int]) -> Point:
        h, w = frame_shape[:2]
        return Point(x=w * 0.33, y=h * 0.67)


class CustomPointTargetStrategy(TargetStrategy):
    def __init__(self, normalized_x: float, normalized_y: float) -> None:
        self._nx = min(1.0, max(0.0, normalized_x))
        self._ny = min(1.0, max(0.0, normalized_y))

    def get_target_point(self, frame_shape: tuple[int, int, int]) -> Point:
        h, w = frame_shape[:2]
        return Point(x=w * self._nx, y=h * self._ny)


def build_target_strategy(
    preset: TargetPreset, custom: tuple[float, float] | None = None
) -> TargetStrategy:
    if preset == TargetPreset.CUSTOM_POINT and custom is not None:
        return CustomPointTargetStrategy(custom[0], custom[1])
    strategy_map: dict[TargetPreset, type[TargetStrategy]] = {
        TargetPreset.CENTER: CenterTargetStrategy,
        TargetPreset.LEFT_THIRD: LeftThirdTargetStrategy,
        TargetPreset.LOWER_LEFT: LowerLeftTargetStrategy,
    }
    strategy_cls = strategy_map.get(preset, CenterTargetStrategy)
    return strategy_cls()
