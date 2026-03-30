from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from config import GimbalConfig, ServoAxisConfig


@dataclass(slots=True)
class GimbalState:
    pan_command_angle: float
    tilt_command_angle: float
    pan_feedback_angle: float
    tilt_feedback_angle: float
    feedback_valid: bool = False


class ServoDriver(ABC):
    @abstractmethod
    def write_angle(self, axis: str, angle_deg: float, axis_cfg: ServoAxisConfig) -> None:
        raise NotImplementedError

    @abstractmethod
    def read_angle(self, axis: str, axis_cfg: ServoAxisConfig) -> float | None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class MockServoDriver(ServoDriver):
    def __init__(self) -> None:
        self._logger = logging.getLogger(self.__class__.__name__)
        self._angles = {"pan": 0.0, "tilt": 0.0}

    def write_angle(self, axis: str, angle_deg: float, axis_cfg: ServoAxisConfig) -> None:
        self._angles[axis] = angle_deg
        self._logger.debug("[MOCK] axis=%s angle=%.2f", axis, angle_deg)

    def read_angle(self, axis: str, axis_cfg: ServoAxisConfig) -> float | None:
        return self._angles.get(axis)

    def close(self) -> None:
        return


class RaspberryPiPWMDriver(ServoDriver):
    """
    Raspberry Pi PWM driver using PCA9685 via adafruit_servokit.
    """

    def __init__(self, pca9685_address: int = 0x40, channels: int = 16) -> None:
        try:
            from adafruit_servokit import ServoKit
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency adafruit-circuitpython-servokit. "
                "Install it on Raspberry Pi first."
            ) from exc
        self._logger = logging.getLogger(self.__class__.__name__)
        self._kit = ServoKit(channels=channels, address=pca9685_address)
        self._channels = channels
        self._angles: dict[str, float] = {"pan": 0.0, "tilt": 0.0}

    def write_angle(self, axis: str, angle_deg: float, axis_cfg: ServoAxisConfig) -> None:
        channel = axis_cfg.servo_id
        if channel < 0 or channel >= self._channels:
            raise ValueError(
                f"Invalid servo_id={channel} for axis={axis}. "
                f"Expected [0, {self._channels - 1}]."
            )

        servo_angle = self._to_servo_space(angle_deg, axis_cfg)
        self._kit.servo[channel].angle = servo_angle
        self._angles[axis] = angle_deg
        self._logger.debug(
            "[HW] axis=%s servo_id=%s angle=%.2f servo_angle=%.2f",
            axis,
            channel,
            angle_deg,
            servo_angle,
        )

    def read_angle(self, axis: str, axis_cfg: ServoAxisConfig) -> float | None:
        # No position feedback channel yet; return the latest commanded angle.
        return self._angles.get(axis)

    def close(self) -> None:
        return

    @staticmethod
    def _to_servo_space(angle_deg: float, axis_cfg: ServoAxisConfig) -> float:
        span = axis_cfg.max_angle - axis_cfg.min_angle
        if span <= 0:
            raise ValueError(
                f"Invalid axis range: min={axis_cfg.min_angle}, max={axis_cfg.max_angle}"
            )
        clamped = min(axis_cfg.max_angle, max(axis_cfg.min_angle, angle_deg))
        normalized = (clamped - axis_cfg.min_angle) / span
        return normalized * 180.0


class TTLBusSerialDriver(ServoDriver):
    """
    TTL bus-servo driver over serial port.
    Frame format follows controller command style:
    {G0000#000P1500T1500!#001P1500T1500!}
    """

    def __init__(
        self,
        *,
        port: str,
        baudrate: int = 115200,
        move_time_ms: int = 120,
        timeout_s: float = 0.2,
    ) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("Missing dependency pyserial. Install requirements first.") from exc

        self._logger = logging.getLogger(self.__class__.__name__)
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout_s,
            write_timeout=timeout_s,
        )
        self._move_time_ms = max(20, int(move_time_ms))
        self._angles: dict[str, float] = {"pan": 0.0, "tilt": 0.0}
        self._pulses_by_id: dict[int, int] = {}
        self._id_by_axis: dict[str, int] = {}

    def write_angle(self, axis: str, angle_deg: float, axis_cfg: ServoAxisConfig) -> None:
        servo_id = axis_cfg.servo_id
        pulse = self._angle_to_pulse(angle_deg, axis_cfg)

        self._angles[axis] = angle_deg
        self._id_by_axis[axis] = servo_id
        self._pulses_by_id[servo_id] = pulse

        frame = self._build_group_frame(self._pulses_by_id, self._move_time_ms)
        self._serial.write(frame.encode("ascii"))
        self._serial.flush()
        self._logger.debug(
            "[TTL] axis=%s servo_id=%03d angle=%.2f pulse=%d frame=%s",
            axis,
            servo_id,
            angle_deg,
            pulse,
            frame.strip(),
        )

    def read_angle(self, axis: str, axis_cfg: ServoAxisConfig) -> float | None:
        return self._angles.get(axis)

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()

    @staticmethod
    def _angle_to_pulse(angle_deg: float, axis_cfg: ServoAxisConfig) -> int:
        span = axis_cfg.max_angle - axis_cfg.min_angle
        if span <= 0:
            raise ValueError(
                f"Invalid axis range: min={axis_cfg.min_angle}, max={axis_cfg.max_angle}"
            )
        clamped = min(axis_cfg.max_angle, max(axis_cfg.min_angle, angle_deg))
        normalized = (clamped - axis_cfg.min_angle) / span
        pulse = int(round(500 + normalized * 2000))
        return min(2500, max(500, pulse))

    @staticmethod
    def _build_group_frame(pulses_by_id: dict[int, int], move_time_ms: int) -> str:
        if not pulses_by_id:
            return "{G0000!}\r\n"
        parts = [f"#{sid:03d}P{pulse:04d}T{move_time_ms:04d}!" for sid, pulse in sorted(pulses_by_id.items())]
        return "{G0000" + "".join(parts) + "}\r\n"


class GimbalController:
    def __init__(self, config: GimbalConfig, driver: ServoDriver) -> None:
        self._config = config
        self._driver = driver
        self._logger = logging.getLogger(self.__class__.__name__)
        self._lock = threading.RLock()
        self._stop_feedback_event = threading.Event()
        self._feedback_thread: threading.Thread | None = None
        self._state = GimbalState(
            pan_command_angle=config.pan.home_angle,
            tilt_command_angle=config.tilt.home_angle,
            pan_feedback_angle=config.pan.home_angle,
            tilt_feedback_angle=config.tilt.home_angle,
        )
        self.set_absolute(
            self._state.pan_command_angle, self._state.tilt_command_angle, smooth=False
        )
        self.refresh_feedback()
        self._start_feedback_loop()

    @property
    def state(self) -> GimbalState:
        with self._lock:
            return GimbalState(
                pan_command_angle=self._state.pan_command_angle,
                tilt_command_angle=self._state.tilt_command_angle,
                pan_feedback_angle=self._state.pan_feedback_angle,
                tilt_feedback_angle=self._state.tilt_feedback_angle,
                feedback_valid=self._state.feedback_valid,
            )

    def home(self) -> None:
        with self._lock:
            self.set_absolute(
                self._config.pan.home_angle, self._config.tilt.home_angle, smooth=True
            )

    def set_absolute(self, pan: float, tilt: float, smooth: bool = True) -> None:
        with self._lock:
            pan = self._clamp(pan, self._config.pan)
            tilt = self._clamp(tilt, self._config.tilt)

            if not smooth:
                self._apply(pan, tilt)
                return

            # Guard against accidental zero/negative step that can cause infinite loop.
            pan_step = max(1e-6, abs(self._config.pan.max_step_deg))
            tilt_step = max(1e-6, abs(self._config.tilt.max_step_deg))
            current_pan, current_tilt = self.get_current_angles(prefer_feedback=True)
            while True:
                next_pan = self._step_towards(
                    current_pan, pan, pan_step
                )
                next_tilt = self._step_towards(
                    current_tilt, tilt, tilt_step
                )
                self._apply(next_pan, next_tilt)

                done_pan = abs(next_pan - pan) < 1e-3
                done_tilt = abs(next_tilt - tilt) < 1e-3
                if done_pan and done_tilt:
                    break
                current_pan, current_tilt = self.get_current_angles(prefer_feedback=True)
                time.sleep(self._config.smooth_sleep_s)

    def move_relative(self, pan_delta: float, tilt_delta: float, smooth: bool = True) -> None:
        with self._lock:
            current_pan, current_tilt = self.get_current_angles(prefer_feedback=True)
            self.set_absolute(
                pan=current_pan + pan_delta,
                tilt=current_tilt + tilt_delta,
                smooth=smooth,
            )

    def refresh_feedback(self) -> GimbalState:
        with self._lock:
            pan = self._driver.read_angle("pan", self._config.pan)
            tilt = self._driver.read_angle("tilt", self._config.tilt)
            if pan is None or tilt is None:
                self._state.feedback_valid = False
                return self.state

            self._state.pan_feedback_angle = self._clamp(pan, self._config.pan)
            self._state.tilt_feedback_angle = self._clamp(tilt, self._config.tilt)
            self._state.feedback_valid = True
            return self.state

    def get_current_angles(self, prefer_feedback: bool = True) -> tuple[float, float]:
        with self._lock:
            if prefer_feedback:
                state = self.refresh_feedback()
                if state.feedback_valid:
                    return state.pan_feedback_angle, state.tilt_feedback_angle
            return self._state.pan_command_angle, self._state.tilt_command_angle

    def close(self) -> None:
        self._stop_feedback_event.set()
        if self._feedback_thread is not None:
            self._feedback_thread.join(timeout=1.0)
        with self._lock:
            self._driver.close()

    def _apply(self, pan: float, tilt: float) -> None:
        self._driver.write_angle("pan", pan, self._config.pan)
        self._driver.write_angle("tilt", tilt, self._config.tilt)
        self._state.pan_command_angle = pan
        self._state.tilt_command_angle = tilt
        self.refresh_feedback()
        self._logger.debug("gimbal pan=%.2f tilt=%.2f", pan, tilt)

    @staticmethod
    def _clamp(value: float, axis_cfg: ServoAxisConfig) -> float:
        return min(axis_cfg.max_angle, max(axis_cfg.min_angle, value))

    @staticmethod
    def _step_towards(current: float, target: float, max_step: float) -> float:
        if abs(target - current) <= max_step:
            return target
        return current + max_step if target > current else current - max_step

    def _start_feedback_loop(self) -> None:
        if self._feedback_thread is not None:
            return
        self._feedback_thread = threading.Thread(
            target=self._feedback_worker,
            name="gimbal-feedback-loop",
            daemon=True,
        )
        self._feedback_thread.start()

    def _feedback_worker(self) -> None:
        while not self._stop_feedback_event.is_set():
            try:
                self.refresh_feedback()
            except Exception:
                self._logger.exception("Failed to refresh gimbal feedback angle.")
            time.sleep(max(0.01, self._config.feedback_poll_interval_s))
