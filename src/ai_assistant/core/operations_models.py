"""运维操作相关的数据模型定义"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List


class SSHMethod(Enum):
    """SSH 认证方式"""
    PASSWORD = "password"
    KEY = "key"


class OperationRisk(Enum):
    """运维操作风险等级"""
    LOW = "low"           # 只读操作，无需审批
    MEDIUM = "medium"     # 可恢复操作，需要审批
    HIGH = "high"         # 破坏性操作，需要审批且有更严格的条件


@dataclass
class SSHConfig:
    """SSH 连接配置"""
    host: str
    port: int = 22
    username: str = "root"
    method: SSHMethod = SSHMethod.PASSWORD
    password: Optional[str] = None  # 加密后的密码
    key_path: Optional[str] = None  # 私钥路径
    key_passphrase: Optional[str] = None  # 私钥密码（加密后）
    timeout: int = 30


@dataclass
class Application:
    """应用程序定义"""
    name: str                           # 应用名称（唯一标识）
    display_name: str                   # 显示名称
    description: str = ""               # 应用描述
    machines: List[str] = field(default_factory=list)  # 关联的机器名称列表
    tags: List[str] = field(default_factory=list)      # 标签（用于分类和搜索）
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据


@dataclass
class Machine:
    """机器/服务器定义"""
    name: str                           # 机器名称（唯一标识）
    display_name: str                   # 显示名称
    ssh_config: SSHConfig               # SSH 连接配置
    description: str = ""               # 机器描述
    tags: List[str] = field(default_factory=list)      # 标签（用于分类和搜索）
    applications: List[str] = field(default_factory=list)  # 部署的应用列表
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据


@dataclass
class OperatorIdentity:
    """操作者身份信息"""
    user_id: str                        # 用户唯一标识
    username: str                       # 用户名
    display_name: Optional[str] = None  # 显示名称
    roles: List[str] = field(default_factory=list)  # 角色列表
    permissions: List[str] = field(default_factory=list)  # 权限列表
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据


@dataclass
class PendingOperation:
    """待审批的运维操作"""
    operation_id: str                   # 操作唯一标识
    operator: OperatorIdentity          # 操作者
    machine: Machine                    # 目标机器
    command: str                        # 待执行的命令
    risk_level: OperationRisk           # 风险等级
    reason: str = ""                    # 操作原因
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None  # 过期时间
    status: str = "pending"             # "pending" | "approved" | "rejected" | "expired" | "executed"
    approver: Optional[str] = None      # 审批者
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
