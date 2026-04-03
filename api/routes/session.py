"""
会话相关路由
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.session_manager import SessionOpenPayload, session_manager

router = APIRouter(prefix="/api/v1/session", tags=["会话"])


@router.post("/open")
async def open_session(
    stream_url: str,
    mirror_view: bool = True,
    start_mode: str = "MANUAL",
) -> dict:
    """创建拍摄会话"""
    try:
        session = session_manager.open_session(
            SessionOpenPayload(
                stream_url=stream_url,
                mirror_view=mirror_view,
                start_mode=start_mode,
            )
        )
        return {
            "session_id": session.session_id,
            "status": "running",
            "message": "会话创建成功",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"创建会话失败: {exc}") from exc


@router.post("/close")
async def close_session() -> dict:
    """关闭拍摄会话"""
    if not session_manager.close_session():
        raise HTTPException(status_code=400, detail="没有活跃的会话")
    return {"message": "会话已关闭"}
