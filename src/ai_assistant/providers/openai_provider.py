"""
OpenAI 兼容 API Provider

支持所有 OpenAI 兼容的 API，包括：
- OpenAI 官方 API
- Azure OpenAI
- CherryStudio
- Ollama
- LM Studio
- 其他兼容 OpenAI API 的服务
"""

import requests
from typing import List
from loguru import logger
from ai_assistant.core.ai_provider import AIProvider
from ai_assistant.core.models import Message


class OpenAIProvider(AIProvider):
    """OpenAI 兼容 API Provider"""

    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4", timeout: int = 30):
        """
        初始化 OpenAI Provider

        Args:
            base_url: API 基础 URL
            api_key: API 密钥（可选，某些本地服务不需要）
            model: 模型名称
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

        logger.info(f"OpenAI Provider 初始化: {base_url}, 模型: {model}")

    def send_message(self, messages: List[Message]) -> str:
        """发送消息到 OpenAI 兼容 API"""
        try:
            # 转换消息格式为 OpenAI 格式
            api_messages = []
            for msg in messages:
                content_text = ""
                for content in msg.content:
                    if content.type == "text":
                        content_text += content.data

                api_messages.append({
                    "role": msg.role,
                    "content": content_text
                })

            # 构建请求头
            headers = {
                "Content-Type": "application/json"
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            # 构建请求体
            payload = {
                "model": self.model,
                "messages": api_messages
            }

            # 发送请求
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout
            )
            response.raise_for_status()

            # 解析响应
            result = response.json()
            reply = result["choices"][0]["message"]["content"]

            logger.info(f"AI 回复已接收: {len(reply)} 字符")
            return reply

        except requests.exceptions.Timeout:
            logger.error("AI API 请求超时")
            raise Exception("AI 服务响应超时")
        except requests.exceptions.RequestException as e:
            logger.error(f"AI API 请求失败: {e}")
            raise Exception(f"AI 服务调用失败: {str(e)}")
        except (KeyError, IndexError) as e:
            logger.error(f"解析 AI 响应失败: {e}")
            raise Exception("AI 响应格式错误")

    def check_health(self) -> bool:
        """检查 API 服务健康状态"""
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.get(
                f"{self.base_url}/v1/models",
                headers=headers,
                timeout=5
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"健康检查失败: {e}")
            return False
