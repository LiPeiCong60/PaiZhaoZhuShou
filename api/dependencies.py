from __future__ import annotations

from fastapi import HTTPException

from api.session_manager import ApiSessionContext, session_manager


def require_session() -> ApiSessionContext:
    session = session_manager.current_session()
    if session is None:
        raise HTTPException(status_code=503, detail="服务未初始化，请先创建会话")
    return session
