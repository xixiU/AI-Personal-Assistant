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
        self.base_url = config.get("base_url", "https://open.feishu.cn")  # 支持私有化部署
        self.allowed_chats = config.get("allowed_chats", [])
        self.allowed_users = config.get("allowed_users", [])

        self.tenant_access_token = None
        self.token_expire_time = 0

        # 存储最新接收的消息（由 webhook 服务器设置）
        self.latest_message = None
        self.latest_event = None

        # 欢迎消息配置
        self.welcome_message = config.get("welcome_message", "")

    def add_reaction(self, message_id: str, emoji_type: str = "THINKING_FACE") -> bool:
        """
        给消息添加表情回复

        Args:
            message_id: 消息 ID
            emoji_type: 表情类型，默认 THINKING_FACE（🤔）
                       常用表情：THINKING_FACE, OK_HAND, THUMBSUP, EYES, FIRE

        Returns:
            是否成功
        """
        try:
            token = self.get_tenant_access_token()
            url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}/reactions"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            payload = {
                "reaction_type": {
                    "emoji_type": emoji_type
                }
            }

            logger.debug(f"添加表情回复: message_id={message_id}, emoji={emoji_type}")
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("code") == 0:
                logger.info(f"表情回复成功: {emoji_type}")
                return True
            else:
                logger.warning(f"表情回复失败: {result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"添加表情回复失败: {e}")
            return False

    def download_image(self, message_id: str, image_key: str) -> Optional[Dict[str, str]]:
        """
        下载飞书消息中的图片

        Args:
            message_id: 消息 ID
            image_key: 图片 key

        Returns:
            包含 base64 编码图片和 media_type 的字典，失败返回 None
        """
        try:
            token = self.get_tenant_access_token()
            url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}/resources/{image_key}"
            headers = {"Authorization": f"Bearer {token}"}
            params = {"type": "image"}

            logger.debug(f"下载飞书图片: message_id={message_id}, image_key={image_key}")
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()

            # 获取图片类型
            content_type = response.headers.get("Content-Type", "image/png")

            # base64 编码
            image_data = base64.b64encode(response.content).decode("utf-8")

            logger.info(f"图片下载成功: size={len(response.content)} bytes, type={content_type}")
            return {
                "data": image_data,
                "media_type": content_type
            }
        except Exception as e:
            logger.error(f"下载飞书图片失败: {e}")
            return None

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
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }

        logger.debug(f"Requesting tenant_access_token with app_url ={url}, app_id={self.app_id[:10]}...")

        try:
            response = requests.post(url, json=payload, timeout=10)

            response.raise_for_status()
            result = response.json()

            logger.info(f"Token API response: {result}")

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

    def process_webhook_event(self, event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        处理 webhook 事件（后台线程中执行，可以执行耗时操作）

        这个方法在后台线程中执行，可以安全地执行耗时操作（如调用飞书 API）。

        Args:
            event_data: 原始 webhook 数据

        Returns:
            处理后的事件数据（需要 AI 回复的消息），如果不需要 AI 回复则返回 None
        """
        try:
            # 如果配置了加密，先解密
            if self.encrypt_key and "encrypt" in event_data:
                try:
                    event_data = self._decrypt(event_data["encrypt"])
                    logger.info(f"Webhook event decrypted successfully")
                except Exception as e:
                    logger.error(f"Failed to decrypt webhook event: {e}")
                    return None

            # 记录事件类型（解密后才能读取）
            event_type = event_data.get('header', {}).get('event_type', event_data.get('type', 'unknown'))
            logger.info(f"📨 Processing event_type: {event_type}")

            # v1.0 旧版事件回调格式（私有化飞书可能使用）
            if event_data.get("type") == "event_callback":
                logger.info("📨 Received v1.0 event_callback format")
                event = event_data.get("event", {})
                msg_type = event.get("msg_type", "")
                text = event.get("text_without_at_bot", "") or event.get("text", "")
                chat_id = event.get("open_chat_id", "")
                message_id = event.get("open_message_id", "")
                sender_id = event.get("open_id", "")

                logger.info(f"📩 v1.0 Message: chat_id={chat_id}, msg_type={msg_type}, sender={sender_id}")

                if msg_type == "text" and text:
                    # 转换为 v2.0 兼容格式，返回给主流程处理
                    return {
                        "header": {"event_id": event_data.get("uuid", "unknown")},
                        "event": {
                            "message": {
                                "message_id": message_id,
                                "chat_id": chat_id,
                                "chat_type": event.get("chat_type", ""),
                                "message_type": "text",
                                "content": json.dumps({"text": text})
                            },
                            "sender": {
                                "sender_id": {"open_id": sender_id}
                            }
                        }
                    }
                return None

            # v2.0 事件格式
            header = event_data.get("header", {})
            event_type = header.get("event_type")

            # 处理机器人入群事件（发送欢迎消息）
            if event_type == "im.chat.member.bot.added_v1":
                event = event_data.get("event", {})
                chat_id = event.get("chat_id", "")
                logger.info(f"🎉 Bot added to chat: {chat_id}")

                if self.welcome_message:
                    logger.info(f"🚀 Sending welcome message to chat_id={chat_id}")
                    self._send_welcome_message(chat_id)
                return None  # 欢迎消息不需要 AI 回复

            # 处理消息接收事件
            if event_type == "im.message.receive_v1":
                event = event_data.get("event", {})
                message = event.get("message", {})
                message_type = message.get("message_type", "")

                # 系统消息（如机器人入群通知），发送欢迎消息但不触发 AI
                if message_type == "system":
                    chat_id = message.get("chat_id", "")
                    logger.info(f"🔔 System message in chat_id={chat_id}")
                    if self.welcome_message:
                        self._send_welcome_message(chat_id)
                    return None

                # 普通文本消息，返回给主流程处理
                return event_data

            return None

        except Exception as e:
            logger.error(f"Failed to process webhook event: {e}")
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

        飞书机器人模式：所有消息都触发（用户主动发给机器人的消息本身就是意图明确的）
        keyword 参数保留用于兼容接口，但不实际使用

        Args:
            keyword: 触发关键词（飞书机器人模式下忽略）

        Returns:
            是否触发
        """
        if not self.latest_event:
            logger.debug("check_trigger: no latest_event")
            return False

        try:
            event = self.latest_event["event"]
            message = event["message"]
            chat_type = message.get("chat_type", "p2p")  # p2p=私聊, group=群聊

            # 检查白名单
            chat_id = message.get("chat_id", "")
            sender_id = event["sender"]["sender_id"].get("open_id", "")

            if self.allowed_chats and chat_id not in self.allowed_chats:
                logger.info(f"❌ Chat {chat_id} not in whitelist, skipping")
                self.latest_event = None
                return False

            if self.allowed_users and sender_id not in self.allowed_users:
                logger.info(f"❌ User {sender_id} not in whitelist, skipping")
                self.latest_event = None
                return False

            # 解析消息内容
            content_str = message.get("content", "{}")
            content_data = json.loads(content_str)

            # 仅处理文本消息
            if message.get("message_type") != "text":
                logger.info(f"⏭️  Skipping non-text message type: {message.get('message_type')}")
                self.latest_event = None
                return False

            text = content_data.get("text", "")

            # 飞书机器人模式：所有文本消息都触发（不检查关键词）
            logger.info(f"🎯 Message received in {chat_type} chat, triggering AI response")
            self.latest_message = {
                "message_id": message.get("message_id"),
                "chat_id": chat_id,
                "chat_type": chat_type,
                "text": text,
                "sender_id": sender_id,
                "sender_name": event["sender"].get("sender_id", {}).get("open_id", "")
            }
            return True

        except Exception as e:
            logger.error(f"❌ Error checking trigger: {e}", exc_info=True)
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
        回复消息（使用消息卡片样式，保持消息线程）

        Args:
            reply_text: 回复文本

        Returns:
            是否成功
        """
        if not self.latest_message:
            logger.error("❌ No message to reply to")
            return False

        try:
            from ai_assistant.utils.feishu_message import FeishuMessageBuilder

            token = self.get_tenant_access_token()
            message_id = self.latest_message["message_id"]
            chat_id = self.latest_message.get("chat_id", "")

            logger.info(f"📤 Sending reply to message_id={message_id}, chat_id={chat_id}")
            logger.debug(f"Reply content: {reply_text[:100]}")

            payload = FeishuMessageBuilder.ai_reply_card(reply_text)
            success = FeishuMessageBuilder.send(self.base_url, token, message_id, payload)

            if success:
                self.latest_event = None
                self.latest_message = None
            return success

        except Exception as e:
            logger.error(f"❌ Error sending reply: {e}", exc_info=True)
            return False

    def clear_latest_event(self):
        """清除最新事件（处理完成后调用）"""
        self.latest_event = None
        self.latest_message = None

    def _send_welcome_message(self, chat_id: str):
        """
        向群聊发送欢迎消息

        Args:
            chat_id: 群聊 ID
        """
        logger.info(f"📤 _send_welcome_message called with chat_id={chat_id}")

        try:
            logger.info("🔑 Getting tenant access token...")
            token = self.get_tenant_access_token()
            logger.info(f"✅ Token obtained: {token[:20]}...")

            url = f"{self.base_url}/open-apis/im/v1/messages"
            logger.info(f"🌐 API URL: {url}")

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            # 检查欢迎消息中是否包含 HTTP 链接，如果有则使用富文本发送
            logger.info("🔨 Building welcome message content...")
            msg_type, content = self._build_welcome_content(self.welcome_message)
            logger.info(f"📋 Message type: {msg_type}")
            logger.debug(f"📄 Message content: {content}")

            params = {"receive_id_type": "chat_id"}
            payload = {
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": content
            }

            logger.info(f"📤 Sending welcome message to chat_id={chat_id}")
            logger.debug(f"Request payload: {json.dumps(payload, ensure_ascii=False)}")

            response = requests.post(url, headers=headers, json=payload, params=params, timeout=10)
            logger.info(f"📡 Response status: {response.status_code}")
            logger.debug(f"Response headers: {response.headers}")

            response.raise_for_status()
            result = response.json()
            logger.info(f"📥 Response body: {json.dumps(result, ensure_ascii=False)}")

            if result.get("code") == 0:
                logger.info(f"✅ Welcome message sent successfully to chat {chat_id}")
            else:
                logger.error(f"❌ Failed to send welcome message: code={result.get('code')}, msg={result.get('msg')}")

        except Exception as e:
            logger.error(f"❌ Error sending welcome message: {e}", exc_info=True)

    def _build_welcome_content(self, text: str) -> tuple:
        """
        构建欢迎消息内容，自动检测链接并使用富文本格式

        Args:
            text: 欢迎消息文本

        Returns:
            (msg_type, content_json_str) 元组
        """
        import re
        url_pattern = re.compile(r'(https?://\S+)')
        urls = url_pattern.findall(text)

        if not urls:
            # 纯文本消息
            return "text", json.dumps({"text": text})

        # 包含链接，使用富文本（post）格式，链接可点击跳转
        parts = url_pattern.split(text)
        content_elements = []

        for part in parts:
            if not part:
                continue
            if url_pattern.match(part):
                content_elements.append({
                    "tag": "a",
                    "text": part,
                    "href": part
                })
            else:
                content_elements.append({
                    "tag": "text",
                    "text": part
                })

        post_content = {
            "zh_cn": {
                "content": [content_elements]
            }
        }

        return "post", json.dumps(post_content)
