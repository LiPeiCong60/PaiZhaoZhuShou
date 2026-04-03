"""
AI相关路由
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import require_session
from api.session_manager import ApiSessionContext

router = APIRouter(prefix="/api/v1/ai", tags=["AI"])


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
