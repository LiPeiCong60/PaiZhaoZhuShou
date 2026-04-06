"""
AI相关路由
"""

from __future__ import annotations

import os
import tempfile
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from api.dependencies import require_session
from api.session_manager import ApiSessionContext, session_manager
from interfaces.ai_assistant import AIPhotoAssistant, BackgroundAnalysis, CaptureAnalysis, build_ai_assistant_from_env

router = APIRouter(prefix="/api/v1/ai", tags=["AI"])


def _resolve_upload_analysis_context() -> tuple[AIPhotoAssistant, dict[str, object], bool]:
    session = session_manager.current_session()
    if session is not None:
        return session.ai_assistant, session.build_ai_context(), True
    return build_ai_assistant_from_env(), {}, False


def _ensure_valid_image(image_path: str) -> None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(image_path) as image:
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("上传文件不是有效图片") from exc


def _serialize_photo_analysis(analysis: CaptureAnalysis) -> dict:
    return {
        "score": analysis.score,
        "summary": analysis.summary,
        "suggestions": analysis.suggestions,
    }


def _serialize_background_analysis(analysis: BackgroundAnalysis) -> dict:
    return {
        "score": analysis.score,
        "summary": analysis.summary,
        "placement": analysis.placement,
        "camera_angle": analysis.camera_angle,
        "lighting": analysis.lighting,
        "suggestions": analysis.suggestions,
        "recommended_pan_delta": analysis.recommended_pan_delta,
        "recommended_tilt_delta": analysis.recommended_tilt_delta,
        "target_box_norm": list(analysis.target_box_norm),
    }


@router.post("/analyze-upload")
async def analyze_uploaded_photo(
    file: UploadFile = File(...),
    analysis_type: Literal["photo", "background"] = "photo",
) -> dict:
    """上传图片并立即执行 AI 分析"""
    suffix = os.path.splitext(file.filename or "")[1] or ".jpg"
    tmp_path = None
    try:
        raw = await file.read()
        if not raw:
            raise ValueError("上传图片为空")

        with tempfile.NamedTemporaryFile(prefix="ai_upload_", suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(raw)

        _ensure_valid_image(tmp_path)
        ai_assistant, context, used_session_context = _resolve_upload_analysis_context()

        if analysis_type == "background":
            analysis = ai_assistant.analyze_background(tmp_path, context=context)
            return {
                "message": "上传图片分析完成",
                "filename": file.filename,
                "analysis_type": analysis_type,
                "used_session_context": used_session_context,
                "analysis": _serialize_background_analysis(analysis),
            }

        analysis = ai_assistant.analyze_capture(tmp_path, context=context)
        return {
            "message": "上传图片分析完成",
            "filename": file.filename,
            "analysis_type": analysis_type,
            "used_session_context": used_session_context,
            "analysis": _serialize_photo_analysis(analysis),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@router.post("/angle-search/start")
async def start_angle_search(
    pan_range: float = 6.0,
    tilt_range: float = 3.0,
    pan_step: float = 4.0,
    tilt_step: float = 3.0,
    max_candidates: int = 9,
    settle_s: float = 0.35,
    session: ApiSessionContext = Depends(require_session),
) -> dict:
    """启动自动找角度"""
    scan_config = {
        "pan_range": pan_range,
        "tilt_range": tilt_range,
        "pan_step": pan_step,
        "tilt_step": tilt_step,
        "max_candidates": max_candidates,
        "settle_s": settle_s,
    }
    try:
        session.start_angle_search_async(scan_config)
        return {"message": "AI自动找角度已启动", "ai_angle_search_running": True}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/background-lock/start")
async def start_background_lock(
    pan_range: float = 6.0,
    tilt_range: float = 3.0,
    pan_step: float = 4.0,
    tilt_step: float = 3.0,
    max_candidates: int = 9,
    settle_s: float = 0.35,
    delay_s: float = 0.0,
    session: ApiSessionContext = Depends(require_session),
) -> dict:
    """启动背景扫描锁机位"""
    scan_config = {
        "pan_range": pan_range,
        "tilt_range": tilt_range,
        "pan_step": pan_step,
        "tilt_step": tilt_step,
        "max_candidates": max_candidates,
        "settle_s": settle_s,
    }
    try:
        session.start_background_lock_async(scan_config, delay_s)
        return {"message": "背景扫描锁机位已启动"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/background-lock/unlock")
async def unlock_background_lock(session: ApiSessionContext = Depends(require_session)) -> dict:
    """解除锁机位"""
    try:
        session.ai_orchestrator.unlock_background_lock()
        return {"message": "已解除AI机位锁定"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
