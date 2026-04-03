"""
API应用主入口
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.ai import router as ai_router
from api.routes.capture import router as capture_router
from api.routes.control import router as control_router
from api.routes.session import router as session_router
from api.routes.status import router as status_router
from api.routes.template import router as template_router

app = FastAPI(title="智能云台拍照助手 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(session_router)
app.include_router(control_router)
app.include_router(capture_router)
app.include_router(template_router)
app.include_router(ai_router)
app.include_router(status_router)


@app.get("/api/v1/health")
async def health_check() -> dict:
    """健康检查"""
    return {"status": "healthy", "version": "1.0.0"}
