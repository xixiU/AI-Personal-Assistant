"""审批提供者抽象接口"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from .operations_models import PendingOperation, OperatorIdentity


@dataclass
class ApprovalRequest:
    """审批请求数据"""
    operation: PendingOperation
    operator: OperatorIdentity
    request_message: str  # 审批请求描述信息


@dataclass
class ApprovalResponse:
    """审批响应数据"""
    approved: bool
    approver: Optional[str] = None
    reason: Optional[str] = None  # 拒绝原因或批准备注


class ApprovalProvider(ABC):
    """审批提供者抽象基类

    不同的审批方式需要继承此类实现具体逻辑：
    - 飞书卡片审批
    - 企业微信审批
    - 钉钉审批
    - Web 页面审批
    """

    @abstractmethod
    def send_approval_request(self, request: ApprovalRequest) -> str:
        """发送审批请求

        Args:
            request: 审批请求数据

        Returns:
            str: 审批请求的唯一标识（用于后续查询状态）

        Raises:
            Exception: 发送失败时抛出异常
        """
        pass

    @abstractmethod
    def get_approval_status(self, request_id: str) -> Optional[ApprovalResponse]:
        """查询审批状态

        Args:
            request_id: 审批请求标识

        Returns:
            Optional[ApprovalResponse]: 审批响应，如果还未审批则返回 None
        """
        pass

    @abstractmethod
    def cancel_approval_request(self, request_id: str) -> bool:
        """取消审批请求

        Args:
            request_id: 审批请求标识

        Returns:
            bool: 是否成功取消
        """
        pass
