import requests
from typing import List
from loguru import logger
from ai_assistant.core.ai_provider import AIProvider
from ai_assistant.core.models import Message


class CherryStudioProvider(AIProvider):
    """CherryStudio AI Provider 实现"""

    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4", timeout: int = 30):
        """
        初始化 CherryStudio Provider

        Args:
            base_url: API 基础 URL
            api_key: API 密钥（可选）
            model: 模型名称
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def send_message(self, messages: List[Message]) -> str:
        """发送消息到 CherryStudio API"""
        try:
            # 转换消息格式为 OpenAI 兼容格式
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

            # 构建请求
            headers = {
                "Content-Type": "application/json"
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

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

            logger.info(f"AI reply received: {len(reply)} characters")
            return reply

        except requests.exceptions.Timeout:
            logger.error("AI API request timeout")
            raise Exception("AI 服务响应超时")
        except requests.exceptions.RequestException as e:
            logger.error(f"AI API request failed: {e}")
            raise Exception(f"AI 服务调用失败: {str(e)}")
        except (KeyError, IndexError) as e:
            logger.error(f"Failed to parse AI response: {e}")
            raise Exception("AI 响应格式错误")

    def check_health(self) -> bool:
        """检查 CherryStudio 服务健康状态"""
        try:
            response = requests.get(
                f"{self.base_url}/v1/models",
                timeout=5
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
            return False
