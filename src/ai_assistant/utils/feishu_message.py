"""
飞书消息构建器

支持纯文本消息和消息卡片（Interactive Card），统一飞书消息的构建和发送。
"""

import json
import re
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

    @staticmethod
    def _convert_markdown_for_feishu(text: str) -> str:
        """
        转换 Markdown 为飞书消息卡片支持的格式

        飞书消息卡片的 Markdown 限制：
        - 支持：加粗(**), 斜体(*), 删除线(~~), 链接([]()), @提及
        - 不支持：标题(##), 表格, 代码块(```), 引用(>), 分隔线(---)

        转换策略：
        1. 标题（## / ###） → 加粗 + 换行
        2. 代码块（```） → 保留内容，去除反引号-飞书可以支持
        3. 表格 → 转为列表格式
        4. 引用块（>） → 添加缩进
        """
        lines = text.split("\n")
        converted = []
        in_code_block = False
        in_table = False
        table_buffer = []

        for line in lines:
            # 代码块直接保留（飞书卡片支持 ``` 代码块渲染）
            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                converted.append(line)
                continue

            if in_code_block:
                converted.append(line)
                continue

            # 检测表格（Markdown 表格以 | 开头）
            if "|" in line and (line.strip().startswith("|") or "|" in line[:10]):
                if not in_table:
                    in_table = True
                    table_buffer = []
                table_buffer.append(line)
                continue
            else:
                # 表格结束，转换为列表
                if in_table:
                    converted.extend(FeishuMessageBuilder._convert_table_to_list(table_buffer))
                    table_buffer = []
                    in_table = False

            # 处理标题（## 或 ### 等）
            if line.strip().startswith("#"):
                level = len(line) - len(line.lstrip("#"))
                content = line.lstrip("#").strip()
                converted.append(f"\n**{content}**")
                continue

            # 处理引用块（> 开头）
            if line.strip().startswith(">"):
                content = line.lstrip(">").strip()
                converted.append(f"  {content}")
                continue

            # 普通行直接保留
            converted.append(line)

        # 处理末尾的表格
        if in_table and table_buffer:
            converted.extend(FeishuMessageBuilder._convert_table_to_list(table_buffer))

        return "\n".join(converted)

    @staticmethod
    def _convert_table_to_list(table_lines: list) -> list:
        """
        将 Markdown 表格转换为列表格式

        输入示例：
        | 字段名 | 类型 | 长度 | 说明 |
        |-------|------|------|------|
        | flag | string | 4 | 标识 |

        输出示例：
        • 字段名: flag  |  类型: string  |  长度: 4  |  说明: 标识
        """
        if not table_lines:
            return []

        result = []
        header = None

        for line in table_lines:
            line = line.strip()
            if not line or not "|" in line:
                continue

            # 分隔符行（|-----|）跳过
            if re.match(r"^\|[\s\-|]+\|$", line):
                continue

            # 分割单元格
            cells = [cell.strip() for cell in line.split("|") if cell.strip()]

            if header is None:
                # 第一行作为表头
                header = cells
            else:
                # 数据行转为列表项
                if len(cells) == len(header):
                    parts = [f"{header[i]}: {cells[i]}" for i in range(len(cells))]
                    result.append(f"• {' | '.join(parts)}")

        return result

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
        # 转换 Markdown 为飞书支持的格式
        converted_text = cls._convert_markdown_for_feishu(reply_text)

        builder = cls(title=title, template=template)
        builder.add_markdown(converted_text)
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
