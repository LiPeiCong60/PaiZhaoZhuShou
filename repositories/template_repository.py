"""
模板仓储接口
定义模板数据的访问契约，支持本地和远程实现
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from template_compose import TemplateProfile


class TemplateRepository(ABC):
    """模板仓储抽象接口"""

    @abstractmethod
    def add(self, profile: TemplateProfile) -> None:
        """添加模板"""
        pass

    @abstractmethod
    def remove(self, template_id: str) -> bool:
        """移除模板"""
        pass

    @abstractmethod
    def get(self, template_id: str) -> Optional[TemplateProfile]:
        """获取模板"""
        pass

    @abstractmethod
    def list_all(self) -> List[TemplateProfile]:
        """列出所有模板"""
        pass

    @abstractmethod
    def exists(self, template_id: str) -> bool:
        """检查模板是否存在"""
        pass