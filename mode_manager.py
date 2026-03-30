from __future__ import annotations

from enum import Enum


class ControlMode(str, Enum):
    MANUAL = "MANUAL"
    AUTO_TRACK = "AUTO_TRACK"
    SMART_COMPOSE = "SMART_COMPOSE"


class ModeManager:
    def __init__(self, initial_mode: ControlMode = ControlMode.MANUAL) -> None:
        self._mode = initial_mode

    @property
    def mode(self) -> ControlMode:
        return self._mode

    def set_mode(self, mode: ControlMode) -> None:
        self._mode = mode

