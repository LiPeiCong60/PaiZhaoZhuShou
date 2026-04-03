"""
模板相关路由
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from api.dependencies import require_session
from api.session_manager import ApiSessionContext

router = APIRouter(prefix="/api/v1/templates", tags=["模板"])


@router.post("/import")
async def import_template(
    file: UploadFile = File(...),
    name: str | None = None,
    session: ApiSessionContext = Depends(require_session),
) -> dict:
    """导入模板"""
    suffix = os.path.splitext(file.filename or "")[1] or ".jpg"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="template_upload_", suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(await file.read())

        profile = session.template_service.import_template(
            tmp_path,
            name=name or os.path.splitext(file.filename or "template")[0],
        )
        session.template_service.select_template(profile.template_id)
        return {
            "template_id": profile.template_id,
            "name": profile.name,
            "created": True,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@router.get("/")
async def list_templates(session: ApiSessionContext = Depends(require_session)) -> list[dict]:
    """获取模板列表"""
    try:
        templates = session.template_service.list_templates()
        return [
            {
                "template_id": t.template_id,
                "name": t.name,
                "created_at": t.created_at,
                "image_path": t.image_path,
            }
            for t in templates
        ]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/select")
async def select_template(template_id: str, session: ApiSessionContext = Depends(require_session)) -> dict:
    """选择模板"""
    try:
        if session.template_service.select_template(template_id):
            return {"message": f"已选择模板 {template_id}", "selected_template_id": template_id}
        raise HTTPException(status_code=404, detail="模板不存在")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{template_id}")
async def delete_template(template_id: str, session: ApiSessionContext = Depends(require_session)) -> dict:
    """删除模板"""
    try:
        if session.template_service.delete_template(template_id):
            return {"message": "模板已删除"}
        raise HTTPException(status_code=404, detail="模板不存在")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
