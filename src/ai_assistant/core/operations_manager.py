"""运维操作管理器核心实现（只读模式）"""

import os
import paramiko
import threading
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any
from loguru import logger
from cryptography.fernet import Fernet

from .operations_models import (
    SSHMethod,
    SSHConfig,
    Application,
    Machine,
    OperatorIdentity,
)


class OperationsManager:
    """运维操作管理器（只读模式）

    负责：
    1. 机器和应用管理
    2. SSH 连接池管理
    3. 只读命令执行（查询状态、日志等）
    4. 权限验证（白名单机制）
    """

    def __init__(
        self,
        machines: List[Machine],
        applications: List[Application],
        encrypt_key: Optional[str] = None,
        authorization_config: Optional[Dict] = None
    ):
        """初始化运维管理器（只读查询）

        Args:
            machines: 机器配置列表
            applications: 应用配置列表
            encrypt_key: 加密密钥（用于解密密码），从环境变量 OPERATIONS_ENCRYPT_KEY 读取
            authorization_config: 权限配置（operators/groups 白名单）
        """
        self.machines: Dict[str, Machine] = {m.name: m for m in machines}
        self.applications: Dict[str, Application] = {a.name: a for a in applications}
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

        logger.info(f"运维管理器初始化完成（只读模式）: {len(self.machines)} 台机器, {len(self.applications)} 个应用")

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

        return cls(
            machines=machines,
            applications=applications,
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
        chat_id: Optional[str] = None
    ) -> bool:
        """检查操作者是否有权限访问指定机器（只读权限）

        Args:
            operator: 操作者身份
            machine: 目标机器
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

    def close(self):
        """关闭管理器，释放资源"""
        # 关闭所有 SSH 连接
        with self._ssh_pool_lock:
            for machine_name, client in self._ssh_pool.items():
                try:
                    client.close()
                    logger.debug(f"SSH 连接已关闭: {machine_name}")
                except:
                    pass
            self._ssh_pool.clear()

        logger.info("运维管理器已关闭（只读模式）")

    def __del__(self):
        """析构函数"""
        self.close()
