"""
运维工具封装模块

封装运维指令调用，供 AI 使用，提供：
- 简化的方法接口（每个方法对应一个运维指令）
- 统一的返回格式
- 危险操作的审批流程集成
- 完整的类型注解和文档
"""

from typing import Dict, Any, Optional, List
from loguru import logger

from ai_assistant.core.operations_models import (
    Machine,
)
from ai_assistant.tools.operations_commands import (
    get_command_registry,
    CommandResult,
    GetAppStatusCommand,
    GetAppVersionCommand,
    GetJarInfoCommand,
    GetProcessInfoCommand,
    GetPortInfoCommand,
    GetSystemMetricsCommand,
    GetLogsCommand,
)


class OperationsTools:
    """
    运维工具类

    封装所有运维指令的调用，提供统一的接口给 AI 使用。
    危险操作会自动集成审批流程。
    """

    def __init__(self, operations_manager=None):
        """
        初始化运维工具

        Args:
            operations_manager: 运维管理器实例（用于审批流程和密码解密），可选
        """
        self.registry = get_command_registry()
        self.operations_manager = operations_manager

    @property
    def _cipher(self):
        """获取密码解密器（来自 operations_manager，用于 SSH 密码认证）"""
        if self.operations_manager:
            return getattr(self.operations_manager, "cipher", None)
        return None

    # ==================== 工具方法 ====================

    def get_app_status(
        self,
        machine: Machine,
        app_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        查询应用运行状态

        Args:
            machine: 目标机器
            app_name: 应用名称（可选，不提供则查询所有 Java 应用）

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 状态数据
                    - status: str - "running" | "stopped"
                    - process_count: int - 进程数量
                    - processes: list - 进程列表
                - error: str - 错误信息（如果失败）

        Example:
            >>> tools = OperationsTools()
            >>> result = tools.get_app_status(machine, app_name="my-service")
            >>> if result["success"]:
            >>>     print(f"状态: {result['data']['status']}")
        """
        try:
            command = GetAppStatusCommand(cipher=self._cipher)
            result = command.execute(machine, app_name=app_name)
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_app_status failed: {e}")
            return self._error_result(f"Failed to get app status: {str(e)}")

    def get_app_version(
        self,
        machine: Machine,
        application_metadata: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
        jar_path: Optional[str] = None,
        jar_file_path: Optional[str] = None,
        log_path: Optional[str] = None,
        version_file: Optional[str] = None,
        api_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        查询应用版本信息

        支持两种模式：
        1. 自动模式：提供 application_metadata，自动从 metadata.version_detection 配置中按顺序尝试
        2. 手动模式：直接指定 source 和对应参数

        Args:
            machine: 目标机器
            application_metadata: 应用的 metadata 配置（包含 version_detection 列表）
            source: 版本来源（手动模式），可选值:
                - "jar": 从 JAR 包获取（支持指定 jar_file_path）
                - "log": 从日志文件中查找版本信息
                - "file": 从指定的版本文件读取
                - "api": 从 API 接口获取
            jar_path: JAR 包路径（source="jar" 时需要）
            jar_file_path: JAR 包内的文件路径（source="jar" 时可选，默认 META-INF/MANIFEST.MF）
            log_path: 日志文件路径（source="log" 时需要）
            version_file: 版本文件路径（source="file" 时需要）
            api_url: API 地址（source="api" 时需要）

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 版本数据
                - error: str - 错误信息（如果失败）
                - tried_methods: list - 尝试的方法列表（自动模式）

        Example:
            # 自动模式（推荐）
            >>> result = tools.get_app_version(
            >>>     machine,
            >>>     application_metadata={
            >>>         "version_detection": [
            >>>             {"type": "jar_manifest", "jar_path": "app.jar", "file_path": "BOOT-INF/classes/git.info"},
            >>>             {"type": "jar_manifest", "jar_path": "app.jar"}
            >>>         ]
            >>>     }
            >>> )

            # 手动模式
            >>> result = tools.get_app_version(
            >>>     machine,
            >>>     source="jar",
            >>>     jar_path="/app/service.jar",
            >>>     jar_file_path="BOOT-INF/classes/git.info"
            >>> )
        """
        try:
            command = GetAppVersionCommand(cipher=self._cipher)

            # 自动模式：从 application_metadata 中读取 version_detection 配置
            if application_metadata and 'version_detection' in application_metadata:
                return self._auto_detect_version(machine, application_metadata['version_detection'], command)

            # 手动模式：直接调用指定的 source
            if source:
                result = command.execute(
                    machine,
                    source=source,
                    jar_path=jar_path,
                    jar_file_path=jar_file_path,
                    log_path=log_path,
                    version_file=version_file,
                    api_url=api_url
                )
                return self._format_result(result)

            return self._error_result("Either 'application_metadata' or 'source' must be provided")

        except Exception as e:
            logger.error(f"get_app_version failed: {e}")
            return self._error_result(f"Failed to get app version: {str(e)}")

    def _auto_detect_version(
        self,
        machine: Machine,
        version_detection_list: List[Dict[str, Any]],
        command: GetAppVersionCommand
    ) -> Dict[str, Any]:
        """
        自动检测版本：按配置顺序尝试多种方式

        Args:
            machine: 目标机器
            version_detection_list: version_detection 配置列表
            command: GetAppVersionCommand 实例

        Returns:
            Dict[str, Any]: 执行结果
        """
        tried_methods = []
        errors = []

        for idx, config in enumerate(version_detection_list):
            detection_type = config.get('type', '')
            method_name = f"{detection_type} (method {idx + 1})"
            tried_methods.append(method_name)

            try:
                result = None

                if detection_type == 'jar_manifest':
                    jar_path = config.get('jar_path')
                    file_path = config.get('file_path', 'META-INF/MANIFEST.MF')
                    if jar_path:
                        result = command.execute(
                            machine,
                            source='jar',
                            jar_path=jar_path,
                            jar_file_path=file_path
                        )

                elif detection_type == 'log_file':
                    log_command = config.get('command')
                    if log_command:
                        # 直接执行自定义命令
                        ssh_result = command._execute_ssh_command(machine.ssh_config, log_command)
                        if ssh_result.success:
                            result = CommandResult(
                                success=True,
                                data={"version_from_log": ssh_result.data},
                                raw_output=ssh_result.raw_output
                            )
                        else:
                            result = ssh_result

                elif detection_type == 'version_file':
                    version_file = config.get('file_path')
                    if version_file:
                        result = command.execute(
                            machine,
                            source='file',
                            version_file=version_file
                        )

                elif detection_type == 'api_endpoint':
                    api_url = config.get('url')
                    if api_url:
                        result = command.execute(
                            machine,
                            source='api',
                            api_url=api_url
                        )

                # 如果成功，直接返回
                if result and result.success:
                    logger.info(f"Version detection succeeded with method: {method_name}")
                    return {
                        "success": True,
                        "data": result.data,
                        "method_used": method_name,
                        "tried_methods": tried_methods
                    }
                else:
                    error_msg = result.error if result else "Invalid configuration"
                    errors.append(f"{method_name}: {error_msg}")
                    logger.debug(f"Version detection failed with {method_name}: {error_msg}")

            except Exception as e:
                error_msg = str(e)
                errors.append(f"{method_name}: {error_msg}")
                logger.debug(f"Version detection exception with {method_name}: {error_msg}")

        # 所有方法都失败
        return {
            "success": False,
            "data": None,
            "error": f"All version detection methods failed. Tried: {', '.join(tried_methods)}",
            "tried_methods": tried_methods,
            "errors": errors
        }

    def get_jar_info(
        self,
        machine: Machine,
        jar_path: str
    ) -> Dict[str, Any]:
        """
        分析 JAR 包详细信息

        从 JAR 包中提取：
        - MANIFEST.MF 内容
        - pom.properties（Maven 构建信息）
        - git.properties（Git 版本信息）
        - 文件大小和修改时间

        Args:
            machine: 目标机器
            jar_path: JAR 包路径

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - JAR 包信息
                    - manifest: dict - MANIFEST.MF 内容
                    - pom: dict - pom.properties 内容
                    - git: dict - git.properties 内容
                    - file_size: int - 文件大小（字节）
                    - modified_time: int - 修改时间（Unix 时间戳）
                - error: str - 错误信息（如果失败）

        Example:
            >>> result = tools.get_jar_info(machine, jar_path="/app/service.jar")
            >>> if result["success"]:
            >>>     git_info = result["data"].get("git", {})
            >>>     commit = git_info.get("git.commit.id.abbrev", "unknown")
        """
        try:
            command = GetJarInfoCommand(cipher=self._cipher)
            result = command.execute(machine, jar_path=jar_path)
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_jar_info failed: {e}")
            return self._error_result(f"Failed to get jar info: {str(e)}")

    def get_process_info(
        self,
        machine: Machine,
        pid: Optional[int] = None,
        app_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        查询进程详细信息

        包括：
        - 进程基本信息（CPU、内存、运行时间等）
        - 监听端口列表
        - 线程数
        - 文件描述符数
        - 网络连接数

        Args:
            machine: 目标机器
            pid: 进程 ID（可选）
            app_name: 应用名称（可选，用于查找 PID）
            注意：pid 和 app_name 至少提供一个

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 进程信息
                    - process: dict - 进程基本信息
                    - listening_ports: list - 监听端口
                    - thread_count: int - 线程数
                    - fd_count: int - 文件描述符数
                    - connection_count: int - 网络连接数
                - error: str - 错误信息（如果失败）

        Example:
            >>> result = tools.get_process_info(machine, app_name="my-service")
            >>> if result["success"]:
            >>>     threads = result["data"]["thread_count"]
        """
        try:
            command = GetProcessInfoCommand(cipher=self._cipher)
            result = command.execute(machine, pid=pid, app_name=app_name)
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_process_info failed: {e}")
            return self._error_result(f"Failed to get process info: {str(e)}")

    def get_port_info(
        self,
        machine: Machine,
        port: int
    ) -> Dict[str, Any]:
        """
        根据端口号查询正在监听该端口的进程

        当需要确认「某个端口是否有服务在运行」或「哪个进程占用了某端口」时，
        必须使用本工具，而不是用 get_app_status(app_name=端口号)——因为端口号不会
        出现在进程命令行里，用 grep 端口号是查不到服务的。

        本工具通过 ss/netstat 定位监听 PID，并返回该进程的完整命令行和工作目录
        （cwd），可用于区分部署路径不同的同名服务（如 ts-cs 与 ts-bs）。

        Args:
            machine: 目标机器
            port: 端口号（整数，如 8181）

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 端口信息
                    - port: int - 查询的端口
                    - listening: bool - 是否有进程在监听
                    - pids: list - 监听该端口的 PID 列表
                    - processes: list - 每个进程的详情（basic/cmdline/cwd）
                    - message: str - 当无监听时的说明
                - error: str - 错误信息（如果失败）

        Example:
            >>> result = tools.get_port_info(machine, port=8181)
            >>> if result["success"] and result["data"]["listening"]:
            >>>     print(result["data"]["processes"][0]["cwd"])
        """
        try:
            command = GetPortInfoCommand(cipher=self._cipher)
            result = command.execute(machine, port=port)
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_port_info failed: {e}")
            return self._error_result(f"Failed to get port info: {str(e)}")

    def get_system_metrics(
        self,
        machine: Machine
    ) -> Dict[str, Any]:
        """
        查询系统资源使用情况

        包括：
        - CPU 使用率
        - 内存使用情况
        - 磁盘使用情况
        - 系统负载
        - 网络接口统计

        Args:
            machine: 目标机器

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 系统指标
                    - cpu_usage: str - CPU 使用率
                    - memory: dict - 内存信息
                    - disks: list - 磁盘列表
                    - load_average: str - 系统负载
                    - network_interfaces: list - 网络接口
                - error: str - 错误信息（如果失败）

        Example:
            >>> result = tools.get_system_metrics(machine)
            >>> if result["success"]:
            >>>     mem_usage = result["data"]["memory"]["usage_percent"]
        """
        try:
            command = GetSystemMetricsCommand(cipher=self._cipher)
            result = command.execute(machine)
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_system_metrics failed: {e}")
            return self._error_result(f"Failed to get system metrics: {str(e)}")

    def get_logs(
        self,
        machine: Machine,
        log_path: str,
        lines: int = 100,
        grep_pattern: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        查看应用日志

        支持 tail 方式读取最新日志，可选 grep 过滤。

        Args:
            machine: 目标机器
            log_path: 日志文件路径
            lines: 读取行数（默认 100）
            grep_pattern: 过滤模式（可选，用于过滤日志内容）

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 日志数据
                    - log_path: str - 日志文件路径
                    - lines_count: int - 返回的行数
                    - lines: list - 日志行列表
                - error: str - 错误信息（如果失败）

        Example:
            >>> result = tools.get_logs(
            >>>     machine,
            >>>     log_path="/var/log/app.log",
            >>>     lines=50,
            >>>     grep_pattern="ERROR"
            >>> )
        """
        try:
            command = GetLogsCommand(cipher=self._cipher)
            result = command.execute(
                machine,
                log_path=log_path,
                lines=lines,
                grep_pattern=grep_pattern
            )
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_logs failed: {e}")
            return self._error_result(f"Failed to get logs: {str(e)}")

    # ==================== 辅助方法 ====================

    def list_available_commands(self) -> List[Dict[str, Any]]:
        """
        列出所有可用的运维指令

        Returns:
            List[Dict[str, Any]]: 指令列表
                每项包含：name, description, risk_level

        Example:
            >>> tools = OperationsTools()
            >>> commands = tools.list_available_commands()
            >>> for cmd in commands:
            >>>     print(f"{cmd['name']}: {cmd['description']} (风险: {cmd['risk_level']})")
        """
        return self.registry.list_commands()

    # ==================== 内部辅助方法 ====================

    @staticmethod
    def _format_result(result: CommandResult) -> Dict[str, Any]:
        """格式化指令执行结果"""
        return {
            "success": result.success,
            "data": result.data,
            "error": result.error
        }

    @staticmethod
    def _error_result(error_message: str) -> Dict[str, Any]:
        """创建错误结果"""
        return {
            "success": False,
            "data": None,
            "error": error_message
        }
