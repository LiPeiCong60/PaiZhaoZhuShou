from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TARGET_BOX_NORM: tuple[float, float, float, float] = (0.38, 0.18, 0.24, 0.66)


@dataclass(slots=True)
class CaptureAnalysis:
    score: float
    summary: str
    suggestions: list[str]


@dataclass(slots=True)
class BackgroundAnalysis:
    score: float
    summary: str
    placement: str
    camera_angle: str
    lighting: str
    suggestions: list[str]
    recommended_pan_delta: float = 0.0
    recommended_tilt_delta: float = 0.0
    target_box_norm: tuple[float, float, float, float] = DEFAULT_TARGET_BOX_NORM


@dataclass(slots=True)
class TemplateBackgroundGuidance:
    reproducibility_score: float
    summary: str
    feasibility: str
    placement: str
    camera_angle: str
    pose_tip: str
    suggestions: list[str]


@dataclass(slots=True)
class BatchPickResult:
    """Result of batch picking the best image from multiple candidates."""
    best_index: int
    score: float
    summary: str
    suggestions: list[str]


@dataclass(slots=True)
class BatchBackgroundPickResult:
    """Result of batch picking the best background angle."""
    best_index: int
    score: float
    summary: str
    placement: str
    camera_angle: str
    lighting: str
    suggestions: list[str]
    recommended_pan_delta: float = 0.0
    recommended_tilt_delta: float = 0.0
    target_box_norm: tuple[float, float, float, float] = DEFAULT_TARGET_BOX_NORM


class AIPhotoAssistant(ABC):
    @abstractmethod
    def reply(self, message: str, context: dict[str, Any] | None = None) -> str:
        raise NotImplementedError

    @abstractmethod
    def analyze_capture(self, image_path: str, context: dict[str, Any] | None = None) -> CaptureAnalysis:
        raise NotImplementedError

    @abstractmethod
    def analyze_background(
        self, image_path: str, context: dict[str, Any] | None = None
    ) -> BackgroundAnalysis:
        raise NotImplementedError

    @abstractmethod
    def guide_with_template_and_background(
        self,
        *,
        template_image_path: str,
        background_image_path: str,
        context: dict[str, Any] | None = None,
    ) -> TemplateBackgroundGuidance:
        raise NotImplementedError

    @abstractmethod
    def pick_best_from_batch(
        self, image_paths: list[str],
    ) -> BatchPickResult:
        raise NotImplementedError

    @abstractmethod
    def pick_best_background_from_batch(
        self, image_paths: list[str],
    ) -> BatchBackgroundPickResult:
        raise NotImplementedError


class MockAIPhotoAssistant(AIPhotoAssistant):
    def reply(self, message: str, context: dict[str, Any] | None = None) -> str:
        return "AI接口预留中。后续接入后可返回拍照评分、构图建议、姿态优化建议。"

    def analyze_capture(self, image_path: str, context: dict[str, Any] | None = None) -> CaptureAnalysis:
        return CaptureAnalysis(
            score=0.0,
            summary="尚未接入真实AI评分模型",
            suggestions=[
                "可接入云端多模态模型进行审美评分",
                "可加入历史模板偏好学习",
                "可加入环境光与曝光分析",
            ],
        )

    def analyze_background(
        self, image_path: str, context: dict[str, Any] | None = None
    ) -> BackgroundAnalysis:
        return BackgroundAnalysis(
            score=0.0,
            summary="尚未接入真实背景分析模型",
            placement="人物站在画面中心略偏下",
            camera_angle="镜头略低于眼平线，轻微上仰",
            lighting="主光从人物侧前方 30-45 度",
            suggestions=[
                "清理画面边缘杂物",
                "避免顶灯直射造成面部阴影",
                "保留背景层次，避免人物紧贴背景",
            ],
            recommended_pan_delta=0.0,
            recommended_tilt_delta=0.0,
            target_box_norm=DEFAULT_TARGET_BOX_NORM,
        )

    def guide_with_template_and_background(
        self,
        *,
        template_image_path: str,
        background_image_path: str,
        context: dict[str, Any] | None = None,
    ) -> TemplateBackgroundGuidance:
        return TemplateBackgroundGuidance(
            reproducibility_score=0.0,
            summary="尚未接入模板与背景联合指导模型",
            feasibility="中",
            placement="先站在背景主体区域，再微调左右位置",
            camera_angle="先眼平位，再小幅俯仰微调",
            pose_tip="先对齐躯干和头肩方向，再调整手臂细节",
            suggestions=[
                "优先还原模板中的身体朝向",
                "利用背景纵深留出主体空间",
                "先拍一张测试图再细调动作",
            ],
        )

    def pick_best_from_batch(self, image_paths: list[str]) -> BatchPickResult:
        return BatchPickResult(
            best_index=0,
            score=0.0,
            summary="尚未接入AI批量分析",
            suggestions=["请接入AI模型"],
        )

    def pick_best_background_from_batch(self, image_paths: list[str]) -> BatchBackgroundPickResult:
        return BatchBackgroundPickResult(
            best_index=0,
            score=0.0,
            summary="尚未接入AI批量背景分析",
            placement="人物站在画面中心",
            camera_angle="眼平机位",
            lighting="侧前方打光",
            suggestions=["请接入AI模型"],
        )


class SiliconFlowAIPhotoAssistant(AIPhotoAssistant):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "Pro/moonshotai/Kimi-K2.5",
        endpoint: str = "https://api.siliconflow.cn/v1/chat/completions",
        timeout_s: float = 45.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._endpoint = endpoint.strip()
        self._timeout_s = max(5.0, float(timeout_s))
        self._max_retries = max(0, int(max_retries))
        if not self._api_key:
            raise ValueError("Missing SILICONFLOW_API_KEY")

    def reply(self, message: str, context: dict[str, Any] | None = None) -> str:
        ctx = context or {}
        sys_prompt = (
            "你是智能云台拍照助手。请给出简短、可执行建议，优先中文。"
            "如果用户在模板引导模式，优先给构图和姿态建议。"
        )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": f"上下文: {json.dumps(ctx, ensure_ascii=False)}\n用户问题: {message}",
                },
            ],
            "temperature": 0.4,
            "max_tokens": 250,
        }
        return self._chat(payload)

    def analyze_capture(self, image_path: str, context: dict[str, Any] | None = None) -> CaptureAnalysis:
        prompt = (
            "评估照片，输出严格JSON："
            '{"score":0-100,"summary":"一句话","subscores":{"composition":0-100,"pose":0-100,"lighting":0-100,"background":0-100},"suggestions":["建议1","建议2"]}'
        )
        image_url = _encode_image_as_data_url(image_path)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是摄影评估助手，只输出JSON。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
                    ],
                },
            ],
            "temperature": 0.1,
            "max_tokens": 300,
        }
        text = self._chat(payload)
        return _parse_capture_analysis(text)

    def analyze_background(
        self, image_path: str, context: dict[str, Any] | None = None
    ) -> BackgroundAnalysis:
        prompt = (
            "分析背景照片给出拍摄建议，输出严格JSON："
            '{"score":0-100,"summary":"一句话","placement":"站位建议","camera_angle":"机位建议","lighting":"光线建议",'
            '"recommended_pan_delta":数字,"recommended_tilt_delta":数字,"target_box_norm":[x,y,w,h],"suggestions":["建议1","建议2"]}'
        )
        image_url = _encode_image_as_data_url(image_path)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是摄影指导，只输出JSON。pan_delta/tilt_delta范围-20到20，target_box_norm各值0到1。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
                    ],
                },
            ],
            "temperature": 0.1,
            "max_tokens": 350,
        }
        text = self._chat(payload)
        return _parse_background_analysis(text)

    def guide_with_template_and_background(
        self,
        *,
        template_image_path: str,
        background_image_path: str,
        context: dict[str, Any] | None = None,
    ) -> TemplateBackgroundGuidance:
        prompt = (
            "第一张是模板图，第二张是背景图。给出联合拍摄指导，输出严格JSON："
            '{"reproducibility_score":0-100,"summary":"一句话","feasibility":"高/中/低","placement":"站位建议","camera_angle":"机位建议","pose_tip":"姿势要点","suggestions":["建议1","建议2"]}'
        )
        template_url = _encode_image_as_data_url(template_image_path)
        background_url = _encode_image_as_data_url(background_image_path)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是摄影导演，只输出JSON。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": template_url, "detail": "low"}},
                        {"type": "image_url", "image_url": {"url": background_url, "detail": "low"}},
                    ],
                },
            ],
            "temperature": 0.1,
            "max_tokens": 350,
        }
        text = self._chat(payload)
        return _parse_template_background_guidance(text)

    def pick_best_from_batch(self, image_paths: list[str]) -> BatchPickResult:
        n = len(image_paths)
        prompt = (
            f"以下{n}张候选照片编号1到{n}，选出拍摄效果最佳的一张，输出严格JSON："
            '{"best_index":编号(1起),  "score":0-100,"summary":"一句话","suggestions":["建议1","建议2"]}'
        )
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in image_paths:
            url = _encode_image_as_data_url(path)
            content_parts.append({"type": "image_url", "image_url": {"url": url, "detail": "low"}})
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是摄影评估助手，只输出JSON。"},
                {"role": "user", "content": content_parts},
            ],
            "temperature": 0.1,
            "max_tokens": 250,
        }
        text = self._chat(payload)
        return _parse_batch_pick_result(text, n)

    def pick_best_background_from_batch(self, image_paths: list[str]) -> BatchBackgroundPickResult:
        n = len(image_paths)
        prompt = (
            f"以下{n}张不同角度的背景照片编号1到{n}，选出最适合人像拍摄的角度，输出严格JSON："
            '{"best_index":编号(1起),"score":0-100,"summary":"一句话","placement":"站位建议",'
            '"camera_angle":"机位建议","lighting":"光线建议",'
            '"recommended_pan_delta":数字,"recommended_tilt_delta":数字,'
            '"target_box_norm":[x,y,w,h],"suggestions":["建议1","建议2"]}'
        )
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for path in image_paths:
            url = _encode_image_as_data_url(path)
            content_parts.append({"type": "image_url", "image_url": {"url": url, "detail": "low"}})
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "你是摄影指导，只输出JSON。pan/tilt_delta范围-20到20，target_box_norm各值0到1。"},
                {"role": "user", "content": content_parts},
            ],
            "temperature": 0.1,
            "max_tokens": 400,
        }
        text = self._chat(payload)
        return _parse_batch_background_pick_result(text, n)

    def _chat(self, payload: dict[str, Any]) -> str:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        total_attempts = self._max_retries + 1
        for i in range(total_attempts):
            # 每次重试都创建新的 Request 对象，避免复用导致的流状态问题
            req = urllib.request.Request(
                self._endpoint,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    body = resp.read().decode("utf-8", errors="ignore")
                    reply_payload = json.loads(body)
                    return _extract_choice_content(reply_payload)
            except (
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
                json.JSONDecodeError,
                RuntimeError,
                http.client.HTTPException,
            ) as exc:
                last_error = exc
                logger.warning(
                    "SiliconFlow attempt %d/%d failed: %s: %s",
                    i + 1, total_attempts, type(exc).__name__, exc,
                )
                if i < self._max_retries:
                    time.sleep(min(2.0, 0.5 * (i + 1)))
                else:
                    break
        raise RuntimeError(
            f"AI请求失败(重试{total_attempts}次): {type(last_error).__name__}: {last_error}"
        )


def build_ai_assistant_from_env() -> AIPhotoAssistant:
    api_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        return MockAIPhotoAssistant()
    model = os.getenv("SILICONFLOW_MODEL", "Pro/moonshotai/Kimi-K2.5").strip()
    endpoint = os.getenv("SILICONFLOW_ENDPOINT", "https://api.siliconflow.cn/v1/chat/completions").strip()
    timeout_s = float(os.getenv("SILICONFLOW_TIMEOUT_S", "45"))
    return SiliconFlowAIPhotoAssistant(
        api_key=api_key,
        model=model,
        endpoint=endpoint,
        timeout_s=timeout_s,
    )


def _extract_choice_content(payload: dict[str, Any]) -> str:
    # 先检查 API 是否返回了错误
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        msg = error_obj.get("message", "unknown error")
        code = error_obj.get("code", "")
        raise RuntimeError(f"API error ({code}): {msg}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"Invalid response: missing choices, keys={list(payload.keys())}")
    msg = choices[0].get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
        if parts:
            return "\n".join(parts).strip()
    raise RuntimeError(f"Invalid response: unsupported content type={type(content).__name__}")


def _encode_image_as_data_url(image_path: str) -> str:
    ext = os.path.splitext(image_path)[1].lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")
    optimized = _read_image_bytes_for_upload(image_path, prefer_jpeg=(mime != "image/png"))
    if optimized is None:
        with open(image_path, "rb") as f:
            raw = f.read()
    else:
        raw, mime = optimized
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _read_image_bytes_for_upload(
    image_path: str, *, prefer_jpeg: bool
) -> tuple[bytes, str] | None:
    """
    Compresses and resizes images before upload to reduce latency and payload.
    Falls back to raw bytes when OpenCV is unavailable or decode fails.
    """
    try:
        import cv2
    except Exception:
        return None
    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        return None
    h, w = image.shape[:2]
    max_side = 720
    if max(h, w) > max_side:
        scale = float(max_side) / float(max(h, w))
        nw = max(2, int(w * scale))
        nh = max(2, int(h * scale))
        image = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_AREA)
    if prefer_jpeg:
        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        mime = "image/jpeg"
    else:
        ok, encoded = cv2.imencode(".png", image, [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
        mime = "image/png"
    if not ok:
        return None
    return encoded.tobytes(), mime


def _parse_capture_analysis(text: str) -> CaptureAnalysis:
    obj = _extract_json_obj(text)
    if obj is None:
        return CaptureAnalysis(
            score=0.0,
            summary=text.strip() or "模型未返回结构化评分",
            suggestions=["请重试一次", "检查图片是否清晰", "检查网络连接"],
        )
    try:
        score = float(obj.get("score", 0.0))
    except Exception:
        score = 0.0
    score = max(0.0, min(100.0, score))
    summary = str(obj.get("summary", ""))
    subs = obj.get("subscores", {})
    subs_text = ""
    if isinstance(subs, dict) and subs:
        parts = []
        for k in ("composition", "pose", "lighting", "background"):
            if k in subs:
                parts.append(f"{k}={subs[k]}")
        if parts:
            subs_text = " | " + ", ".join(parts)
    suggestions_raw = obj.get("suggestions", [])
    suggestions: list[str] = []
    if isinstance(suggestions_raw, list):
        suggestions = [str(x) for x in suggestions_raw if str(x).strip()]
    if not suggestions:
        suggestions = ["保持主体清晰", "优化光线方向", "减少背景干扰"]
    return CaptureAnalysis(
        score=score,
        summary=(summary + subs_text).strip(),
        suggestions=suggestions[:5],
    )


def _parse_background_analysis(text: str) -> BackgroundAnalysis:
    obj = _extract_json_obj(text)
    if obj is None:
        return BackgroundAnalysis(
            score=0.0,
            summary=text.strip() or "模型未返回结构化背景分析",
            placement="建议人物站在画面主体区域",
            camera_angle="建议保持眼平机位后再微调",
            lighting="建议补充侧前方柔光",
            suggestions=["清理背景杂物", "保证主体与背景有层次", "避免复杂高亮背景"],
            recommended_pan_delta=0.0,
            recommended_tilt_delta=0.0,
            target_box_norm=DEFAULT_TARGET_BOX_NORM,
        )
    try:
        score = float(obj.get("score", 0.0))
    except Exception:
        score = 0.0
    score = max(0.0, min(100.0, score))
    summary = str(obj.get("summary", "")).strip() or "背景可优化后再拍摄"
    placement = str(obj.get("placement", "")).strip() or "人物站在画面中心略偏下"
    camera_angle = str(obj.get("camera_angle", "")).strip() or "镜头略低于眼平线"
    lighting = str(obj.get("lighting", "")).strip() or "主光从侧前方打光"
    suggestions_raw = obj.get("suggestions", [])
    suggestions: list[str] = []
    if isinstance(suggestions_raw, list):
        suggestions = [str(x).strip() for x in suggestions_raw if str(x).strip()]
    if not suggestions:
        suggestions = ["清理背景杂物", "优化人物与背景距离", "补足主光源"]
    try:
        recommended_pan_delta = float(obj.get("recommended_pan_delta", 0.0))
    except Exception:
        recommended_pan_delta = 0.0
    try:
        recommended_tilt_delta = float(obj.get("recommended_tilt_delta", 0.0))
    except Exception:
        recommended_tilt_delta = 0.0
    recommended_pan_delta = max(-20.0, min(20.0, recommended_pan_delta))
    recommended_tilt_delta = max(-15.0, min(15.0, recommended_tilt_delta))
    target_box_norm = _safe_box_norm(obj.get("target_box_norm"))
    return BackgroundAnalysis(
        score=score,
        summary=summary,
        placement=placement,
        camera_angle=camera_angle,
        lighting=lighting,
        suggestions=suggestions[:5],
        recommended_pan_delta=recommended_pan_delta,
        recommended_tilt_delta=recommended_tilt_delta,
        target_box_norm=target_box_norm,
    )


def _parse_template_background_guidance(text: str) -> TemplateBackgroundGuidance:
    obj = _extract_json_obj(text)
    if obj is None:
        return TemplateBackgroundGuidance(
            reproducibility_score=0.0,
            summary=text.strip() or "模型未返回结构化联合指导",
            feasibility="中",
            placement="人物先站在背景主体区域",
            camera_angle="机位保持眼平并微调俯仰",
            pose_tip="先还原躯干朝向再还原手部细节",
            suggestions=["先试拍一张", "调整人物与背景距离", "按模板先还原大姿态"],
        )
    try:
        reproducibility_score = float(obj.get("reproducibility_score", 0.0))
    except Exception:
        reproducibility_score = 0.0
    reproducibility_score = max(0.0, min(100.0, reproducibility_score))
    summary = str(obj.get("summary", "")).strip() or "可根据背景调整后复刻模板"
    feasibility = str(obj.get("feasibility", "")).strip() or "中"
    placement = str(obj.get("placement", "")).strip() or "人物先站在画面主体区域"
    camera_angle = str(obj.get("camera_angle", "")).strip() or "先眼平机位后小幅微调"
    pose_tip = str(obj.get("pose_tip", "")).strip() or "先对齐躯干，再对齐手臂和头部角度"
    suggestions_raw = obj.get("suggestions", [])
    suggestions: list[str] = []
    if isinstance(suggestions_raw, list):
        suggestions = [str(x).strip() for x in suggestions_raw if str(x).strip()]
    if not suggestions:
        suggestions = ["先完成站位", "再微调机位", "最后修姿势细节"]
    return TemplateBackgroundGuidance(
        reproducibility_score=reproducibility_score,
        summary=summary,
        feasibility=feasibility,
        placement=placement,
        camera_angle=camera_angle,
        pose_tip=pose_tip,
        suggestions=suggestions[:5],
    )


def _extract_json_obj(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", stripped)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _safe_box_norm(raw: Any) -> tuple[float, float, float, float]:
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            x, y, w, h = [float(v) for v in raw]
            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            w = max(0.08, min(1.0, w))
            h = max(0.12, min(1.0, h))
            if x + w > 1.0:
                x = max(0.0, 1.0 - w)
            if y + h > 1.0:
                y = max(0.0, 1.0 - h)
            return (x, y, w, h)
        except Exception:
            pass
    return DEFAULT_TARGET_BOX_NORM


def _parse_batch_pick_result(text: str, n: int) -> BatchPickResult:
    obj = _extract_json_obj(text)
    if obj is None:
        return BatchPickResult(best_index=0, score=0.0, summary=text.strip() or "模型未返回结构化结果", suggestions=["请重试"])
    try:
        best_index = int(obj.get("best_index", 1)) - 1  # 1-based -> 0-based
    except Exception:
        best_index = 0
    best_index = max(0, min(n - 1, best_index))
    try:
        score = float(obj.get("score", 0.0))
    except Exception:
        score = 0.0
    score = max(0.0, min(100.0, score))
    summary = str(obj.get("summary", "")).strip() or "已选出最佳照片"
    suggestions_raw = obj.get("suggestions", [])
    suggestions: list[str] = []
    if isinstance(suggestions_raw, list):
        suggestions = [str(x).strip() for x in suggestions_raw if str(x).strip()]
    if not suggestions:
        suggestions = ["保持主体清晰", "优化光线方向"]
    return BatchPickResult(best_index=best_index, score=score, summary=summary, suggestions=suggestions[:5])


def _parse_batch_background_pick_result(text: str, n: int) -> BatchBackgroundPickResult:
    obj = _extract_json_obj(text)
    if obj is None:
        return BatchBackgroundPickResult(
            best_index=0, score=0.0, summary=text.strip() or "模型未返回结构化结果",
            placement="画面中心", camera_angle="眼平机位", lighting="自然光",
            suggestions=["请重试"],
        )
    try:
        best_index = int(obj.get("best_index", 1)) - 1
    except Exception:
        best_index = 0
    best_index = max(0, min(n - 1, best_index))
    try:
        score = float(obj.get("score", 0.0))
    except Exception:
        score = 0.0
    score = max(0.0, min(100.0, score))
    summary = str(obj.get("summary", "")).strip() or "已选出最佳背景角度"
    placement = str(obj.get("placement", "")).strip() or "人物站在画面中心"
    camera_angle = str(obj.get("camera_angle", "")).strip() or "眼平机位"
    lighting = str(obj.get("lighting", "")).strip() or "侧前方打光"
    suggestions_raw = obj.get("suggestions", [])
    suggestions: list[str] = []
    if isinstance(suggestions_raw, list):
        suggestions = [str(x).strip() for x in suggestions_raw if str(x).strip()]
    if not suggestions:
        suggestions = ["清理背景杂物", "优化人物与背景距离"]
    try:
        recommended_pan_delta = float(obj.get("recommended_pan_delta", 0.0))
    except Exception:
        recommended_pan_delta = 0.0
    try:
        recommended_tilt_delta = float(obj.get("recommended_tilt_delta", 0.0))
    except Exception:
        recommended_tilt_delta = 0.0
    recommended_pan_delta = max(-20.0, min(20.0, recommended_pan_delta))
    recommended_tilt_delta = max(-15.0, min(15.0, recommended_tilt_delta))
    target_box_norm = _safe_box_norm(obj.get("target_box_norm"))
    return BatchBackgroundPickResult(
        best_index=best_index, score=score, summary=summary,
        placement=placement, camera_angle=camera_angle, lighting=lighting,
        suggestions=suggestions[:5],
        recommended_pan_delta=recommended_pan_delta,
        recommended_tilt_delta=recommended_tilt_delta,
        target_box_norm=target_box_norm,
    )
