"""
控制相关路由
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import require_session
from api.session_manager import ApiSessionContext
from mode_manager import ControlMode

router = APIRouter(prefix="/api/v1/control", tags=["控制"])


@router.post("/mode")
async def set_mode(mode: str, session: ApiSessionContext = Depends(require_session)) -> dict:
    """切换控制模式"""
    try:
        session.control_service.set_mode(ControlMode(mode))
        return {"message": f"模式已切换为 {mode}", "mode": session.control_service.get_mode().value}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/manual-move")
async def manual_move(
    action: str | None = None,
    pan_delta: float | None = None,
    tilt_delta: float | None = None,
    session: ApiSessionContext = Depends(require_session),
) -> dict:
    """手动云台控制"""
    try:
        if pan_delta is not None or tilt_delta is not None:
            session.control_service.move_relative(float(pan_delta or 0.0), float(tilt_delta or 0.0))
            return {
                "message": "手动相对移动已执行",
                "pan_delta": float(pan_delta or 0.0),
                "tilt_delta": float(tilt_delta or 0.0),
            }
        if not action:
            raise ValueError("必须提供 action 或 pan_delta/tilt_delta")
        session.control_service.manual_move(action)
        return {"message": f"手动移动: {action}"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/home")
async def home(session: ApiSessionContext = Depends(require_session)) -> dict:
    """云台回中"""
    try:
        session.control_service.home()
        return {"message": "云台已回中"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/follow-mode")
async def set_follow_mode(follow_mode: str, session: ApiSessionContext = Depends(require_session)) -> dict:
    """设置跟随模式"""
    try:
        session.control_service.set_follow_mode(follow_mode)
        return {"message": f"跟随模式已设置为 {follow_mode}", "follow_mode": follow_mode}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/speed")
async def set_speed_mode(speed_mode: str, session: ApiSessionContext = Depends(require_session)) -> dict:
    """设置速度模式"""
    try:
        session.control_service.set_speed_mode(speed_mode)
        return {"message": f"速度模式已设置为 {speed_mode}", "speed_mode": speed_mode}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
