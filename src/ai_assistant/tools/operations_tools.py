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
    OperationRisk,
)
from ai_assistant.tools.operations_commands import (
    get_command_registry,
    CommandResult,
    GetAppStatusCommand,
    GetAppVersionCommand,
    GetJarInfoCommand,
    GetProcessInfoCommand,
    GetSystemMetricsCommand,
    GetLogsCommand,
    RestartAppCommand,
    StopAppCommand,
    StartAppCommand,
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
            operations_manager: 运维管理器实例（用于审批流程），可选
        """
        self.registry = get_command_registry()
        self.operations_manager = operations_manager

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
            command = GetAppStatusCommand()
            result = command.execute(machine, app_name=app_name)
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_app_status failed: {e}")
            return self._error_result(f"Failed to get app status: {str(e)}")

    def get_app_version(
        self,
        machine: Machine,
        source: str = "jar",
        jar_path: Optional[str] = None,
        log_path: Optional[str] = None,
        version_file: Optional[str] = None,
        api_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        查询应用版本信息

        Args:
            machine: 目标机器
            source: 版本来源，可选值:
                - "jar": 从 JAR 包的 MANIFEST.MF 获取
                - "log": 从日志文件中查找版本信息
                - "file": 从指定的版本文件读取
                - "api": 从 API 接口获取
            jar_path: JAR 包路径（source="jar" 时需要）
            log_path: 日志文件路径（source="log" 时需要）
            version_file: 版本文件路径（source="file" 时需要）
            api_url: API 地址（source="api" 时需要）

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 版本数据（根据 source 不同而不同）
                - error: str - 错误信息（如果失败）

        Example:
            >>> result = tools.get_app_version(
            >>>     machine,
            >>>     source="jar",
            >>>     jar_path="/app/service.jar"
            >>> )
        """
        try:
            command = GetAppVersionCommand()
            result = command.execute(
                machine,
                source=source,
                jar_path=jar_path,
                log_path=log_path,
                version_file=version_file,
                api_url=api_url
            )
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_app_version failed: {e}")
            return self._error_result(f"Failed to get app version: {str(e)}")

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
            command = GetJarInfoCommand()
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
            command = GetProcessInfoCommand()
            result = command.execute(machine, pid=pid, app_name=app_name)
            return self._format_result(result)
        except Exception as e:
            logger.error(f"get_process_info failed: {e}")
            return self._error_result(f"Failed to get process info: {str(e)}")

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
            command = GetSystemMetricsCommand()
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
            command = GetLogsCommand()
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

    # ==================== 危险操作（需要审批） ====================

    def restart_app(
        self,
        machine: Machine,
        restart_script: str,
        reason: str = "",
        operator_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        重启应用服务（危险操作，需要审批）

        Args:
            machine: 目标机器
            restart_script: 重启脚本路径
            reason: 操作原因（用于审批记录）
            operator_id: 操作者 ID（可选）

        Returns:
            Dict[str, Any]: 执行结果
                - success: bool - 是否成功
                - data: dict - 执行数据
                - error: str - 错误信息（如果失败）
                - need_approval: bool - 是否需要审批（如果返回 True，表示已创建审批请求）
                - operation_id: str - 操作 ID（如果需要审批）

        Example:
            >>> result = tools.restart_app(
            >>>     machine,
            >>>     restart_script="/app/restart.sh",
            >>>     reason="应用无响应需要重启",
            >>>     operator_id="user123"
            >>> )
            >>> if result.get("need_approval"):
            >>>     print(f"需要审批，操作 ID: {result['operation_id']}")
        """
        try:
            command = RestartAppCommand()

            # 检查是否需要审批
            if self.operations_manager and command.risk_level in [OperationRisk.MEDIUM, OperationRisk.HIGH]:
                # 创建审批请求
                approval_result = self._create_approval_request(
                    machine=machine,
                    command_name=command.name,
                    risk_level=command.risk_level,
                    reason=reason,
                    operator_id=operator_id,
                    params={"restart_script": restart_script}
                )

                if approval_result.get("need_approval"):
                    return approval_result

            # 直接执行（无需审批或已审批）
            result = command.execute(machine, restart_script=restart_script)
            return self._format_result(result)

        except Exception as e:
            logger.error(f"restart_app failed: {e}")
            return self._error_result(f"Failed to restart app: {str(e)}")

    def stop_app(
        self,
        machine: Machine,
        stop_script: Optional[str] = None,
        pid: Optional[int] = None,
        reason: str = "",
        operator_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        停止应用服务（危险操作，需要审批）

        Args:
            machine: 目标机器
            stop_script: 停止脚本路径（可选）
            pid: 进程 ID（可选，如果不提供脚本则使用 kill）
            reason: 操作原因（用于审批记录）
            operator_id: 操作者 ID（可选）
            注意：stop_script 和 pid 至少提供一个

        Returns:
            Dict[str, Any]: 执行结果（格式同 restart_app）

        Example:
            >>> result = tools.stop_app(
            >>>     machine,
            >>>     stop_script="/app/stop.sh",
            >>>     reason="进行版本升级",
            >>>     operator_id="user123"
            >>> )
        """
        try:
            command = StopAppCommand()

            # 检查是否需要审批
            if self.operations_manager and command.risk_level in [OperationRisk.MEDIUM, OperationRisk.HIGH]:
                # 创建审批请求
                approval_result = self._create_approval_request(
                    machine=machine,
                    command_name=command.name,
                    risk_level=command.risk_level,
                    reason=reason,
                    operator_id=operator_id,
                    params={"stop_script": stop_script, "pid": pid}
                )

                if approval_result.get("need_approval"):
                    return approval_result

            # 直接执行（无需审批或已审批）
            result = command.execute(machine, stop_script=stop_script, pid=pid)
            return self._format_result(result)

        except Exception as e:
            logger.error(f"stop_app failed: {e}")
            return self._error_result(f"Failed to stop app: {str(e)}")

    def start_app(
        self,
        machine: Machine,
        start_script: str,
        reason: str = "",
        operator_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        启动应用服务（危险操作，需要审批）

        Args:
            machine: 目标机器
            start_script: 启动脚本路径
            reason: 操作原因（用于审批记录）
            operator_id: 操作者 ID（可选）

        Returns:
            Dict[str, Any]: 执行结果（格式同 restart_app）

        Example:
            >>> result = tools.start_app(
            >>>     machine,
            >>>     start_script="/app/start.sh",
            >>>     reason="版本升级完成后启动",
            >>>     operator_id="user123"
            >>> )
        """
        try:
            command = StartAppCommand()

            # 检查是否需要审批
            if self.operations_manager and command.risk_level in [OperationRisk.MEDIUM, OperationRisk.HIGH]:
                # 创建审批请求
                approval_result = self._create_approval_request(
                    machine=machine,
                    command_name=command.name,
                    risk_level=command.risk_level,
                    reason=reason,
                    operator_id=operator_id,
                    params={"start_script": start_script}
                )

                if approval_result.get("need_approval"):
                    return approval_result

            # 直接执行（无需审批或已审批）
            result = command.execute(machine, start_script=start_script)
            return self._format_result(result)

        except Exception as e:
            logger.error(f"start_app failed: {e}")
            return self._error_result(f"Failed to start app: {str(e)}")

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

    def execute_custom_command(
        self,
        command_name: str,
        machine: Machine,
        **kwargs
    ) -> Dict[str, Any]:
        """
        执行自定义注册的指令

        Args:
            command_name: 指令名称
            machine: 目标机器
            **kwargs: 指令参数

        Returns:
            Dict[str, Any]: 执行结果

        Example:
            >>> # 假设用户注册了自定义指令 "check_database"
            >>> result = tools.execute_custom_command(
            >>>     "check_database",
            >>>     machine,
            >>>     db_name="mydb"
            >>> )
        """
        try:
            command = self.registry.get(command_name)
            if not command:
                return self._error_result(f"Command '{command_name}' not found")

            # 检查是否需要审批
            if self.operations_manager and command.risk_level in [OperationRisk.MEDIUM, OperationRisk.HIGH]:
                reason = kwargs.pop("reason", "")
                operator_id = kwargs.pop("operator_id", None)

                approval_result = self._create_approval_request(
                    machine=machine,
                    command_name=command_name,
                    risk_level=command.risk_level,
                    reason=reason,
                    operator_id=operator_id,
                    params=kwargs
                )

                if approval_result.get("need_approval"):
                    return approval_result

            # 执行指令
            result = command.execute(machine, **kwargs)
            return self._format_result(result)

        except Exception as e:
            logger.error(f"execute_custom_command failed: {e}")
            return self._error_result(f"Failed to execute command: {str(e)}")

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

    def _create_approval_request(
        self,
        machine: Machine,
        command_name: str,
        risk_level: OperationRisk,
        reason: str,
        operator_id: Optional[str],
        params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        创建审批请求

        Args:
            machine: 目标机器
            command_name: 指令名称
            risk_level: 风险等级
            reason: 操作原因
            operator_id: 操作者 ID
            params: 指令参数

        Returns:
            Dict[str, Any]: 审批请求结果
        """
        try:
            if not self.operations_manager:
                return {"need_approval": False}

            # 调用 operations_manager 创建审批请求
            # 这里假设 operations_manager 有 create_pending_operation 方法
            operation_id = self.operations_manager.create_pending_operation(
                machine=machine,
                command_name=command_name,
                risk_level=risk_level,
                reason=reason,
                operator_id=operator_id,
                params=params
            )

            return {
                "success": False,
                "need_approval": True,
                "operation_id": operation_id,
                "data": None,
                "error": f"Operation requires approval (risk level: {risk_level.value}). Operation ID: {operation_id}"
            }

        except Exception as e:
            logger.error(f"Failed to create approval request: {e}")
            return self._error_result(f"Failed to create approval request: {str(e)}")
