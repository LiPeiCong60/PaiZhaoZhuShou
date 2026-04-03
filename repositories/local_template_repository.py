"""
本地模板仓储实现
基于文件系统存储模板数据
"""

from typing import List, Optional

from template_compose import TemplateLibrary, TemplateProfile
from repositories.template_repository import TemplateRepository


class LocalTemplateRepository(TemplateRepository):
    """本地模板仓储实现"""

    def __init__(self, template_library: TemplateLibrary) -> None:
        self._library = template_library

    def add(self, profile: TemplateProfile) -> None:
        """添加模板"""
        self._library.add(profile)

    def remove(self, template_id: str) -> bool:
        """移除模板"""
        try:
            self._library.remove(template_id)
            return True
        except Exception:
            return False

    def get(self, template_id: str) -> Optional[TemplateProfile]:
        """获取模板"""
        return self._library.get(template_id)

    def list_all(self) -> List[TemplateProfile]:
        """列出所有模板"""
        return self._library.list_templates()

    def exists(self, template_id: str) -> bool:
        """检查模板是否存在"""
        return self._library.get(template_id) is not None