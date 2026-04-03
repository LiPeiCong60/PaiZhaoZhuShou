"""
状态查询相关路由
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import require_session
from api.session_manager import ApiSessionContext

router = APIRouter(prefix="/api/v1/status", tags=["状态"])


@router.get("")
@router.get("/")
async def get_status(session: ApiSessionContext = Depends(require_session)) -> dict:
    """获取完整系统状态"""
    try:
        status = session.status_service.get_status()
        status.update(
            {
                "session_id": session.session_id,
                "last_angle_search_result": session.last_angle_search_result,
                "last_angle_search_error": session.last_angle_search_error,
                "last_background_lock_result": session.last_background_lock_result,
                "last_background_lock_error": session.last_background_lock_error,
            }
        )
        return status
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mode")
async def get_mode(session: ApiSessionContext = Depends(require_session)) -> dict:
    """获取当前模式"""
    try:
        status = session.status_service.get_status()
        return {"mode": status["mode"]}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/compose")
async def get_compose_status(session: ApiSessionContext = Depends(require_session)) -> dict:
    """获取模板构图状态"""
    try:
        status = session.status_service.get_status()
        return {
            "compose_score": status["compose_score"],
            "compose_ready": status["compose_ready"],
            "selected_template_id": status["selected_template_id"],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tracking")
async def get_tracking_status(session: ApiSessionContext = Depends(require_session)) -> dict:
    """获取跟踪状态"""
    try:
        status = session.status_service.get_status()
        return {
            "tracking_stable": status["tracking_stable"],
            "follow_mode": status["follow_mode"],
            "speed_mode": status["speed_mode"],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ai")
async def get_ai_status(session: ApiSessionContext = Depends(require_session)) -> dict:
    """获取AI状态"""
    try:
        status = session.status_service.get_status()
        return {
            "ai_angle_search_running": status["ai_angle_search_running"],
            "ai_lock_mode_enabled": status["ai_lock_mode_enabled"],
            "ai_lock_fit_score": status["ai_lock_fit_score"],
            "ai_lock_target_box_norm": status["ai_lock_target_box_norm"],
            "last_angle_search_result": session.last_angle_search_result,
            "last_angle_search_error": session.last_angle_search_error,
            "last_background_lock_result": session.last_background_lock_result,
            "last_background_lock_error": session.last_background_lock_error,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
