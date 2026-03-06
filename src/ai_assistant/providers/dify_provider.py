"""
Dify API Provider

支持 Dify 平台的对话型应用和完成型应用
"""

import requests
from typing import List
from loguru import logger
from ai_assistant.core.ai_provider import AIProvider
from ai_assistant.core.models import Message


class DifyProvider(AIProvider):
    """Dify API Provider"""

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
        self.conversation_id = None  # 用于维持对话上下文

        logger.info(f"Dify Provider 初始化: {base_url}, 应用类型: {app_type}")

    def send_message(self, messages: List[Message]) -> str:
        """发送消息到 Dify API"""
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

                # 如果有对话 ID，添加到请求中以维持上下文
                if self.conversation_id:
                    payload["conversation_id"] = self.conversation_id

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

            # 发送请求
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout
            )
            response.raise_for_status()

            # 解析响应
            result = response.json()

            # 保存对话 ID（用于对话型应用）
            if self.app_type == "chat" and "conversation_id" in result:
                self.conversation_id = result["conversation_id"]

            # 提取回复内容
            reply = result.get("answer", "")

            logger.info(f"Dify 回复已接收: {len(reply)} 字符")
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
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            # 尝试发送一个简单的测试请求
            endpoint = f"{self.base_url}/chat-messages" if self.app_type == "chat" else f"{self.base_url}/completion-messages"
            payload = {
                "inputs": {},
                "query": "test" if self.app_type == "chat" else None,
                "response_mode": "blocking",
                "user": self.user
            }

            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=5
            )
            return response.status_code in [200, 201]
        except Exception as e:
            logger.warning(f"Dify 健康检查失败: {e}")
            return False
