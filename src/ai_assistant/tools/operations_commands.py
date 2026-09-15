"""
运维指令实现模块

提供可扩展的运维指令体系，支持：
- 指令抽象基类和内置指令实现
- SSH 命令执行（支持密码和密钥认证）
- 指令注册中心
- 风险等级标记
"""

import json
import re
import shlex
import paramiko
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Any, Optional, List, Type
from enum import Enum

from loguru import logger

from ai_assistant.core.operations_models import (
    SSHConfig,
    Machine,
    OperationRisk,
    SSHMethod
)


class CommandResult:
    """指令执行结果"""

    def __init__(
        self,
        success: bool,
        data: Any = None,
        error: Optional[str] = None,
        raw_output: Optional[str] = None
    ):
        self.success = success
        self.data = data
        self.error = error
        self.raw_output = raw_output

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "raw_output": self.raw_output
        }


class OperationCommand(ABC):
    """运维指令抽象基类"""

    def __init__(self, cipher=None):
        """
        初始化运维指令

        Args:
            cipher: 密码解密器（用于解密 SSH 配置中的密码）
        """
        self.cipher = cipher

    @property
    @abstractmethod
    def name(self) -> str:
        """指令名称"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """指令描述"""
        pass

    @property
    @abstractmethod
    def risk_level(self) -> OperationRisk:
        """风险等级"""
        pass

    @abstractmethod
    def execute(
        self,
        machine: Machine,
        **kwargs
    ) -> CommandResult:
        """
        执行指令

        Args:
            machine: 目标机器
            **kwargs: 指令特定参数

        Returns:
            CommandResult: 执行结果
        """
        pass

    def _execute_ssh_command(
        self,
        ssh_config: SSHConfig,
        command: str,
        timeout: Optional[int] = None
    ) -> CommandResult:
        """
        执行 SSH 命令（安全封装）

        Args:
            ssh_config: SSH 配置
            command: 要执行的命令
            timeout: 超时时间（秒）

        Returns:
            CommandResult: 执行结果
        """
        client = None
        try:
            # 使用 paramiko 建立 SSH 连接（支持密码和密钥认证）
            client = self._create_ssh_client(ssh_config)

            exec_timeout = timeout or ssh_config.timeout
            stdin, stdout, stderr = client.exec_command(command, timeout=exec_timeout)

            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                return CommandResult(
                    success=True,
                    data=output.strip(),
                    raw_output=output
                )
            else:
                return CommandResult(
                    success=False,
                    error=f"Command failed with exit code {exit_code}: {error}",
                    raw_output=error
                )

        except Exception as e:
            logger.error(f"SSH command execution failed: {e}")
            return CommandResult(
                success=False,
                error=f"Execution error: {str(e)}"
            )
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def _create_ssh_client(self, ssh_config: SSHConfig) -> paramiko.SSHClient:
        """
        根据 SSH 配置创建 paramiko 客户端（支持密码和密钥认证）

        Args:
            ssh_config: SSH 配置

        Returns:
            paramiko.SSHClient: 已连接的 SSH 客户端

        Raises:
            Exception: 连接失败时抛出
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        if ssh_config.method == SSHMethod.KEY:
            # 密钥认证
            passphrase = None
            if ssh_config.key_passphrase and self.cipher:
                try:
                    passphrase = self.cipher.decrypt(ssh_config.key_passphrase.encode()).decode()
                except Exception as e:
                    logger.warning(f"解密密钥密码失败: {e}")

            client.connect(
                hostname=ssh_config.host,
                port=ssh_config.port,
                username=ssh_config.username,
                key_filename=ssh_config.key_path,
                passphrase=passphrase,
                timeout=ssh_config.timeout
            )
        else:
            # 密码认证：解密配置中的密码
            password = None
            if ssh_config.password:
                if self.cipher:
                    try:
                        password = self.cipher.decrypt(ssh_config.password.encode()).decode()
                    except Exception as e:
                        logger.warning(f"解密密码失败: {e}")
                        password = ssh_config.password
                else:
                    # 无 cipher 时按明文处理（向后兼容）
                    password = ssh_config.password

            client.connect(
                hostname=ssh_config.host,
                port=ssh_config.port,
                username=ssh_config.username,
                password=password,
                timeout=ssh_config.timeout
            )

        return client

    def _build_ssh_command(
        self,
        ssh_config: SSHConfig,
        remote_command: str
    ) -> List[str]:
        """
        构建安全的 SSH 命令

        Args:
            ssh_config: SSH 配置
            remote_command: 远程执行的命令

        Returns:
            List[str]: SSH 命令参数列表
        """
        ssh_args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"ConnectTimeout={ssh_config.timeout}",
            "-p", str(ssh_config.port),
        ]

        # 认证方式
        if ssh_config.method == SSHMethod.KEY and ssh_config.key_path:
            ssh_args.extend(["-i", ssh_config.key_path])

        # 目标地址
        ssh_args.append(f"{ssh_config.username}@{ssh_config.host}")

        # 远程命令（作为单独参数，避免注入）
        ssh_args.append(remote_command)

        return ssh_args

    @staticmethod
    def _safe_command(*parts: str) -> str:
        """
        安全拼接命令（避免注入）

        Args:
            *parts: 命令各部分

        Returns:
            str: 拼接后的命令
        """
        return " ".join(shlex.quote(str(p)) for p in parts)


# ==================== 内置指令实现 ====================


class GetAppStatusCommand(OperationCommand):
    """查询应用状态"""

    @property
    def name(self) -> str:
        return "get_app_status"

    @property
    def description(self) -> str:
        return "查询应用运行状态（进程是否存在）"

    @property
    def risk_level(self) -> OperationRisk:
        return OperationRisk.LOW

    def execute(
        self,
        machine: Machine,
        app_name: Optional[str] = None,
        **kwargs
    ) -> CommandResult:
        """
        查询应用状态

        Args:
            machine: 目标机器
            app_name: 应用名称（可选，不提供则查询所有应用）

        Returns:
            CommandResult: 执行结果
        """
        try:
            if app_name:
                # 查询特定应用
                command = f"ps aux | grep -i {shlex.quote(app_name)} | grep -v grep"
            else:
                # 查询所有 Java 应用
                command = "ps aux | grep java | grep -v grep"

            result = self._execute_ssh_command(machine.ssh_config, command)

            if result.success and result.data:
                # 解析进程信息
                processes = []
                for line in result.data.split('\n'):
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 11:
                            processes.append({
                                "user": parts[0],
                                "pid": parts[1],
                                "cpu": parts[2],
                                "mem": parts[3],
                                "command": " ".join(parts[10:])[:100]  # 限制长度
                            })

                return CommandResult(
                    success=True,
                    data={
                        "status": "running" if processes else "stopped",
                        "process_count": len(processes),
                        "processes": processes
                    },
                    raw_output=result.raw_output
                )
            else:
                return CommandResult(
                    success=True,
                    data={
                        "status": "stopped",
                        "process_count": 0,
                        "processes": []
                    }
                )

        except Exception as e:
            logger.error(f"Get app status failed: {e}")
            return CommandResult(
                success=False,
                error=f"Failed to get app status: {str(e)}"
            )


class GetAppVersionCommand(OperationCommand):
    """查询应用版本"""

    @property
    def name(self) -> str:
        return "get_app_version"

    @property
    def description(self) -> str:
        return "查询应用版本和分支信息（支持 jar/log/file/api 多种来源）"

    @property
    def risk_level(self) -> OperationRisk:
        return OperationRisk.LOW

    def execute(
        self,
        machine: Machine,
        source: str = "jar",
        jar_path: Optional[str] = None,
        jar_file_path: Optional[str] = None,
        log_path: Optional[str] = None,
        version_file: Optional[str] = None,
        api_url: Optional[str] = None,
        **kwargs
    ) -> CommandResult:
        """
        查询应用版本

        Args:
            machine: 目标机器
            source: 版本来源（jar/log/file/api）
            jar_path: JAR 包路径（source=jar 时需要）
            jar_file_path: JAR 包内的文件路径（source=jar 时可选，默认 META-INF/MANIFEST.MF）
            log_path: 日志文件路径（source=log 时需要）
            version_file: 版本文件路径（source=file 时需要）
            api_url: API 地址（source=api 时需要）

        Returns:
            CommandResult: 执行结果
        """
        try:
            if source == "jar" and jar_path:
                file_path = jar_file_path or "META-INF/MANIFEST.MF"
                return self._get_version_from_jar(machine, jar_path, file_path)
            elif source == "log" and log_path:
                return self._get_version_from_log(machine, log_path)
            elif source == "file" and version_file:
                return self._get_version_from_file(machine, version_file)
            elif source == "api" and api_url:
                return self._get_version_from_api(machine, api_url)
            else:
                return CommandResult(
                    success=False,
                    error=f"Invalid source '{source}' or missing required parameter"
                )

        except Exception as e:
            logger.error(f"Get app version failed: {e}")
            return CommandResult(
                success=False,
                error=f"Failed to get app version: {str(e)}"
            )

    def _get_version_from_jar(
        self,
        machine: Machine,
        jar_path: str,
        file_path: str = "META-INF/MANIFEST.MF"
    ) -> CommandResult:
        """
        从 JAR 包获取版本信息

        Args:
            machine: 目标机器
            jar_path: JAR 包路径
            file_path: JAR 包内的文件路径（默认 META-INF/MANIFEST.MF）
        """
        # 用 unzip -p 提取 jar 内文件。注意：SSH 登录会执行 .bashrc，可能打印
        # "PROMPT_COMMAND：只读变量" 之类的 stderr 噪音并污染退出码，所以：
        # 1) 显式 grep 出目标行做存在性判断（不依赖 unzip 退出码）
        # 2) 不做任何解析，直接把整段原始内容交给大模型自行读取分支/commit
        marker = "___GITINFO_NOT_FOUND___"
        command = (
            f"unzip -p {shlex.quote(jar_path)} {shlex.quote(file_path)} 2>/dev/null"
            f" || echo {marker}"
        )
        result = self._execute_ssh_command(machine.ssh_config, command)

        content = (result.data or "").strip()
        if result.success and content and marker not in content:
            # 不解析：原始内容(通常是 JSON 或 key=value)整体返回，大模型能直接读懂
            return CommandResult(
                success=True,
                data={"file_path": file_path, "content": content},
                raw_output=result.raw_output
            )
        else:
            return CommandResult(
                success=False,
                error=f"Failed to read {file_path} from JAR: {result.error or result.data}"
            )

    def _get_version_from_log(self, machine: Machine, log_path: str) -> CommandResult:
        """从日志文件获取版本信息"""
        command = f"grep -i 'version\\|branch\\|commit' {shlex.quote(log_path)} | head -20"
        result = self._execute_ssh_command(machine.ssh_config, command)

        if result.success:
            return CommandResult(
                success=True,
                data={"version_lines": result.data.split('\n')},
                raw_output=result.raw_output
            )
        else:
            return result

    def _get_version_from_file(self, machine: Machine, version_file: str) -> CommandResult:
        """从版本文件获取版本信息"""
        command = f"cat {shlex.quote(version_file)}"
        result = self._execute_ssh_command(machine.ssh_config, command)

        if result.success:
            return CommandResult(
                success=True,
                data={"version_content": result.data},
                raw_output=result.raw_output
            )
        else:
            return result

    def _get_version_from_api(self, machine: Machine, api_url: str) -> CommandResult:
        """从 API 获取版本信息"""
        command = f"curl -s {shlex.quote(api_url)}"
        result = self._execute_ssh_command(machine.ssh_config, command)

        if result.success and result.data:
            try:
                version_data = json.loads(result.data)
                return CommandResult(success=True, data=version_data, raw_output=result.raw_output)
            except json.JSONDecodeError:
                return CommandResult(
                    success=True,
                    data={"raw_response": result.data},
                    raw_output=result.raw_output
                )
        else:
            return result

    @staticmethod
    def _parse_manifest(manifest_content: str) -> Dict[str, Any]:
        """解析 MANIFEST.MF 文件"""
        info = {}
        for line in manifest_content.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                info[key.strip()] = value.strip()
        return info


class GetJarInfoCommand(OperationCommand):
    """分析 JAR 包信息"""

    @property
    def name(self) -> str:
        return "get_jar_info"

    @property
    def description(self) -> str:
        return "分析 JAR 包详细信息（manifest/pom/git.properties）"

    @property
    def risk_level(self) -> OperationRisk:
        return OperationRisk.LOW

    def execute(
        self,
        machine: Machine,
        jar_path: str,
        **kwargs
    ) -> CommandResult:
        """
        分析 JAR 包信息

        Args:
            machine: 目标机器
            jar_path: JAR 包路径

        Returns:
            CommandResult: 执行结果，包含 manifest、pom、git 信息
        """
        try:
            info = {}

            # 1. 获取 MANIFEST.MF
            manifest_cmd = f"unzip -p {shlex.quote(jar_path)} META-INF/MANIFEST.MF 2>/dev/null"
            manifest_result = self._execute_ssh_command(machine.ssh_config, manifest_cmd)
            if manifest_result.success and manifest_result.data:
                info["manifest"] = self._parse_key_value(manifest_result.data)

            # 2. 获取 pom.properties
            pom_cmd = f"unzip -p {shlex.quote(jar_path)} '**/pom.properties' 2>/dev/null | head -50"
            pom_result = self._execute_ssh_command(machine.ssh_config, pom_cmd)
            if pom_result.success and pom_result.data:
                info["pom"] = self._parse_key_value(pom_result.data)

            # 3. 获取 git.properties
            git_cmd = f"unzip -p {shlex.quote(jar_path)} '**/git.properties' 2>/dev/null | head -50"
            git_result = self._execute_ssh_command(machine.ssh_config, git_cmd)
            if git_result.success and git_result.data:
                info["git"] = self._parse_key_value(git_result.data)

            # 4. 获取 JAR 文件大小和修改时间
            stat_cmd = f"stat -c '%s %Y' {shlex.quote(jar_path)} 2>/dev/null"
            stat_result = self._execute_ssh_command(machine.ssh_config, stat_cmd)
            if stat_result.success and stat_result.data:
                parts = stat_result.data.split()
                if len(parts) == 2:
                    info["file_size"] = int(parts[0])
                    info["modified_time"] = int(parts[1])

            return CommandResult(
                success=True,
                data=info
            )

        except Exception as e:
            logger.error(f"Get jar info failed: {e}")
            return CommandResult(
                success=False,
                error=f"Failed to get jar info: {str(e)}"
            )

    @staticmethod
    def _parse_key_value(content: str) -> Dict[str, str]:
        """解析 key=value 或 key: value 格式的内容"""
        result = {}
        for line in content.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # 支持 = 和 : 两种分隔符
            if '=' in line:
                key, value = line.split('=', 1)
            elif ':' in line:
                key, value = line.split(':', 1)
            else:
                continue

            result[key.strip()] = value.strip()

        return result


class GetProcessInfoCommand(OperationCommand):
    """查询进程详细信息"""

    @property
    def name(self) -> str:
        return "get_process_info"

    @property
    def description(self) -> str:
        return "查询进程详细信息（端口/线程/内存/连接数）"

    @property
    def risk_level(self) -> OperationRisk:
        return OperationRisk.LOW

    def execute(
        self,
        machine: Machine,
        pid: Optional[int] = None,
        app_name: Optional[str] = None,
        **kwargs
    ) -> CommandResult:
        """
        查询进程详细信息

        Args:
            machine: 目标机器
            pid: 进程 ID（可选）
            app_name: 应用名称（可选，用于查找 PID）

        Returns:
            CommandResult: 执行结果
        """
        try:
            # 如果没有提供 PID，尝试根据应用名称查找
            if pid is None and app_name:
                status_cmd = GetAppStatusCommand(cipher=self.cipher)
                status_result = status_cmd.execute(machine, app_name=app_name)
                if status_result.success and status_result.data.get("processes"):
                    pid = int(status_result.data["processes"][0]["pid"])
                else:
                    return CommandResult(
                        success=False,
                        error=f"Process not found for app '{app_name}'"
                    )

            if pid is None:
                return CommandResult(
                    success=False,
                    error="Either 'pid' or 'app_name' must be provided"
                )

            info = {}

            # 1. 进程基本信息
            ps_cmd = f"ps -p {pid} -o pid,ppid,user,%cpu,%mem,vsz,rss,etime,stat,command"
            ps_result = self._execute_ssh_command(machine.ssh_config, ps_cmd)
            if ps_result.success and ps_result.data:
                lines = ps_result.data.strip().split('\n')
                if len(lines) >= 2:
                    headers = lines[0].split()
                    values = lines[1].split(None, len(headers) - 1)
                    info["process"] = dict(zip(headers, values))

            # 2. 监听端口
            port_cmd = f"ss -tlnp | grep 'pid={pid},' || netstat -tlnp 2>/dev/null | grep {pid}"
            port_result = self._execute_ssh_command(machine.ssh_config, port_cmd)
            if port_result.success and port_result.data:
                info["listening_ports"] = port_result.data.split('\n')

            # 3. 线程数
            thread_cmd = f"ps -o nlwp -p {pid} | tail -1"
            thread_result = self._execute_ssh_command(machine.ssh_config, thread_cmd)
            if thread_result.success and thread_result.data:
                info["thread_count"] = int(thread_result.data.strip())

            # 4. 打开的文件描述符数
            fd_cmd = f"ls -1 /proc/{pid}/fd 2>/dev/null | wc -l"
            fd_result = self._execute_ssh_command(machine.ssh_config, fd_cmd)
            if fd_result.success and fd_result.data:
                info["fd_count"] = int(fd_result.data.strip())

            # 5. 网络连接数
            conn_cmd = f"ss -np | grep 'pid={pid},' | wc -l"
            conn_result = self._execute_ssh_command(machine.ssh_config, conn_cmd)
            if conn_result.success and conn_result.data:
                info["connection_count"] = int(conn_result.data.strip())

            return CommandResult(
                success=True,
                data=info
            )

        except Exception as e:
            logger.error(f"Get process info failed: {e}")
            return CommandResult(
                success=False,
                error=f"Failed to get process info: {str(e)}"
            )


class GetPortInfoCommand(OperationCommand):
    """根据端口号查询监听该端口的进程详细信息"""

    @property
    def name(self) -> str:
        return "get_port_info"

    @property
    def description(self) -> str:
        return "根据端口号查询监听该端口的进程（PID/命令行/部署路径/工作目录）"

    @property
    def risk_level(self) -> OperationRisk:
        return OperationRisk.LOW

    def execute(
        self,
        machine: Machine,
        port: int = None,
        **kwargs
    ) -> CommandResult:
        """
        根据端口号查询监听该端口的进程详细信息

        Args:
            machine: 目标机器
            port: 端口号

        Returns:
            CommandResult: 执行结果
        """
        try:
            # 端口号校验（AI 可能传入字符串）
            try:
                port = int(port)
            except (TypeError, ValueError):
                return CommandResult(
                    success=False,
                    error=f"Invalid port: {port!r}, must be an integer"
                )

            info = {"port": port}

            # 1. 查找监听该端口的进程（ss 优先，netstat 兜底）
            #    grep ':{port} ' 精确匹配「本地地址:端口」后跟空格，避免误匹配 :81810 之类
            listen_cmd = (
                f"ss -tlnp 2>/dev/null | grep -E ':{port}[[:space:]]' "
                f"|| netstat -tlnp 2>/dev/null | grep -E ':{port}[[:space:]]'"
            )
            listen_result = self._execute_ssh_command(machine.ssh_config, listen_cmd)

            if not (listen_result.success and listen_result.data and listen_result.data.strip()):
                return CommandResult(
                    success=True,
                    data={
                        "port": port,
                        "listening": False,
                        "message": f"端口 {port} 当前没有 TCP 进程在监听（服务可能已停止，或使用非 TCP 协议）"
                    }
                )

            raw_listen = listen_result.data.strip()
            info["listening"] = True
            info["raw_listen"] = raw_listen

            # 2. 从监听行中解析 PID
            #    ss 格式:  users:(("java",pid=12345,fd=123))
            #    netstat 格式: 12345/java
            pids = re.findall(r'pid=(\d+)', raw_listen)
            if not pids:
                pids = re.findall(r'(\d+)/\S+', raw_listen)
            # 去重并保持顺序
            seen = set()
            pids = [p for p in pids if not (p in seen or seen.add(p))]

            if not pids:
                info["message"] = (
                    f"端口 {port} 正在被监听，但无法解析出 PID"
                    f"（可能权限不足，请确认使用 root 账号）"
                )
                return CommandResult(success=True, data=info)

            # 3. 逐个 PID 采集进程详情（含完整命令行与工作目录，用于区分同名服务）
            processes = []
            for pid in pids:
                proc = {"pid": int(pid)}

                # 3.1 基本信息
                ps_cmd = (
                    f"ps -p {pid} -o pid,ppid,user,%cpu,%mem,etime,stat,comm --no-headers"
                )
                ps_result = self._execute_ssh_command(machine.ssh_config, ps_cmd)
                if ps_result.success and ps_result.data and ps_result.data.strip():
                    parts = ps_result.data.strip().split(None, 7)
                    keys = ["pid", "ppid", "user", "cpu", "mem", "etime", "stat", "comm"]
                    proc["basic"] = dict(zip(keys, parts))

                # 3.2 完整命令行（/proc/{pid}/cmdline，避免 ps 截断）
                cmdline_cmd = f"tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null"
                cmdline_result = self._execute_ssh_command(machine.ssh_config, cmdline_cmd)
                if cmdline_result.success and cmdline_result.data:
                    proc["cmdline"] = cmdline_result.data.strip()

                # 3.3 工作目录（关键：用于区分不同部署路径的同名服务）
                cwd_cmd = f"readlink /proc/{pid}/cwd 2>/dev/null"
                cwd_result = self._execute_ssh_command(machine.ssh_config, cwd_cmd)
                if cwd_result.success and cwd_result.data:
                    proc["cwd"] = cwd_result.data.strip()

                processes.append(proc)

            info["processes"] = processes
            info["pids"] = [int(p) for p in pids]

            return CommandResult(success=True, data=info)

        except Exception as e:
            logger.error(f"Get port info failed: {e}")
            return CommandResult(
                success=False,
                error=f"Failed to get port info: {str(e)}"
            )


class GetSystemMetricsCommand(OperationCommand):
    """查询系统资源指标"""

    @property
    def name(self) -> str:
        return "get_system_metrics"

    @property
    def description(self) -> str:
        return "查询系统资源使用情况（CPU/内存/磁盘/网络）"

    @property
    def risk_level(self) -> OperationRisk:
        return OperationRisk.LOW

    def execute(
        self,
        machine: Machine,
        **kwargs
    ) -> CommandResult:
        """
        查询系统资源指标

        Args:
            machine: 目标机器

        Returns:
            CommandResult: 执行结果
        """
        try:
            metrics = {}

            # 1. CPU 使用率
            cpu_cmd = "top -bn1 | grep 'Cpu(s)' | awk '{print $2}'"
            cpu_result = self._execute_ssh_command(machine.ssh_config, cpu_cmd)
            if cpu_result.success and cpu_result.data:
                metrics["cpu_usage"] = cpu_result.data.strip()

            # 2. 内存使用情况
            mem_cmd = "free -m | awk 'NR==2{printf \"{\\\"total\\\":%s,\\\"used\\\":%s,\\\"free\\\":%s,\\\"usage_percent\\\":%.2f}\", $2,$3,$4,$3*100/$2}'"
            mem_result = self._execute_ssh_command(machine.ssh_config, mem_cmd)
            if mem_result.success and mem_result.data:
                try:
                    metrics["memory"] = json.loads(mem_result.data)
                except json.JSONDecodeError:
                    metrics["memory"] = {"raw": mem_result.data}

            # 3. 磁盘使用情况
            disk_cmd = "df -h | awk 'NR>1 {print $1,$2,$3,$4,$5,$6}'"
            disk_result = self._execute_ssh_command(machine.ssh_config, disk_cmd)
            if disk_result.success and disk_result.data:
                disks = []
                for line in disk_result.data.split('\n'):
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 6:
                            disks.append({
                                "filesystem": parts[0],
                                "size": parts[1],
                                "used": parts[2],
                                "available": parts[3],
                                "use_percent": parts[4],
                                "mounted_on": parts[5]
                            })
                metrics["disks"] = disks

            # 4. 系统负载
            load_cmd = "uptime | awk -F'load average:' '{print $2}'"
            load_result = self._execute_ssh_command(machine.ssh_config, load_cmd)
            if load_result.success and load_result.data:
                metrics["load_average"] = load_result.data.strip()

            # 5. 网络接口统计
            net_cmd = "cat /proc/net/dev | awk 'NR>2 {print $1,$2,$10}'"
            net_result = self._execute_ssh_command(machine.ssh_config, net_cmd)
            if net_result.success and net_result.data:
                interfaces = []
                for line in net_result.data.split('\n'):
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 3:
                            interfaces.append({
                                "interface": parts[0].rstrip(':'),
                                "rx_bytes": parts[1],
                                "tx_bytes": parts[2]
                            })
                metrics["network_interfaces"] = interfaces

            return CommandResult(
                success=True,
                data=metrics
            )

        except Exception as e:
            logger.error(f"Get system metrics failed: {e}")
            return CommandResult(
                success=False,
                error=f"Failed to get system metrics: {str(e)}"
            )


class GetLogsCommand(OperationCommand):
    """查看日志文件"""

    @property
    def name(self) -> str:
        return "get_logs"

    @property
    def description(self) -> str:
        return "查看应用日志（支持 tail -n 和 grep 过滤）"

    @property
    def risk_level(self) -> OperationRisk:
        return OperationRisk.LOW

    def execute(
        self,
        machine: Machine,
        log_path: str,
        lines: int = 100,
        grep_pattern: Optional[str] = None,
        **kwargs
    ) -> CommandResult:
        """
        查看日志文件

        Args:
            machine: 目标机器
            log_path: 日志文件路径
            lines: 读取行数（默认 100）
            grep_pattern: 过滤模式（可选）

        Returns:
            CommandResult: 执行结果
        """
        try:
            # 构建命令
            if grep_pattern:
                command = f"tail -n {lines} {shlex.quote(log_path)} | grep {shlex.quote(grep_pattern)}"
            else:
                command = f"tail -n {lines} {shlex.quote(log_path)}"

            result = self._execute_ssh_command(machine.ssh_config, command)

            if result.success:
                log_lines = result.data.split('\n') if result.data else []
                return CommandResult(
                    success=True,
                    data={
                        "log_path": log_path,
                        "lines_count": len(log_lines),
                        "lines": log_lines
                    },
                    raw_output=result.raw_output
                )
            else:
                return result

        except Exception as e:
            logger.error(f"Get logs failed: {e}")
            return CommandResult(
                success=False,
                error=f"Failed to get logs: {str(e)}"
            )


# ==================== 指令注册中心 ====================


class CommandRegistry:
    """指令注册中心"""

    def __init__(self):
        self._commands: Dict[str, Type[OperationCommand]] = {}
        self._register_builtin_commands()

    def _register_builtin_commands(self):
        """注册内置指令"""
        builtin_commands = [
            GetAppStatusCommand,
            GetAppVersionCommand,
            GetJarInfoCommand,
            GetProcessInfoCommand,
            GetPortInfoCommand,
            GetSystemMetricsCommand,
            GetLogsCommand,
        ]

        for cmd_class in builtin_commands:
            self.register(cmd_class)

    def register(self, command_class: Type[OperationCommand]):
        """
        注册指令

        Args:
            command_class: 指令类（必须继承 OperationCommand）
        """
        if not issubclass(command_class, OperationCommand):
            raise TypeError(f"{command_class} must be a subclass of OperationCommand")

        # 实例化以获取名称
        instance = command_class()
        self._commands[instance.name] = command_class
        logger.debug(f"Registered command: {instance.name}")

    def get(self, command_name: str) -> Optional[OperationCommand]:
        """
        获取指令实例

        Args:
            command_name: 指令名称

        Returns:
            Optional[OperationCommand]: 指令实例，如果不存在则返回 None
        """
        command_class = self._commands.get(command_name)
        if command_class:
            return command_class()
        return None

    def list_commands(self) -> List[Dict[str, Any]]:
        """
        列出所有已注册的指令

        Returns:
            List[Dict[str, Any]]: 指令列表
        """
        commands = []
        for name, cmd_class in self._commands.items():
            instance = cmd_class()
            commands.append({
                "name": instance.name,
                "description": instance.description,
                "risk_level": instance.risk_level.value
            })
        return commands

    def is_registered(self, command_name: str) -> bool:
        """
        检查指令是否已注册

        Args:
            command_name: 指令名称

        Returns:
            bool: 是否已注册
        """
        return command_name in self._commands


# 全局注册中心实例
_global_registry = CommandRegistry()


def get_command_registry() -> CommandRegistry:
    """获取全局指令注册中心"""
    return _global_registry
