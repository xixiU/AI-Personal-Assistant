"""
飞书消息构建器

支持纯文本消息和消息卡片（Interactive Card），统一飞书消息的构建和发送。
"""

import json
import requests
from datetime import datetime
from typing import Optional
from loguru import logger


class FeishuMessageBuilder:
    """飞书消息构建器，支持纯文本和消息卡片两种模式"""

    # 卡片头部颜色模板
    TEMPLATE_BLUE = "blue"
    TEMPLATE_GREEN = "green"
    TEMPLATE_RED = "red"
    TEMPLATE_ORANGE = "orange"
    TEMPLATE_PURPLE = "purple"

    def __init__(self, title: str = "🤖 AI 助手回复", template: str = "blue"):
        """
        Args:
            title: 卡片标题
            template: 卡片头部颜色模板
        """
        self._title = title
        self._template = template
        self._elements = []

    def add_markdown(self, content: str) -> "FeishuMessageBuilder":
        """添加 Markdown 内容块"""
        self._elements.append({"tag": "markdown", "content": content})
        return self

    def add_text(self, content: str) -> "FeishuMessageBuilder":
        """添加纯文本内容块"""
        self._elements.append({
            "tag": "div",
            "text": {"tag": "plain_text", "content": content}
        })
        return self

    def add_hr(self) -> "FeishuMessageBuilder":
        """添加分隔线"""
        self._elements.append({"tag": "hr"})
        return self

    def add_note(self, text: str) -> "FeishuMessageBuilder":
        """添加底部备注"""
        self._elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": text}]
        })
        return self

    def build_card(self) -> dict:
        """构建消息卡片 payload"""
        card = {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "template": self._template,
                "title": {"content": self._title, "tag": "plain_text"},
            },
            "elements": self._elements,
        }
        return {"msg_type": "interactive", "content": json.dumps(card)}

    @staticmethod
    def build_text(text: str) -> dict:
        """构建纯文本消息 payload"""
        return {"msg_type": "text", "content": json.dumps({"text": text})}

    @classmethod
    def ai_reply_card(cls, reply_text: str, title: str = "🤖 AI 助手回复", template: str = "blue") -> dict:
        """
        快捷方法：构建标准 AI 回复卡片

        Args:
            reply_text: AI 回复内容（支持 Markdown）
            title: 卡片标题
            template: 卡片头部颜色

        Returns:
            可直接用于飞书 API 的 payload dict
        """
        builder = cls(title=title, template=template)
        builder.add_markdown(reply_text)
        builder.add_hr()
        builder.add_note(f"⏱️ 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return builder.build_card()

    @staticmethod
    def send(
        base_url: str,
        token: str,
        message_id: str,
        payload: dict,
        timeout: int = 10,
    ) -> bool:
        """
        发送消息（reply 接口）

        Args:
            base_url: 飞书 API 地址
            token: tenant_access_token
            message_id: 要回复的消息 ID
            payload: 消息 payload（由 build_card / build_text / ai_reply_card 生成）
            timeout: 请求超时

        Returns:
            是否发送成功
        """
        url = f"{base_url}/open-apis/im/v1/messages/{message_id}/reply"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            result = response.json()

            if result.get("code") == 0:
                logger.info(f"Reply sent successfully to message {message_id}")
                return True
            else:
                logger.error(f"Failed to send reply: code={result.get('code')}, msg={result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"Error sending feishu reply: {e}")
            return False
