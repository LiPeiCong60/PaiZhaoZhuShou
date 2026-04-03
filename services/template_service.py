"""
模板服务模块
负责模板的上传、删除、选择和查询
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from repositories.template_repository import TemplateRepository
from services.runtime_state import RuntimeState
from template_compose import TemplateProfile


class TemplateService:
    """模板服务类，封装所有模板管理逻辑"""

    def __init__(
        self,
        repository: TemplateRepository,
        runtime_state: Optional[RuntimeState] = None,
        detector_factory: Optional[Callable[[], object]] = None,
        storage_dir: str = ".template_library/images",
    ) -> None:
        self._repository = repository
        self._runtime_state = runtime_state
        self._detector_factory = detector_factory or self._build_default_detector
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def import_template(self, image_path: str, name: Optional[str] = None) -> Optional[TemplateProfile]:
        """导入新模板"""
        import cv2

        from template_compose import TemplateComposeEngine

        image = cv2.imread(image_path)
        if image is None:
            raise ValueError("模板读取失败: 图片无法打开")

        detector = self._detector_factory()
        vision = detector.detect(image)

        candidates = vision.tracking_candidates or []
        detection = max(candidates, key=lambda d: d.bbox.area) if candidates else vision.tracking_detection

        if detection is None:
            raise ValueError("模板创建失败: 未检测到人物")

        if name is None:
            name = os.path.splitext(os.path.basename(image_path))[0]

        stored_image_path = self._persist_template_image(image_path)
        profile = TemplateComposeEngine.create_profile(name, str(stored_image_path), detection, image.shape)
        if profile is None:
            raise ValueError("模板创建失败: 未检测到有效人物区域")

        self._repository.add(profile)
        return profile

    def delete_template(self, template_id: str) -> bool:
        """删除模板"""
        removed = self._repository.remove(template_id)
        if removed and self._runtime_state is not None and self._runtime_state.selected_template_id == template_id:
            self._runtime_state.selected_template_id = None
        return removed

    def get_template(self, template_id: str) -> Optional[TemplateProfile]:
        """获取模板"""
        return self._repository.get(template_id)

    def list_templates(self) -> List[TemplateProfile]:
        """列出所有模板"""
        return self._repository.list_all()

    def select_template(self, template_id: str) -> bool:
        """选择模板"""
        if not self._repository.exists(template_id):
            return False
        if self._runtime_state is not None:
            self._runtime_state.selected_template_id = template_id
        return True

    def get_selected_template_id(self) -> Optional[str]:
        """获取当前选中的模板ID"""
        if self._runtime_state is None:
            return None
        selected_id = self._runtime_state.selected_template_id
        if selected_id and self._repository.exists(selected_id):
            return selected_id
        return None

    def get_selected_template(self) -> Optional[TemplateProfile]:
        """获取当前选中的模板"""
        selected_id = self.get_selected_template_id()
        if not selected_id:
            return None
        return self._repository.get(selected_id)

    def clear_selected_template(self) -> None:
        """清空当前选中的模板"""
        if self._runtime_state is not None:
            self._runtime_state.selected_template_id = None

    def _persist_template_image(self, image_path: str) -> Path:
        suffix = Path(image_path).suffix or ".jpg"
        target_path = self._storage_dir / f"{uuid.uuid4().hex}{suffix}"
        shutil.copy2(image_path, target_path)
        return target_path

    @staticmethod
    def _build_default_detector():
        from config import DetectionConfig
        from detector import MediaPipeVisionDetector

        return MediaPipeVisionDetector(DetectionConfig())
