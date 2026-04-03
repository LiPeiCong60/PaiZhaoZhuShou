"""
抓拍服务模块
负责抓拍、保存和AI分析
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from interfaces.capture_trigger import CaptureTrigger, LocalFileCaptureTrigger
from interfaces.ai_assistant import AIPhotoAssistant, CaptureAnalysis
from services.runtime_state import RuntimeState


@dataclass(slots=True)
class CaptureResult:
    path: str | None
    analysis: CaptureAnalysis | None = None
    analysis_error: str | None = None


class CaptureService:
    """抓拍服务类，封装所有抓拍相关逻辑"""

    def __init__(
        self,
        capture_trigger: CaptureTrigger,
        ai_assistant: Optional[AIPhotoAssistant] = None,
        runtime_state: Optional[RuntimeState] = None,
    ) -> None:
        self._capture_trigger = capture_trigger
        self._ai_assistant = ai_assistant
        self._runtime_state = runtime_state

    def capture(
        self,
        frame,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        suffix: str = "",
        auto_analyze: bool = False,
        context: Optional[Dict[str, Any]] = None,
    ) -> CaptureResult:
        """执行抓拍"""
        self._capture_trigger.trigger_capture(frame=frame, metadata=metadata, suffix=suffix)

        capture_path = None
        if isinstance(self._capture_trigger, LocalFileCaptureTrigger):
            capture_path = self._capture_trigger.latest_capture_path()

        analysis = None
        analysis_error = None
        if auto_analyze and self._ai_assistant and capture_path:
            try:
                analysis = self._run_ai_analysis(capture_path, context or {})
            except Exception as exc:
                analysis_error = str(exc)

        if self._runtime_state is not None:
            self._runtime_state.latest_capture_path = capture_path
            self._runtime_state.latest_capture_analysis = analysis
            self._runtime_state.latest_capture_error = analysis_error

        return CaptureResult(
            path=capture_path,
            analysis=analysis,
            analysis_error=analysis_error,
        )

    def _run_ai_analysis(self, image_path: str, context: Dict[str, Any]) -> CaptureAnalysis:
        """运行AI分析"""
        if not self._ai_assistant:
            raise RuntimeError("AI助手未初始化")
        return self._ai_assistant.analyze_capture(image_path, context=context)

    def get_latest_capture_path(self) -> Optional[str]:
        """获取最近一次抓拍路径"""
        if self._runtime_state is not None:
            return self._runtime_state.latest_capture_path
        if isinstance(self._capture_trigger, LocalFileCaptureTrigger):
            return self._capture_trigger.latest_capture_path()
        return None
