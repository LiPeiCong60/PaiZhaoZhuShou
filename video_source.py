from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

import cv2
import numpy as np

from config import VideoSourceConfig


class VideoSource(ABC):
    @abstractmethod
    def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def read(self) -> Optional[np.ndarray]:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError


class OpenCVVideoSource(VideoSource):
    def __init__(self, config: VideoSourceConfig) -> None:
        self._config = config
        self._cap: cv2.VideoCapture | None = None
        self._logger = logging.getLogger(self.__class__.__name__)
        self._last_reconnect_try = 0.0
        self._latest_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._read_fail_streak = 0
        self._last_ok_ts = 0.0

    def _open_capture(self) -> None:
        if self._cap is not None:
            self._cap.release()
        source = self._parse_source(self._config.stream_url)
        self._cap = cv2.VideoCapture(source)
        if self._config.capture_buffer_size > 0:
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, self._config.capture_buffer_size)
        self._logger.info("video source opened: %s", self._config.stream_url)
        self._read_fail_streak = 0

    @staticmethod
    def _parse_source(raw: str) -> str | int:
        if raw.isdigit():
            return int(raw)
        return raw

    def start(self) -> None:
        self._open_capture()
        if self._config.threaded_capture and self._reader_thread is None:
            self._stop_event.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_worker,
                name="opencv-video-reader",
                daemon=True,
            )
            self._reader_thread.start()

    def read(self) -> Optional[np.ndarray]:
        if self._config.threaded_capture:
            with self._frame_lock:
                if self._latest_frame is None:
                    return None
                return self._latest_frame.copy()
        if self._cap is None:
            return None
        return self._read_once()

    def stop(self) -> None:
        self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        with self._frame_lock:
            self._latest_frame = None

    def _read_once(self) -> Optional[np.ndarray]:
        if self._cap is None:
            return None
        if not self._cap.isOpened():
            now = time.time()
            if now - self._last_reconnect_try >= self._config.reconnect_interval_s:
                self._last_reconnect_try = now
                self._logger.warning("stream disconnected, reconnecting...")
                self._open_capture()
            return None
        ok, frame = self._cap.read()
        if not ok:
            self._read_fail_streak += 1
            now = time.time()
            should_reopen = (
                self._read_fail_streak >= 18
                and now - self._last_reconnect_try >= self._config.reconnect_interval_s
            )
            if should_reopen:
                self._last_reconnect_try = now
                self._logger.warning("stream read failed repeatedly, reconnecting...")
                self._open_capture()
            return None
        self._read_fail_streak = 0
        self._last_ok_ts = time.time()
        return frame

    def _reader_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._read_once()
            except Exception:
                self._logger.exception("video reader loop failed once, will retry")
                time.sleep(max(0.02, self._config.read_sleep_s))
                continue
            if frame is None:
                time.sleep(max(0.001, self._config.read_sleep_s))
                continue
            with self._frame_lock:
                self._latest_frame = frame
