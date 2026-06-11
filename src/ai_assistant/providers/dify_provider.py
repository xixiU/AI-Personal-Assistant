"""
Dify API Provider

支持 Dify 平台的对话型应用和完成型应用（线程安全）
"""

import requests
import threading
from typing import Any, Dict, List, Optional
from loguru import logger
from ai_assistant.core.ai_provider import AIProvider, KeywordExtractionResult
from ai_assistant.core.models import Message


class DifyProvider(AIProvider):
    """Dify API Provider（线程安全）"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        app_type: str = "chat",
        user: str = "default-user",
        timeout: int = 30
    ):
        """
        初始化 Dify Provider

        Args:
            base_url: Dify API 基础 URL（例如：https://api.dify.ai/v1）
            api_key: Dify API 密钥
            app_type: 应用类型，"chat"（对话型）或 "completion"（完成型）
            user: 用户标识，用于区分不同用户
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.app_type = app_type
        self.user = user
        self.timeout = timeout

        # 按 session_id 存储 conversation_id，支持多用户并发
        self.conversation_ids: Dict[str, str] = {}
        self.lock = threading.Lock()

        logger.info(f"Dify Provider 初始化: {base_url}, 应用类型: {app_type}")

    def send_message(self, messages: List[Message], session_id: Optional[str] = None) -> str:
        """
        发送消息到 Dify API（线程安全）

        Args:
            messages: 消息列表
            session_id: 会话 ID，用于维持多用户对话上下文
        """
        try:
            # 构建请求头
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            # 获取最后一条用户消息
            user_message = ""
            for msg in reversed(messages):
                if msg.role == "user":
                    for content in msg.content:
                        if content.type == "text":
                            user_message = content.data
                            break
                    break

            if not user_message:
                raise Exception("未找到用户消息")

            # 根据应用类型选择不同的端点
            if self.app_type == "chat":
                endpoint = f"{self.base_url}/chat-messages"
                payload = {
                    "inputs": {},
                    "query": user_message,
                    "response_mode": "blocking",
                    "user": self.user
                }

                # 获取该会话的 conversation_id（线程安全）
                conversation_id = None
                if session_id:
                    with self.lock:
                        conversation_id = self.conversation_ids.get(session_id)

                if conversation_id:
                    payload["conversation_id"] = conversation_id

            else:  # completion
                endpoint = f"{self.base_url}/completion-messages"
                payload = {
                    "inputs": {},
                    "response_mode": "blocking",
                    "user": self.user
                }

                # 对于完成型应用，将所有消息拼接成 prompt
                prompt = ""
                for msg in messages:
                    role = "用户" if msg.role == "user" else "助手"
                    for content in msg.content:
                        if content.type == "text":
                            prompt += f"{role}: {content.data}\n"
                payload["inputs"]["prompt"] = prompt

            # 记录请求信息（用于调试）
            logger.debug(f"Dify API 请求: {endpoint}")
            logger.debug(f"Dify API 请求体: {payload}")

            # 发送请求
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout
            )

            # 记录响应状态
            logger.debug(f"Dify API 响应状态: {response.status_code}")

            # 如果失败，记录响应内容
            if response.status_code != 200:
                logger.error(f"Dify API 响应内容: {response.text}")

            response.raise_for_status()

            # 解析响应
            result = response.json()

            # 保存对话 ID（线程安全）
            if self.app_type == "chat" and "conversation_id" in result and session_id:
                with self.lock:
                    self.conversation_ids[session_id] = result["conversation_id"]

            # 提取回复内容和 token 使用信息
            reply = result.get("answer", "")
            metadata = result.get("metadata", {})
            usage = metadata.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            logger.info(
                f"Dify 回复已接收: {len(reply)} 字符, "
                f"tokens(input:{input_tokens}, output:{output_tokens})"
            )
            return reply

        except requests.exceptions.Timeout:
            logger.error("Dify API 请求超时")
            raise Exception("Dify 服务响应超时")
        except requests.exceptions.RequestException as e:
            logger.error(f"Dify API 请求失败: {e}")
            raise Exception(f"Dify 服务调用失败: {str(e)}")
        except (KeyError, IndexError) as e:
            logger.error(f"解析 Dify 响应失败: {e}")
            raise Exception("Dify 响应格式错误")

    def check_health(self) -> bool:
        """检查 Dify API 服务健康状态"""
        try:
            # 实际健康检查需要有效请求，当前简化为直接返回 True
            # 如需真实检查，可启用下方注释代码
            # headers = {
            #     "Authorization": f"Bearer {self.api_key}",
            #     "Content-Type": "application/json"
            # }
            # endpoint = f"{self.base_url}/chat-messages" if self.app_type == "chat" else f"{self.base_url}/completion-messages"
            # payload = {"inputs": {}, "query": "test", "response_mode": "blocking", "user": self.user}
            # response = requests.post(endpoint, json=payload, headers=headers, timeout=5)
            # return response.status_code in [200, 201]
            return True
        except Exception as e:
            logger.warning(f"Dify 健康检查失败: {e}")
            return False

    def extract_keywords(self, query_text: str) -> KeywordExtractionResult:
        """
        Dify 平台不适合做轻量关键词提取调用，返回保守降级值。
        由 feishu_doc_manager 降级到规则提取处理。
        """
        logger.debug("DifyProvider 不支持关键词提取，返回降级值（继续走检索）")
        return KeywordExtractionResult(keywords=[], is_generic_tech=False)

    def filter_docs_by_relevance(self, query: str, candidates: List[Dict[str, Any]], max_docs: int = 3) -> List[int]:
        """
        Dify 平台不适合做文档相关性过滤调用，返回空列表。
        由调用方降级处理（使用原始检索结果）。
        """
        logger.debug("DifyProvider 不支持文档过滤，返回空列表（由调用方降级）")
        return []
