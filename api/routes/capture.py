"""
抓拍相关路由
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import require_session
from api.session_manager import ApiSessionContext

router = APIRouter(prefix="/api/v1/capture", tags=["抓拍"])


@router.post("/manual")
async def manual_capture(
    auto_analyze: bool = False,
    session: ApiSessionContext = Depends(require_session),
) -> dict:
    """手动抓拍"""
    try:
        result = session.capture_manual(auto_analyze=auto_analyze)
        return {
            "message": "手动抓拍已完成",
            "capture_path": result.path,
            "analysis": (
                {
                    "score": result.analysis.score,
                    "summary": result.analysis.summary,
                    "suggestions": result.analysis.suggestions,
                }
                if result.analysis is not None
                else None
            ),
            "analysis_error": result.analysis_error,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
