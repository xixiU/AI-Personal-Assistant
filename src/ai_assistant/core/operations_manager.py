"""运维操作管理器核心实现"""

import os
import re
import uuid
import paramiko
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from loguru import logger
from cryptography.fernet import Fernet

from .operations_models import (
    SSHMethod,
    OperationRisk,
    SSHConfig,
    Application,
    Machine,
    OperatorIdentity,
    PendingOperation
)
from .approval_provider import ApprovalProvider, ApprovalRequest, ApprovalResponse


class OperationsManager:
    """运维操作管理器

    负责：
    1. 机器和应用管理
    2. SSH 连接池管理
    3. 命令执行（安全控制）
    4. 权限验证
    5. 审批流程管理
    6. 风险评估
    """

    def __init__(
        self,
        machines: List[Machine],
        applications: List[Application],
        approval_provider: Optional[ApprovalProvider] = None,
        encrypt_key: Optional[str] = None,
        authorization_config: Optional[Dict] = None
    ):
        """初始化运维管理器

        Args:
            machines: 机器配置列表
            applications: 应用配置列表
            approval_provider: 审批提供者（可选）
            encrypt_key: 加密密钥（用于解密密码），从环境变量 OPERATIONS_ENCRYPT_KEY 读取
            authorization_config: 权限配置（operators/approvers/groups）
        """
        self.machines: Dict[str, Machine] = {m.name: m for m in machines}
        self.applications: Dict[str, Application] = {a.name: a for a in applications}
        self.approval_provider = approval_provider
        self.authorization_config = authorization_config or {}

        # 初始化加密器
        if encrypt_key is None:
            encrypt_key = os.environ.get("OPERATIONS_ENCRYPT_KEY")
            if not encrypt_key:
                # 生成新密钥并提示用户保存
                new_key = Fernet.generate_key().decode()
                logger.warning(f"未找到环境变量 OPERATIONS_ENCRYPT_KEY，已生成新密钥: {new_key}")
                logger.warning("请将此密钥保存到环境变量 OPERATIONS_ENCRYPT_KEY 中")
                encrypt_key = new_key

        self.cipher = Fernet(encrypt_key.encode() if isinstance(encrypt_key, str) else encrypt_key)

        # SSH 连接池（复用连接）
        self._ssh_pool: Dict[str, paramiko.SSHClient] = {}
        self._ssh_pool_lock = threading.Lock()

        # 审批队列
        self._pending_operations: Dict[str, PendingOperation] = {}
        self._pending_lock = threading.Lock()

        # 超时检查定时器
        self._expiry_timer: Optional[threading.Timer] = None
        self._start_expiry_checker()

        logger.info(f"运维管理器初始化完成: {len(self.machines)} 台机器, {len(self.applications)} 个应用")

    @classmethod
    def from_config(cls, config: Dict) -> 'OperationsManager':
        """从配置字典创建 OperationsManager 实例

        Args:
            config: 运维配置字典，格式参考 config.operations.example.yaml

        Returns:
            OperationsManager 实例
        """
        # 解析机器配置
        machines = []
        machines_config = config.get('machines', [])
        for machine_config in machines_config:
            ssh_config = SSHConfig(
                host=machine_config['host'],
                port=machine_config.get('ssh', {}).get('port', 22),
                username=machine_config.get('ssh', {}).get('user', 'root'),
                method=SSHMethod.KEY if machine_config.get('ssh', {}).get('method') == 'key' else SSHMethod.PASSWORD,
                password=machine_config.get('ssh', {}).get('password'),
                key_path=machine_config.get('ssh', {}).get('key_path'),
                timeout=machine_config.get('ssh', {}).get('timeout', 30)
            )

            machine = Machine(
                name=machine_config['name'],
                display_name=machine_config.get('alias', [machine_config['name']])[0] if machine_config.get('alias') else machine_config['name'],
                ssh_config=ssh_config,
                description=machine_config.get('description', ''),
                tags=machine_config.get('tags', []),
                metadata=machine_config.get('metadata', {})
            )
            machines.append(machine)

        # 解析应用配置
        applications = []
        for machine_config in machines_config:
            apps_config = machine_config.get('applications', [])
            for app_config in apps_config:
                application = Application(
                    name=app_config['name'],
                    display_name=app_config.get('alias', [app_config['name']])[0] if app_config.get('alias') else app_config['name'],
                    description=app_config.get('description', ''),
                    machines=[machine_config['name']],
                    tags=app_config.get('tags', []),
                    aliases=app_config.get('alias', []),
                    path=app_config.get('path', ''),
                    metadata=app_config.get('metadata', {})
                )
                applications.append(application)

        # 获取权限配置
        authorization_config = config.get('authorization', {})

        # TODO: 初始化审批提供者（根据配置）
        approval_provider = None

        return cls(
            machines=machines,
            applications=applications,
            approval_provider=approval_provider,
            authorization_config=authorization_config
        )

    def test_connections(self) -> Dict[str, Dict[str, Any]]:
        """
        测试所有机器的SSH连接

        Returns:
            Dict[str, Dict[str, Any]]: 测试结果
                {
                    "machine_name": {
                        "success": bool,
                        "message": str,
                        "details": dict  # 成功时包含系统信息
                    }
                }
        """
        results = {}

        for machine_name, machine in self.machines.items():
            try:
                logger.info(f"测试连接: {machine.display_name} ({machine.ssh_config.host})")

                # 执行简单的测试命令
                success, output = self._execute_ssh_command_direct(
                    machine.ssh_config,
                    "uname -a && uptime"
                )

                if success:
                    lines = output.strip().split('\n')
                    results[machine_name] = {
                        "success": True,
                        "message": f"连接成功: {machine.display_name}",
                        "details": {
                            "host": machine.ssh_config.host,
                            "uname": lines[0] if len(lines) > 0 else "",
                            "uptime": lines[1] if len(lines) > 1 else ""
                        }
                    }
                    logger.info(f"✅ {machine.display_name} 连接测试成功")
                else:
                    results[machine_name] = {
                        "success": False,
                        "message": f"连接失败: {output}",
                        "details": {"host": machine.ssh_config.host}
                    }
                    logger.warning(f"❌ {machine.display_name} 连接测试失败: {output}")

            except Exception as e:
                results[machine_name] = {
                    "success": False,
                    "message": f"连接异常: {str(e)}",
                    "details": {"host": machine.ssh_config.host}
                }
                logger.error(f"❌ {machine.display_name} 连接测试异常: {e}")

        # 统计结果
        success_count = sum(1 for r in results.values() if r["success"])
        total_count = len(results)
        logger.info(f"连接测试完成: {success_count}/{total_count} 台机器连接成功")

        return results

    def _execute_ssh_command_direct(
        self,
        ssh_config: SSHConfig,
        command: str
    ) -> Tuple[bool, str]:
        """
        直接执行SSH命令（不经过权限检查，仅用于内部测试）

        Args:
            ssh_config: SSH配置
            command: 待执行的命令

        Returns:
            Tuple[bool, str]: (是否成功, 输出内容或错误信息)
        """
        try:
            ssh_client = self._get_ssh_client_from_config(ssh_config)
            stdin, stdout, stderr = ssh_client.exec_command(command, timeout=10)

            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                return True, output
            else:
                return False, f"命令执行失败 (exit code {exit_code}):\n{error}"

        except Exception as e:
            return False, f"SSH 连接失败: {str(e)}"

    def _get_ssh_client_from_config(self, ssh_config: SSHConfig) -> paramiko.SSHClient:
        """
        从SSH配置创建SSH客户端（用于测试连接）

        Args:
            ssh_config: SSH配置

        Returns:
            paramiko.SSHClient: SSH客户端
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # 根据认证方式连接
        if ssh_config.method == SSHMethod.KEY:
            # 使用密钥认证
            key_path = ssh_config.key_path
            passphrase = None

            if ssh_config.key_passphrase:
                # 解密密钥密码
                try:
                    passphrase = self.cipher.decrypt(ssh_config.key_passphrase.encode()).decode()
                except Exception as e:
                    logger.warning(f"解密密钥密码失败: {e}")

            client.connect(
                hostname=ssh_config.host,
                port=ssh_config.port,
                username=ssh_config.username,
                key_filename=key_path,
                passphrase=passphrase,
                timeout=ssh_config.timeout
            )
        else:
            # 使用密码认证
            password = None
            if ssh_config.password:
                try:
                    password = self.cipher.decrypt(ssh_config.password.encode()).decode()
                except Exception as e:
                    logger.warning(f"解密密码失败: {e}")

            client.connect(
                hostname=ssh_config.host,
                port=ssh_config.port,
                username=ssh_config.username,
                password=password,
                timeout=ssh_config.timeout
            )

        return client

    def is_authorized(
        self,
        operator: OperatorIdentity,
        machine: Machine,
        operation_type: str = "read",
        chat_id: Optional[str] = None
    ) -> bool:
        """检查操作者是否有权限操作指定机器

        Args:
            operator: 操作者身份
            machine: 目标机器
            operation_type: 操作类型 ("read" | "write" | "admin")
            chat_id: 飞书群组ID（可选，用于群组白名单鉴权）

        Returns:
            bool: 是否有权限
        """
        # 获取渠道（如 feishu）
        channel = operator.metadata.get('channel', 'feishu')

        # 从 authorization_config 中获取操作者白名单
        operators_config = self.authorization_config.get('operators', {}).get(channel, [])

        # 1. 检查用户是否在白名单中
        for op_config in operators_config:
            if op_config.get('open_id') == operator.user_id or op_config.get('user_id') == operator.user_id:
                logger.debug(f"用户 {operator.username} 在运维白名单中")
                return True

        # 2. 检查群组白名单（如果提供了 chat_id）
        if chat_id:
            groups_config = self.authorization_config.get('groups', {}).get(channel, [])
            for group_config in groups_config:
                if group_config.get('chat_id') == chat_id:
                    logger.debug(f"群组 {chat_id} 在运维白名单中")
                    return True

        logger.warning(f"用户 {operator.username} (user_id={operator.user_id}, chat_id={chat_id}) 无运维权限")
        return False

        # 超级管理员拥有所有权限
        if "admin" in operator.roles or "superadmin" in operator.roles:
            return True

        # 检查特定权限
        required_permission = f"{machine.name}:{operation_type}"
        if required_permission in operator.permissions:
            return True

        # 检查通配符权限
        wildcard_permission = f"*:{operation_type}"
        if wildcard_permission in operator.permissions:
            return True

        return False

    def find_machine(self, query: str) -> Optional[Machine]:
        """通过自然语言查找机器

        Args:
            query: 查询字符串（机器名、显示名称、标签、描述等）

        Returns:
            Optional[Machine]: 找到的机器，未找到返回 None
        """
        query_lower = query.lower()

        # 精确匹配名称
        if query in self.machines:
            return self.machines[query]

        # 精确匹配主机 IP/地址（AI 有时会用 IP 作为标识）
        for machine in self.machines.values():
            if machine.ssh_config and machine.ssh_config.host == query:
                return machine

        # 模糊匹配显示名称、描述、标签
        for machine in self.machines.values():
            if query_lower in machine.display_name.lower():
                return machine
            if query_lower in machine.description.lower():
                return machine
            if any(query_lower in tag.lower() for tag in machine.tags):
                return machine

        return None

    def find_application(self, query: str) -> Optional[Application]:
        """通过自然语言查找应用

        Args:
            query: 查询字符串（应用名、显示名称、标签、描述等）

        Returns:
            Optional[Application]: 找到的应用，未找到返回 None
        """
        query_lower = query.lower()

        # 精确匹配名称
        if query in self.applications:
            return self.applications[query]

        # 模糊匹配显示名称、描述、标签
        for app in self.applications.values():
            if query_lower in app.display_name.lower():
                return app
            if query_lower in app.description.lower():
                return app
            if any(query_lower in tag.lower() for tag in app.tags):
                return app

        return None

    def get_operation_risk(self, command: str, machine: Machine) -> OperationRisk:
        """评估命令的风险等级

        Args:
            command: 待执行的命令
            machine: 目标机器

        Returns:
            OperationRisk: 风险等级
        """
        command_lower = command.lower().strip()

        # 高风险命令模式
        high_risk_patterns = [
            r'\brm\s+-rf\b',           # rm -rf
            r'\bdel\s+/s\s+/q\b',      # Windows 递归删除
            r'\bformat\b',             # 格式化
            r'\bmkfs\b',               # 创建文件系统
            r'\bdd\s+if=',             # dd 写入
            r'\bshutdown\b',           # 关机
            r'\breboot\b',             # 重启
            r'\bkill\s+-9\s+1\b',      # 杀死 init 进程
            r'\b>>\s*/dev/sda\b',      # 直接写入磁盘
            r'\bdrop\s+database\b',    # 删除数据库
            r'\bdrop\s+table\b',       # 删除表
            r'\btruncate\s+table\b',   # 清空表
        ]

        for pattern in high_risk_patterns:
            if re.search(pattern, command_lower):
                return OperationRisk.HIGH

        # 中风险命令模式
        medium_risk_patterns = [
            r'\brm\b',                 # rm（非 -rf）
            r'\bmv\b',                 # 移动文件
            r'\bcp\b.*\b-f\b',        # 强制复制
            r'\bsystemctl\s+stop\b',   # 停止服务
            r'\bsystemctl\s+restart\b', # 重启服务
            r'\bkill\b',               # 杀进程
            r'\bpkill\b',              # 批量杀进程
            r'\bdelete\s+from\b',      # SQL 删除
            r'\bupdate\b.*\bset\b',    # SQL 更新
            r'\binsert\s+into\b',      # SQL 插入
            r'\bchmod\b',              # 修改权限
            r'\bchown\b',              # 修改所有者
        ]

        for pattern in medium_risk_patterns:
            if re.search(pattern, command_lower):
                return OperationRisk.MEDIUM

        # 默认为低风险（只读操作）
        return OperationRisk.LOW

    def execute_ssh_command(
        self,
        machine: Machine,
        command: str,
        operator: OperatorIdentity,
        skip_approval: bool = False,
        chat_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """执行 SSH 命令（带权限检查和审批流程）

        Args:
            machine: 目标机器
            command: 待执行的命令
            operator: 操作者身份
            skip_approval: 是否跳过审批（仅用于测试或紧急情况）
            chat_id: 飞书群组ID（可选，用于群组白名单鉴权）

        Returns:
            Tuple[bool, str]: (是否成功, 输出内容或错误信息)
        """
        # 1. 权限检查
        risk_level = self.get_operation_risk(command, machine)
        operation_type = "read" if risk_level == OperationRisk.LOW else "write"

        if not self.is_authorized(operator, machine, operation_type, chat_id=chat_id):
            return False, f"权限不足: 用户 {operator.username} 无权在 {machine.display_name} 上执行 {operation_type} 操作"

        # 2. 审批流程（中高风险且未跳过审批）
        if not skip_approval and risk_level in (OperationRisk.MEDIUM, OperationRisk.HIGH):
            if not self.approval_provider:
                return False, "需要审批但未配置审批提供者"

            # 创建待审批操作
            operation_id = str(uuid.uuid4())
            pending_op = PendingOperation(
                operation_id=operation_id,
                operator=operator,
                machine=machine,
                command=command,
                risk_level=risk_level,
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(minutes=30),  # 30分钟过期
                status="pending"
            )

            with self._pending_lock:
                self._pending_operations[operation_id] = pending_op

            # 发送审批请求
            try:
                approval_request = ApprovalRequest(
                    operation=pending_op,
                    operator=operator,
                    request_message=f"用户 {operator.username} 请求在 {machine.display_name} 上执行命令:\n{command}\n风险等级: {risk_level.value}"
                )
                request_id = self.approval_provider.send_approval_request(approval_request)
                logger.info(f"已发送审批请求: {request_id}")
                return False, f"操作需要审批，请等待审批完成。操作ID: {operation_id}"
            except Exception as e:
                logger.error(f"发送审批请求失败: {e}")
                return False, f"发送审批请求失败: {e}"

        # 3. 执行命令
        try:
            ssh_client = self._get_ssh_client(machine)
            stdin, stdout, stderr = ssh_client.exec_command(command, timeout=machine.ssh_config.timeout)

            # 读取输出
            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                logger.info(f"命令执行成功: {machine.name} - {command[:50]}...")
                return True, output
            else:
                logger.warning(f"命令执行失败 (exit={exit_code}): {machine.name} - {command[:50]}...")
                return False, f"命令执行失败 (exit code {exit_code}):\n{error}"

        except Exception as e:
            logger.error(f"SSH 命令执行异常: {machine.name} - {e}")
            return False, f"SSH 命令执行异常: {e}"

    def request_approval(
        self,
        operator: OperatorIdentity,
        machine: Machine,
        command: str,
        reason: str = ""
    ) -> str:
        """手动请求审批

        Args:
            operator: 操作者
            machine: 目标机器
            command: 待执行命令
            reason: 操作原因

        Returns:
            str: 操作ID
        """
        operation_id = str(uuid.uuid4())
        risk_level = self.get_operation_risk(command, machine)

        pending_op = PendingOperation(
            operation_id=operation_id,
            operator=operator,
            machine=machine,
            command=command,
            risk_level=risk_level,
            reason=reason,
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(minutes=30),
            status="pending"
        )

        with self._pending_lock:
            self._pending_operations[operation_id] = pending_op

        logger.info(f"创建待审批操作: {operation_id}")
        return operation_id

    def approve_operation(self, operation_id: str, approver: str) -> bool:
        """审批通过操作

        Args:
            operation_id: 操作ID
            approver: 审批者标识

        Returns:
            bool: 是否成功
        """
        with self._pending_lock:
            if operation_id not in self._pending_operations:
                logger.warning(f"操作不存在: {operation_id}")
                return False

            operation = self._pending_operations[operation_id]
            if operation.status != "pending":
                logger.warning(f"操作状态不是 pending: {operation_id} ({operation.status})")
                return False

            operation.status = "approved"
            operation.approver = approver
            operation.approved_at = datetime.now()

        logger.info(f"操作已审批通过: {operation_id} by {approver}")
        return True

    def reject_operation(self, operation_id: str, approver: str, reason: str = "") -> bool:
        """拒绝操作

        Args:
            operation_id: 操作ID
            approver: 审批者标识
            reason: 拒绝原因

        Returns:
            bool: 是否成功
        """
        with self._pending_lock:
            if operation_id not in self._pending_operations:
                logger.warning(f"操作不存在: {operation_id}")
                return False

            operation = self._pending_operations[operation_id]
            if operation.status != "pending":
                logger.warning(f"操作状态不是 pending: {operation_id} ({operation.status})")
                return False

            operation.status = "rejected"
            operation.approver = approver
            operation.approved_at = datetime.now()
            operation.rejection_reason = reason

        logger.info(f"操作已拒绝: {operation_id} by {approver}, 原因: {reason}")
        return True

    def get_operation_status(self, operation_id: str) -> Optional[PendingOperation]:
        """获取操作状态

        Args:
            operation_id: 操作ID

        Returns:
            Optional[PendingOperation]: 操作详情，不存在返回 None
        """
        with self._pending_lock:
            return self._pending_operations.get(operation_id)

    def _get_ssh_client(self, machine: Machine) -> paramiko.SSHClient:
        """获取或创建 SSH 连接

        Args:
            machine: 目标机器

        Returns:
            paramiko.SSHClient: SSH 客户端实例
        """
        with self._ssh_pool_lock:
            # 检查连接池
            if machine.name in self._ssh_pool:
                client = self._ssh_pool[machine.name]
                # 检查连接是否仍然活跃
                try:
                    transport = client.get_transport()
                    if transport and transport.is_active():
                        return client
                except:
                    pass
                # 连接已失效，移除
                del self._ssh_pool[machine.name]

            # 创建新连接
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            ssh_config = machine.ssh_config

            try:
                if ssh_config.method == SSHMethod.PASSWORD:
                    # 密码认证
                    password = self._decrypt_password(ssh_config.password) if ssh_config.password else None
                    client.connect(
                        hostname=ssh_config.host,
                        port=ssh_config.port,
                        username=ssh_config.username,
                        password=password,
                        timeout=ssh_config.timeout
                    )
                elif ssh_config.method == SSHMethod.KEY:
                    # 密钥认证
                    key_passphrase = None
                    if ssh_config.key_passphrase:
                        key_passphrase = self._decrypt_password(ssh_config.key_passphrase)

                    client.connect(
                        hostname=ssh_config.host,
                        port=ssh_config.port,
                        username=ssh_config.username,
                        key_filename=ssh_config.key_path,
                        passphrase=key_passphrase,
                        timeout=ssh_config.timeout
                    )
                else:
                    raise ValueError(f"不支持的 SSH 认证方式: {ssh_config.method}")

                self._ssh_pool[machine.name] = client
                logger.info(f"SSH 连接已建立: {machine.name} ({ssh_config.host}:{ssh_config.port})")
                return client

            except Exception as e:
                logger.error(f"SSH 连接失败: {machine.name} - {e}")
                raise

    def _decrypt_password(self, encrypted_password: str) -> str:
        """解密密码

        Args:
            encrypted_password: 加密后的密码

        Returns:
            str: 解密后的密码
        """
        try:
            return self.cipher.decrypt(encrypted_password.encode()).decode()
        except Exception as e:
            logger.error(f"密码解密失败: {e}")
            raise

    def _start_expiry_checker(self):
        """启动过期检查定时器"""
        self._check_expired_operations()
        # 每分钟检查一次
        self._expiry_timer = threading.Timer(60.0, self._start_expiry_checker)
        self._expiry_timer.daemon = True
        self._expiry_timer.start()

    def _check_expired_operations(self):
        """检查并标记过期的操作"""
        now = datetime.now()
        expired_count = 0

        with self._pending_lock:
            for operation in self._pending_operations.values():
                if operation.status == "pending" and operation.expires_at and operation.expires_at < now:
                    self._expire_operation(operation)
                    expired_count += 1

        if expired_count > 0:
            logger.info(f"已标记 {expired_count} 个过期操作")

    def _expire_operation(self, operation: PendingOperation):
        """标记操作为过期

        Args:
            operation: 待标记的操作
        """
        operation.status = "expired"
        logger.info(f"操作已过期: {operation.operation_id}")

    def close(self):
        """关闭管理器，释放资源"""
        # 停止定时器
        if self._expiry_timer:
            self._expiry_timer.cancel()

        # 关闭所有 SSH 连接
        with self._ssh_pool_lock:
            for machine_name, client in self._ssh_pool.items():
                try:
                    client.close()
                    logger.debug(f"SSH 连接已关闭: {machine_name}")
                except:
                    pass
            self._ssh_pool.clear()

        logger.info("运维管理器已关闭")

    def __del__(self):
        """析构函数"""
        self.close()
