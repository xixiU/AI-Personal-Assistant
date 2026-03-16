"""
飞书机器人 API 适配器

基于飞书开放平台 API 实现消息接收和发送
"""
import json
import time
import base64
import hashlib
import hmac
import requests
from typing import List, Optional, Dict, Any
from datetime import datetime
from loguru import logger

from ai_assistant.adapters.base import IMAdapter
from ai_assistant.core.models import Message, Content


class FeishuBotAdapter(IMAdapter):
    """飞书机器人 API 适配器"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化飞书机器人适配器

        Args:
            config: 配置字典，包含 app_id, app_secret 等
        """
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.verification_token = config.get("verification_token", "")
        self.encrypt_key = config.get("encrypt_key", "")
        self.allowed_chats = config.get("allowed_chats", [])
        self.allowed_users = config.get("allowed_users", [])

        self.tenant_access_token = None
        self.token_expire_time = 0

        # 存储最新接收的消息（由 webhook 服务器设置）
        self.latest_message = None
        self.latest_event = None

    def get_tenant_access_token(self) -> str:
        """
        获取 tenant_access_token

        Returns:
            访问令牌
        """
        # 检查 token 是否过期
        if self.tenant_access_token and time.time() < self.token_expire_time:
            return self.tenant_access_token

        # 获取新 token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()

            if result.get("code") == 0:
                self.tenant_access_token = result["tenant_access_token"]
                # token 有效期 2 小时，提前 5 分钟刷新
                self.token_expire_time = time.time() + result.get("expire", 7200) - 300
                logger.info("Tenant access token obtained successfully")
                return self.tenant_access_token
            else:
                logger.error(f"Failed to get tenant access token: {result}")
                raise Exception(f"获取访问令牌失败: {result.get('msg')}")

        except Exception as e:
            logger.error(f"Error getting tenant access token: {e}")
            raise

    def verify_webhook_signature(self, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
        """
        验证 webhook 签名

        Args:
            timestamp: 时间戳
            nonce: 随机数
            encrypt: 加密数据
            signature: 签名

        Returns:
            验证是否通过
        """
        if not self.encrypt_key:
            return True  # 如果没有配置加密密钥，跳过验证

        # 拼接字符串
        sign_str = f"{timestamp}{nonce}{encrypt}{self.encrypt_key}"

        # 计算 SHA256
        calculated_signature = hashlib.sha256(sign_str.encode()).hexdigest()

        return calculated_signature == signature

    def _decrypt(self, encrypt_str: str) -> Dict[str, Any]:
        """
        解密飞书加密数据（AES-256-CBC）

        Args:
            encrypt_str: base64 编码的加密字符串

        Returns:
            解密后的 JSON 数据
        """
        from Crypto.Cipher import AES

        # key = SHA256(encrypt_key)，得到 32 字节
        key = hashlib.sha256(self.encrypt_key.encode()).digest()

        # base64 解码，前 16 字节为 IV，其余为密文
        encrypt_bytes = base64.b64decode(encrypt_str)
        iv = encrypt_bytes[:16]
        ciphertext = encrypt_bytes[16:]

        # AES-256-CBC 解密
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(ciphertext)

        # 去除 PKCS7 padding
        pad_len = decrypted[-1]
        decrypted = decrypted[:-pad_len]

        return json.loads(decrypted.decode("utf-8"))

    def handle_webhook_event(self, event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        处理 webhook 事件

        Args:
            event_data: 事件数据

        Returns:
            响应数据（如果需要）
        """
        # 如果配置了加密，先解密
        if self.encrypt_key and "encrypt" in event_data:
            try:
                event_data = self._decrypt(event_data["encrypt"])
                logger.debug("Webhook event decrypted")
            except Exception as e:
                logger.error(f"Failed to decrypt webhook event: {e}")
                return None

        # URL 验证（飞书开放平台配置 webhook 时的验证请求）
        if event_data.get("type") == "url_verification":
            challenge = event_data.get("challenge", "")
            logger.info("URL verification challenge received")
            return {"challenge": challenge}

        # 事件回调 v2.0 格式
        header = event_data.get("header", {})
        if header.get("event_type") == "im.message.receive_v1":
            self.latest_event = event_data
            logger.info(f"Message event received: {header.get('event_id', 'unknown')}")

        return None

    def detect_active_window(self) -> bool:
        """
        检测是否有新消息（机器人模式不需要检测窗口）

        Returns:
            是否有新消息待处理
        """
        return self.latest_event is not None

    def extract_messages(self, count: int = 10) -> List[Message]:
        """
        提取消息（机器人模式暂不实现历史消息提取）

        Args:
            count: 消息数量

        Returns:
            消息列表
        """
        return []

    def check_trigger(self, keyword: str) -> bool:
        """
        检查是否触发关键词

        Args:
            keyword: 触发关键词

        Returns:
            是否触发
        """
        if not self.latest_event:
            return False

        try:
            event = self.latest_event["event"]
            message = event["message"]
            chat_type = message.get("chat_type", "p2p")  # p2p=私聊, group=群聊

            # 检查白名单
            chat_id = message.get("chat_id", "")
            sender_id = event["sender"]["sender_id"].get("open_id", "")

            if self.allowed_chats and chat_id not in self.allowed_chats:
                logger.debug(f"Chat {chat_id} not in whitelist")
                self.latest_event = None
                return False

            if self.allowed_users and sender_id not in self.allowed_users:
                logger.debug(f"User {sender_id} not in whitelist")
                self.latest_event = None
                return False

            # 解析消息内容
            content_str = message.get("content", "{}")
            content_data = json.loads(content_str)

            # 仅处理文本消息
            if message.get("message_type") != "text":
                logger.debug(f"Skipping non-text message type: {message.get('message_type')}")
                self.latest_event = None
                return False

            text = content_data.get("text", "")

            # 群聊：需要包含触发关键词（用户可将关键词设为 @机器人 相关内容）
            # 私聊：直接检查关键词
            if keyword in text:
                logger.info(f"Trigger keyword '{keyword}' detected in {chat_type} chat")
                self.latest_message = {
                    "message_id": message.get("message_id"),
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "text": text,
                    "sender_id": sender_id,
                    "sender_name": event["sender"].get("sender_id", {}).get("open_id", "")
                }
                return True

            logger.debug(f"No trigger keyword in message (chat_type={chat_type})")
            self.latest_event = None
            return False

        except Exception as e:
            logger.error(f"Error checking trigger: {e}")
            self.latest_event = None
            return False

    def get_session_id(self) -> Optional[str]:
        """
        获取会话 ID

        Returns:
            会话 ID
        """
        if self.latest_message:
            return self.latest_message.get("chat_id")
        return None

    def get_last_message_as_message(self) -> Optional[Message]:
        """
        获取最后的消息作为 Message 对象

        Returns:
            Message 对象
        """
        if not self.latest_message:
            return None

        content = Content(type="text", data=self.latest_message["text"])
        return Message(
            role="user",
            content=[content],
            timestamp=datetime.now()
        )

    def send_reply(self, reply_text: str) -> bool:
        """
        回复消息（使用 reply 接口，保持消息线程）

        Args:
            reply_text: 回复文本

        Returns:
            是否成功
        """
        if not self.latest_message:
            logger.error("No message to reply to")
            return False

        try:
            token = self.get_tenant_access_token()
            message_id = self.latest_message["message_id"]

            # 使用 reply 接口回复具体消息，保持消息线程
            url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            payload = {
                "msg_type": "text",
                "content": json.dumps({"text": reply_text})
            }

            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()

            if result.get("code") == 0:
                logger.info(f"Reply sent to message {message_id}")
                self.latest_event = None
                self.latest_message = None
                return True
            else:
                logger.error(f"Failed to send reply: {result}")
                return False

        except Exception as e:
            logger.error(f"Error sending reply: {e}")
            return False

    def clear_latest_event(self):
        """清除最新事件（处理完成后调用）"""
        self.latest_event = None
        self.latest_message = None
