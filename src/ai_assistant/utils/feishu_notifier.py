"""
飞书消息通知工具

用于自动注册后向用户发送私聊消息，通知登录名和初始密码。
"""

import requests
from typing import Optional, Dict
from loguru import logger


class FeishuNotifier:
    """
    飞书消息通知工具

    通过飞书 Bot API 向用户发送私聊消息，用于账号注册成功后的通知。
    """

    def __init__(self, bot_config: Dict):
        """
        初始化飞书通知工具

        Args:
            bot_config: 飞书 Bot 配置，包含 app_id, app_secret, base_url
        """
        self.app_id = bot_config.get("app_id", "")
        self.app_secret = bot_config.get("app_secret", "")
        self.base_url = bot_config.get("base_url", "https://open.feishu.cn")
        self.tenant_access_token = None
        self.token_expire_time = 0

        logger.info("飞书通知工具初始化完成")

    def get_tenant_access_token(self) -> str:
        """
        获取 tenant_access_token

        Returns:
            访问令牌

        Raises:
            Exception: 获取令牌失败
        """
        import time

        # 检查 token 是否过期
        if self.tenant_access_token and time.time() < self.token_expire_time:
            return self.tenant_access_token

        # 获取新 token
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }

        logger.debug(f"请求 tenant_access_token: app_id={self.app_id[:10]}...")

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()

            if result.get("code") == 0:
                self.tenant_access_token = result["tenant_access_token"]
                # token 有效期 2 小时，提前 5 分钟刷新
                self.token_expire_time = time.time() + result.get("expire", 7200) - 300
                logger.info("获取 tenant_access_token 成功")
                return self.tenant_access_token
            else:
                logger.error(f"获取 tenant_access_token 失败: {result}")
                raise Exception(f"获取访问令牌失败: {result.get('msg')}")

        except Exception as e:
            logger.error(f"获取 tenant_access_token 异常: {e}")
            raise

    def send_private_message(
        self,
        open_id: str,
        username: str,
        password: str,
        frontend_url: Optional[str] = None
    ) -> bool:
        """
        发送私聊消息通知用户账号信息

        Args:
            open_id: 飞书用户 open_id
            username: 登录用户名
            password: 初始密码
            frontend_url: 前端地址（可选）

        Returns:
            发送成功返回 True，失败返回 False
        """
        if not open_id:
            logger.error("open_id 为空，无法发送消息")
            return False

        try:
            token = self.get_tenant_access_token()

            # 构建消息内容
            message_lines = [
                "🎉 您的账号已自动注册成功！",
                "",
                f"📧 登录名: {username}",
                f"🔑 初始密码: {password}",
                "",
                "⚠️ 请妥善保管您的密码，建议首次登录后修改。"
            ]

            if frontend_url:
                message_lines.append("")
                message_lines.append(f"🌐 前端地址: {frontend_url}")

            message_text = "\n".join(message_lines)

            # 发送消息
            url = f"{self.base_url}/open-apis/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            params = {"receive_id_type": "open_id"}
            payload = {
                "receive_id": open_id,
                "msg_type": "text",
                "content": f'{{"text": "{message_text}"}}'
            }

            logger.debug(f"发送账号通知消息: open_id={open_id[:8]}..., username={username}")
            response = requests.post(url, headers=headers, json=payload, params=params, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("code") == 0:
                logger.info(f"账号通知消息发送成功: open_id={open_id[:8]}..., username={username}")
                return True
            else:
                logger.error(f"账号通知消息发送失败: {result.get('msg')}")
                return False

        except Exception as e:
            logger.error(f"发送账号通知消息异常: {e}", exc_info=True)
            return False

    def send_card_message(
        self,
        open_id: str,
        username: str,
        password: str,
        frontend_url: Optional[str] = None
    ) -> bool:
        """
        发送卡片消息通知用户账号信息（更美观的展示方式）

        Args:
            open_id: 飞书用户 open_id
            username: 登录用户名
            password: 初始密码
            frontend_url: 前端地址（可选）

        Returns:
            发送成功返回 True，失败返回 False
        """
        if not open_id:
            logger.error("open_id 为空，无法发送消息")
            return False

        try:
            token = self.get_tenant_access_token()

            # 构建卡片内容
            card_elements = [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "🎉 **您的账号已自动注册成功！**"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": "**📧 登录名**\n" + username
                            }
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": "**🔑 初始密码**\n" + password
                            }
                        }
                    ]
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "⚠️ **请妥善保管您的密码，建议首次登录后修改。**"
                    }
                }
            ]

            if frontend_url:
                card_elements.append({
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "🌐 前往登录"
                            },
                            "type": "primary",
                            "url": frontend_url
                        }
                    ]
                })

            card = {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "template": "green",
                    "title": {
                        "tag": "plain_text",
                        "content": "账号注册成功"
                    }
                },
                "elements": card_elements
            }

            # 发送消息
            url = f"{self.base_url}/open-apis/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            params = {"receive_id_type": "open_id"}

            import json
            payload = {
                "receive_id": open_id,
                "msg_type": "interactive",
                "content": json.dumps(card)
            }

            logger.debug(f"发送账号通知卡片: open_id={open_id[:8]}..., username={username}")
            response = requests.post(url, headers=headers, json=payload, params=params, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("code") == 0:
                logger.info(f"账号通知卡片发送成功: open_id={open_id[:8]}..., username={username}")
                return True
            else:
                logger.error(f"账号通知卡片发送失败: {result.get('msg')}")
                # 降级到文本消息
                logger.info("降级为文本消息发送")
                return self.send_private_message(open_id, username, password, frontend_url)

        except Exception as e:
            logger.error(f"发送账号通知卡片异常: {e}", exc_info=True)
            # 降级到文本消息
            logger.info("降级为文本消息发送")
            return self.send_private_message(open_id, username, password, frontend_url)
