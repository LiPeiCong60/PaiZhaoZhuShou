from __future__ import annotations

import datetime
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import cv2
import numpy as np


class CaptureTrigger(ABC):
    @abstractmethod
    def trigger_capture(
        self,
        frame: np.ndarray | None = None,
        metadata: dict[str, Any] | None = None,
        suffix: str = "",
    ) -> None:
        raise NotImplementedError


class LoggingCaptureTrigger(CaptureTrigger):
    def __init__(self) -> None:
        self._logger = logging.getLogger(self.__class__.__name__)

    def trigger_capture(
        self,
        frame: np.ndarray | None = None,
        metadata: dict[str, Any] | None = None,
        suffix: str = "",
    ) -> None:
        self._logger.info("trigger_capture() called suffix=%s metadata=%s", suffix, metadata or {})


class LocalFileCaptureTrigger(CaptureTrigger):
    def __init__(self, base_dir: str = "captures") -> None:
        self._logger = logging.getLogger(self.__class__.__name__)
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._latest_capture_path: str | None = None

    def trigger_capture(
        self,
        frame: np.ndarray | None = None,
        metadata: dict[str, Any] | None = None,
        suffix: str = "",
    ) -> None:
        if frame is None:
            self._logger.warning("trigger_capture() skipped: frame is None metadata=%s", metadata or {})
            return
        ts = self._now_ts()
        day_dir = self._base_dir / ts[:10]
        day_dir.mkdir(parents=True, exist_ok=True)
        base_name = ts.replace(":", "").replace(" ", "_")
        if suffix:
            base_name = f"{base_name}_{suffix}"
        filename = f"{base_name}.jpg"
        save_path = day_dir / filename
        ok = cv2.imwrite(str(save_path), frame)
        if not ok:
            self._logger.error("failed to save capture: %s", save_path)
            return
        self._latest_capture_path = str(save_path)
        self._logger.info("capture saved: %s metadata=%s", save_path, metadata or {})

    def save_frame(
        self,
        frame: np.ndarray,
        suffix: str = "",
    ) -> str | None:
        """Save a frame and return the path. Used by batch operations."""
        ts = self._now_ts()
        day_dir = self._base_dir / ts[:10]
        day_dir.mkdir(parents=True, exist_ok=True)
        base_name = ts.replace(":", "").replace(" ", "_")
        if suffix:
            base_name = f"{base_name}_{suffix}"
        filename = f"{base_name}.jpg"
        save_path = day_dir / filename
        ok = cv2.imwrite(str(save_path), frame)
        if not ok:
            self._logger.error("failed to save frame: %s", save_path)
            return None
        self._latest_capture_path = str(save_path)
        return str(save_path)

    def latest_capture_path(self) -> str | None:
        return self._latest_capture_path

    @staticmethod
    def _now_ts() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
