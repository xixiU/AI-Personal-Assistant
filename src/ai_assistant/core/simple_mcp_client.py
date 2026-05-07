"""
轻量级 MCP 客户端

基于 httpx + SSE 实现，兼容 FastMCP 服务器的 SSE 传输协议。
使用持久连接：建立 SSE 连接 → 初始化握手 → 复用连接调用工具。
"""

import json
import threading
import queue
import time
from typing import Any, Dict, Optional
from loguru import logger
import httpx


class SimpleMCPClient:
    """MCP 客户端，使用持久 SSE 连接"""

    def __init__(self, server_url: str, timeout: float = 30.0):
        """
        Args:
            server_url: MCP 服务器 SSE 端点（如 http://localhost:50070/sse）
            timeout: 请求超时时间（秒）
        """
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout
        self._request_id = 0
        self._lock = threading.Lock()

        # 持久连接状态
        self._endpoint_url = None
        self._initialized = False
        self._response_queues: Dict[int, queue.Queue] = {}
        self._listener_thread = None
        self._connected = threading.Event()
        self._running = False

        logger.info(f"初始化 MCP 客户端: {self.server_url}")

    def _next_request_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _ensure_connected(self):
        """确保已建立连接并完成初始化"""
        if self._initialized and self._running:
            return

        self._connect()

    def _connect(self):
        """建立 SSE 连接并完成初始化握手"""
        self._running = True
        self._connected.clear()

        # 启动 SSE 监听线程
        self._listener_thread = threading.Thread(target=self._sse_listener, daemon=True)
        self._listener_thread.start()

        # 等待收到 endpoint
        if not self._connected.wait(timeout=15.0):
            self._running = False
            raise ConnectionError(f"无法连接到 MCP 服务器: {self.server_url}")

        # 发送初始化请求
        self._do_initialize()

    def _sse_listener(self):
        """SSE 监听线程，持久运行"""
        try:
            with httpx.Client(timeout=httpx.Timeout(None, connect=10.0)) as client:
                with client.stream("GET", self.server_url) as response:
                    event_type = None
                    buffer = ""

                    for line in response.iter_lines():
                        if not self._running:
                            return

                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            buffer = line[5:].strip()
                        elif line == "":
                            if event_type == "endpoint" and buffer:
                                # 收到 endpoint，构建完整 URL
                                base = self.server_url.rsplit("/", 1)[0]
                                self._endpoint_url = f"{base}{buffer}" if buffer.startswith("/") else buffer
                                logger.debug(f"MCP endpoint: {self._endpoint_url}")
                                self._connected.set()

                            elif event_type == "message" and buffer:
                                try:
                                    data = json.loads(buffer)
                                    msg_id = data.get("id")
                                    if msg_id is not None and msg_id in self._response_queues:
                                        self._response_queues[msg_id].put(data)
                                except json.JSONDecodeError:
                                    pass

                            buffer = ""
                            event_type = None

        except Exception as e:
            logger.error(f"SSE 连接断开: {e}")
        finally:
            self._running = False
            self._initialized = False
            self._connected.clear()

    def _do_initialize(self):
        """发送初始化请求并等待响应"""
        req_id = self._next_request_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "simple-mcp-client", "version": "1.0.0"},
            },
        }

        # 注册响应队列
        resp_queue = queue.Queue()
        self._response_queues[req_id] = resp_queue

        try:
            # 发送初始化请求
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(self._endpoint_url, json=payload)
                resp.raise_for_status()

            # 等待初始化响应
            try:
                data = resp_queue.get(timeout=10.0)
                logger.debug(f"MCP 初始化响应: {data.get('result', {}).get('serverInfo', {})}")
            except queue.Empty:
                raise ConnectionError("MCP 初始化超时")

            # 发送 initialized 通知
            notify_payload = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
            with httpx.Client(timeout=10.0) as client:
                client.post(self._endpoint_url, json=notify_payload)

            self._initialized = True
            logger.info("MCP 客户端初始化完成")

        finally:
            self._response_queues.pop(req_id, None)

    def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """
        调用 MCP 工具并返回结果

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具返回的内容
        """
        if arguments is None:
            arguments = {}

        self._ensure_connected()

        request_id = self._next_request_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        logger.debug(f"MCP 调用: {tool_name}({arguments})")

        # 注册响应队列
        resp_queue = queue.Queue()
        self._response_queues[request_id] = resp_queue

        try:
            # 发送请求
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(self._endpoint_url, json=payload)
                resp.raise_for_status()

            # 等待响应
            try:
                data = resp_queue.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError(f"MCP 调用超时: {tool_name}")

            # 解析结果
            if "error" in data:
                error = data["error"]
                raise ValueError(f"MCP 错误 [{error.get('code')}]: {error.get('message')}")

            result = data.get("result", {})
            # 提取文本内容
            content = result.get("content", [])
            if content and isinstance(content, list):
                texts = [item.get("text", "") for item in content if item.get("type") == "text"]
                text = "\n".join(texts)
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text

            return result

        except (ConnectionError, TimeoutError):
            raise
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"MCP 调用失败: {tool_name}, error={e}")
            raise
        finally:
            self._response_queues.pop(request_id, None)

    def close(self):
        """关闭连接"""
        self._running = False
        self._initialized = False

    # ---- 飞书知识库便捷方法 ----

    def search_all_docs(self, keyword: str, count: int = 20) -> Any:
        """全局搜索文档"""
        return self.call_tool("search_all_docs", {"keyword": keyword, "count": count})

    def list_wiki_nodes(self, wiki_token: str, parent_node_token: str = None) -> Any:
        """列出知识库节点"""
        args = {"wiki_token": wiki_token}
        if parent_node_token:
            args["parent_node_token"] = parent_node_token
        return self.call_tool("list_wiki_nodes", args)

    def get_wiki_document_full_content(self, obj_token: str, obj_type: str = "docx") -> Any:
        """获取知识库文档完整内容"""
        return self.call_tool("get_wiki_document_full_content", {"obj_token": obj_token, "obj_type": obj_type})

    def list_children(self, token: str, type_hint: str = "auto", recursive: bool = False) -> Any:
        """列出子内容（统一入口，自动识别知识库/云空间）"""
        return self.call_tool("list_children", {"token": token, "type": type_hint, "recursive": recursive})

    def read_document(self, token: str) -> Any:
        """读取文档内容（统一入口）"""
        return self.call_tool("read_document", {"token": token})
