"""
飞书机器人 API 适配器

基于飞书开放平台 API 实现消息接收和发送
飞书卡片消息文档:https://open.feishu.cn/document/feishu-cards/card-callback-communication#c98c3220
消息卡片有1.0格式的，还有2.0格式的
"""
import json
import time
import base64
import hashlib
import hmac
import threading
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
        self.bot_config = config  # 保存完整配置供其他模块访问
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

        # Context Manager 引用（由外部设置）
        self.context_manager = None

        # 消息映射缓存（飞书 message_id -> chat_history record_id）
        # 供反馈按钮点击时反查对应的对话历史记录主键
        self.message_cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()

        # 用户昵称缓存（open_id -> name），避免反馈时重复调用飞书通讯录 API
        self._user_name_cache: Dict[str, str] = {}

        # 历史消息回溯配置（支持"先发图、后发文字"场景）
        self.lookback_count = config.get("lookback_count", 5)

        # 日志文件管理器（由 main.py 注入；None 表示不处理文件附件）
        self.log_file_manager = config.get("log_file_manager")

    def cache_message_record(self, message_id: str, record_id: str):
        """
        缓存飞书消息 ID 到 record_id 的映射

        由 main.py 在发送回复卡片成功后调用，建立
        新消息 message_id → 对话历史 record_id 的映射，
        供 process_card_action 在用户点击反馈按钮时反查。

        Args:
            message_id: 飞书回复消息的 message_id
            record_id: 对话历史记录的唯一标识
        """
        with self._cache_lock:
            self.message_cache[message_id] = record_id
            logger.debug(f"Cached mapping: message_id={message_id} → record_id={record_id}")

    def add_reaction(self, message_id: str, emoji_type: str = "THINKING") -> bool:
        """
        给消息添加表情回复

        Args:
            message_id: 消息 ID
            emoji_type: 表情类型，默认 THINKING
                       常用表情：THINKING, OK_HAND, THUMBSUP, EYES, FIRE

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

    @staticmethod
    def _parse_message_content(message_type: str, content_data: dict) -> tuple:
        """
        按飞书消息类型解析出文本、图片 key 列表和文件附件列表

        飞书各类型 content 结构：
        - text:  {"text": "..."}
        - image: {"image_key": "img_v2_xxx"}
        - file:  {"file_key": "file_v3_xxx", "file_name": "app.log"}
        - post:  {"content": [[{"tag": "text", "text": "..."},
                               {"tag": "img", "image_key": "img_v2_xxx"},
                               {"tag": "media"/"file", "file_key": "...", "file_name": "..."}], ...]}

        Args:
            message_type: 消息类型 text / image / file / post
            content_data: 已 json.loads 的 content 字典

        Returns:
            (text, image_keys, files) 三元组；files 为 [{"file_key","file_name"}]
        """
        text = ""
        image_keys = []
        files = []

        if message_type == "text":
            text = content_data.get("text", "")

        elif message_type == "image":
            key = content_data.get("image_key")
            if key:
                image_keys.append(key)

        elif message_type == "file":
            key = content_data.get("file_key")
            if key:
                files.append({
                    "file_key": key,
                    "file_name": content_data.get("file_name") or "attachment",
                })

        elif message_type == "post":
            # post 是二维数组：段落列表，每段是元素列表
            text_parts = []
            for paragraph in content_data.get("content", []) or []:
                if not isinstance(paragraph, list):
                    continue
                for element in paragraph:
                    if not isinstance(element, dict):
                        continue
                    tag = element.get("tag")
                    if tag in ("text", "a", "at"):
                        text_parts.append(element.get("text", ""))
                    elif tag == "img":
                        key = element.get("image_key")
                        if key:
                            image_keys.append(key)
                    elif tag in ("media", "file"):
                        key = element.get("file_key")
                        if key:
                            files.append({
                                "file_key": key,
                                "file_name": element.get("file_name") or "attachment",
                            })
                text_parts.append("\n")
            text = "".join(text_parts).strip()

        return text, image_keys, files

    def download_file(self, message_id: str, file_key: str) -> Optional[bytes]:
        """
        下载飞书消息中的文件附件

        与 download_image 的差别：resources 接口的 type 参数为 file，
        且返回的是原始字节（不做 base64，日志文件按文本处理）。

        Args:
            message_id: 消息 ID
            file_key: 文件 key

        Returns:
            文件字节；失败返回 None
        """
        try:
            token = self.get_tenant_access_token()
            url = f"{self.base_url}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
            headers = {"Authorization": f"Bearer {token}"}
            params = {"type": "file"}

            logger.debug(f"下载飞书文件: message_id={message_id}, file_key={file_key}")
            response = requests.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            logger.info(f"文件下载成功: size={len(response.content)} bytes")
            return response.content
        except Exception as e:
            logger.error(f"下载飞书文件失败: {e}")
            return None

    def _collect_log_files(self, message_id: str, files: List[Dict[str, str]], chat_id: str) -> List[Content]:
        """
        下载并落地文件附件，产出 log_file 类型的 Content 块。

        小文本日志把正文一并带上（AI 可直接分析）；zip 解压后只给路径与文件清单，
        由 AI 用代码排查工具按需读取，避免把整包日志塞进上下文。

        Args:
            message_id: 消息 ID（下载资源需要）
            files: [{"file_key","file_name"}] 列表
            chat_id: 会话 ID（用于分目录存放）

        Returns:
            log_file 类型的 Content 列表
        """
        manager = self.log_file_manager
        if not manager or not getattr(manager, "enabled", False) or not files:
            return []

        contents: List[Content] = []
        for item in files:
            file_name = item.get("file_name") or "attachment"
            if not manager.is_allowed(file_name):
                logger.info(f"跳过非日志类附件: {file_name}")
                continue

            data = self.download_file(message_id, item.get("file_key", ""))
            if not data:
                continue

            path = manager.save_bytes(chat_id, file_name, data)
            if not path:
                # 落地失败或超过大小上限：告知 AI 这一情况，不静默丢弃
                contents.append(Content(type="log_file", data={
                    "name": file_name,
                    "path": "",
                    "text": "",
                    "note": "文件超过大小上限或保存失败，未能读取内容",
                }))
                continue

            if path.lower().endswith(".zip"):
                out_dir, extracted = manager.extract_zip(path)
                listing = "\n".join(f"- {p}" for p in extracted[:50])
                contents.append(Content(type="log_file", data={
                    "name": file_name,
                    "path": out_dir or path,
                    "text": "",
                    "note": (
                        f"这是一个日志压缩包，已解压到本地目录 {out_dir or path}。"
                        f"其中文本日志 {len(extracted)} 个：\n{listing}"
                        if extracted else
                        f"这是一个压缩包，已解压到 {out_dir or path}，未发现文本日志文件。"
                    ),
                }))
            else:
                contents.append(Content(type="log_file", data={
                    "name": file_name,
                    "path": path,
                    "text": manager.read_text_head(path),
                    "note": "",
                }))

        return contents

    def _fetch_recent_messages(self, chat_id: str, current_message_id: str, count: int) -> List[Dict[str, Any]]:
        """
        拉取指定会话的最近 N 条消息（不含当前消息）

        Args:
            chat_id: 会话 ID
            current_message_id: 当前触发的消息 ID（向前回溯时排除此消息，避免重复）
            count: 拉取条数

        Returns:
            消息列表（按时间升序，最老的在前），每条含 message_id / message_type / content / create_time
        """
        if count <= 0:
            return []

        try:
            token = self.get_tenant_access_token()
            url = f"{self.base_url}/open-apis/im/v1/messages"
            headers = {"Authorization": f"Bearer {token}"}
            # container_id_type 必须与 container_id 格式匹配：chat_id 用 chat
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": min(count + 5, 50),  # 多拉几条用于过滤当前消息后还够 N 条
            }

            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data.get("code") != 0:
                logger.warning(f"拉取历史消息失败: code={data.get('code')}, msg={data.get('msg')}")
                return []

            items = data.get("data", {}).get("items", [])
            # 过滤掉当前消息，避免重复
            items = [m for m in items if m.get("message_id") != current_message_id]
            # 飞书返回的是降序（最新在前），翻转成升序（最老在前）
            items.reverse()
            # 只取最近 count 条
            return items[-count:] if len(items) > count else items

        except Exception as e:
            logger.error(f"拉取历史消息异常: {e}")
            return []

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

            # 获取图片类型：去掉 "; charset=xxx" 后缀，Anthropic 只接受纯 MIME
            content_type = response.headers.get("Content-Type", "image/png")
            content_type = content_type.split(";")[0].strip().lower()

            # 非标准/缺失类型时按魔术字节兜底，避免 media_type 不合法被 API 拒绝
            SUPPORTED = {"image/png", "image/jpeg", "image/gif", "image/webp"}
            if content_type not in SUPPORTED:
                raw = response.content
                if raw.startswith(b"\xff\xd8\xff"):
                    detected = "image/jpeg"
                elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    detected = "image/png"
                elif raw.startswith(b"GIF8"):
                    detected = "image/gif"
                elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                    detected = "image/webp"
                else:
                    detected = "image/png"
                logger.warning(f"图片 Content-Type 不受支持({content_type})，按魔术字节判定为 {detected}")
                content_type = detected

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

    def get_user_name(self, open_id: str) -> str:
        """
        通过飞书通讯录 API 获取用户昵称（用于反馈卡片展示反馈人）

        接口: GET /open-apis/contact/v3/users/{user_id}?user_id_type=open_id
        所需权限（需在飞书开放平台后台申请并发布）:
            - 获取用户基本信息  contact:user.base:readonly
              （或"以应用身份读取通讯录" contact:contact.base:readonly）
        若未申请权限 / 调用失败，返回空字符串，调用方需做好降级（不展示昵称）。

        结果带内存缓存，避免同一用户重复点击反馈时反复请求飞书 API。

        Args:
            open_id: 用户 open_id（来自卡片交互事件 operator.open_id）

        Returns:
            用户昵称，获取失败返回空字符串
        """
        if not open_id:
            return ""

        # 命中缓存直接返回
        with self._cache_lock:
            cached = self._user_name_cache.get(open_id)
        if cached is not None:
            return cached

        try:
            token = self.get_tenant_access_token()
            url = f"{self.base_url}/open-apis/contact/v3/users/{open_id}"
            headers = {"Authorization": f"Bearer {token}"}
            params = {"user_id_type": "open_id"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            result = response.json()

            if result.get("code") == 0:
                user_name = result.get("data", {}).get("user", {}).get("name", "") or ""
                # 缓存结果（含空串，避免无权限时对同一用户反复请求）
                with self._cache_lock:
                    self._user_name_cache[open_id] = user_name
                logger.info(f"获取用户昵称成功: open_id={open_id[:8]}..., name={user_name}")
                return user_name
            else:
                # 常见：code=99991672 无 contact 权限。记录告警但不阻断反馈主流程。
                logger.warning(
                    f"获取用户昵称失败(可能未申请通讯录权限): code={result.get('code')}, "
                    f"msg={result.get('msg')}。反馈仍会正常保存，仅不展示昵称。"
                )
                with self._cache_lock:
                    self._user_name_cache[open_id] = ""  # 缓存空串，避免重复失败请求
                return ""
        except Exception as e:
            logger.warning(f"调用飞书用户信息 API 异常（不影响反馈保存）: {e}")
            return ""

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

                # v1.0 图片消息：image_key 在事件顶层
                v1_image_key = event.get("image_key", "")

                if msg_type == "text" and text:
                    normalized_type = "text"
                    normalized_content = json.dumps({"text": text})
                elif msg_type == "image" and v1_image_key:
                    normalized_type = "image"
                    normalized_content = json.dumps({"image_key": v1_image_key})
                else:
                    return None

                # 转换为 v2.0 兼容格式，返回给主流程处理
                return {
                    "header": {"event_id": event_data.get("uuid", "unknown")},
                    "event": {
                        "message": {
                            "message_id": message_id,
                            "chat_id": chat_id,
                            "chat_type": event.get("chat_type", ""),
                            "message_type": normalized_type,
                            "content": normalized_content
                        },
                        "sender": {
                            "sender_id": {"open_id": sender_id}
                        }
                    }
                }

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

            # 支持的消息类型：text（纯文本）、image（图片，如日志截图）、post（图文混排富文本）
            message_type = message.get("message_type", "")
            if message_type not in ("text", "image", "post", "file"):
                logger.info(f"⏭️  Skipping unsupported message type: {message_type}")
                self.latest_event = None
                return False

            # 按消息类型解析文本、图片 key 与文件附件
            text, image_keys, files = self._parse_message_content(message_type, content_data)

            # 图片/文件消息通常没有文字说明，给一句默认提问，避免 text 为空
            if image_keys and not text.strip():
                text = "请分析这张截图中的问题。"
            elif files and not text.strip():
                text = "请分析这个日志文件中的问题。"

            if not text.strip() and not image_keys and not files:
                logger.info(f"⏭️  Skipping empty message (type={message_type})")
                self.latest_event = None
                return False

            logger.info(
                f"🎯 Message received in {chat_type} chat "
                f"(type={message_type}, images={len(image_keys)}, files={len(files)}), "
                f"triggering AI response"
            )
            self.latest_message = {
                "message_id": message.get("message_id"),
                "chat_id": chat_id,
                "chat_type": chat_type,
                "text": text,
                "image_keys": image_keys,
                "files": files,
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
        获取最后的消息作为 Message 对象，支持回溯最近 N 条历史消息

        当配置 lookback_count > 0 时，会向前回溯 N 条消息，收集其中的图片和文本
        （支持"用户先发图、再发文字"场景）。历史消息的文本会标注"[历史消息]"前缀。

        Returns:
            Message 对象
        """
        if not self.latest_message:
            return None

        contents = []
        chat_id = self.latest_message.get("chat_id")
        current_message_id = self.latest_message.get("message_id")

        # 回溯历史消息（拉取最近 N 条，不含当前消息）
        history_images = 0
        history_texts = []
        if self.lookback_count > 0 and chat_id and current_message_id:
            recent_messages = self._fetch_recent_messages(chat_id, current_message_id, self.lookback_count)
            logger.info(f"回溯历史消息: 拉取 {len(recent_messages)} 条")

            for msg in recent_messages:
                msg_type = msg.get("message_type", "")
                content_str = msg.get("content", "{}")
                try:
                    content_data = json.loads(content_str)
                except Exception:
                    continue

                # 解析历史消息的文本和图片（回溯不下载历史文件附件，只取图片与文本：
                # 历史文件与当前提问的相关性弱，下载大文件代价高且易误导）
                hist_text, hist_image_keys, _hist_files = self._parse_message_content(msg_type, content_data)
                hist_msg_id = msg.get("message_id")

                # 收集历史图片。打 from_history 标记：回溯图片与当前消息图片会合并进
                # 同一个 Message，若不区分，模式判定无法分辨"用户这次发的图"和
                # "群里别人早前发的无关截图"，后者不应触发代码排查模式。
                for image_key in hist_image_keys:
                    if not hist_msg_id:
                        continue
                    image_data = self.download_image(hist_msg_id, image_key)
                    if image_data:
                        marked = dict(image_data)
                        marked["from_history"] = True
                        contents.append(Content(type="image", data=marked))
                        history_images += 1
                    else:
                        logger.warning(f"历史图片下载失败: message_id={hist_msg_id}, image_key={image_key}")

                # 收集历史文本
                if hist_text.strip():
                    history_texts.append(hist_text.strip())

        # 当前消息的图片（放在历史图片之后，当前文本之前）
        current_image_keys = self.latest_message.get("image_keys") or []
        current_images = 0
        for image_key in current_image_keys:
            if not current_message_id:
                logger.warning("缺少 message_id，无法下载图片")
                break
            image_data = self.download_image(current_message_id, image_key)
            if image_data:
                contents.append(Content(type="image", data=image_data))
                current_images += 1
            else:
                logger.warning(f"图片下载失败，已跳过: image_key={image_key}")

        # 当前消息的日志文件附件（下载 + 落地，产出 log_file 内容块）
        current_files = self.latest_message.get("files") or []
        log_contents: List[Content] = []
        if current_files and current_message_id and chat_id:
            log_contents = self._collect_log_files(current_message_id, current_files, chat_id)
            contents.extend(log_contents)

        # 构造文本部分：历史文本 + 当前文本
        text_parts = []
        if history_texts:
            text_parts.append(
                f"[最近 {len(history_texts)} 条历史消息，可能与当前问题相关或无关，请自行判断]:\n" +
                "\n".join(f"- {t}" for t in history_texts)
            )
        current_text = self.latest_message.get("text", "").strip()
        if current_text:
            if history_texts:
                text_parts.append(f"\n[当前消息]: {current_text}")
            else:
                text_parts.append(current_text)

        if text_parts:
            contents.append(Content(type="text", data="\n".join(text_parts)))

        if not contents:
            logger.warning("消息既无可用图片、文件也无文本，跳过")
            return None

        logger.info(
            f"构造消息: {len(contents)} 个内容块 "
            f"(历史图片 {history_images}, 当前图片 {current_images}, "
            f"日志文件 {len(log_contents)}, 历史文本 {len(history_texts)} 条)"
        )
        return Message(
            role="user",
            content=contents,
            timestamp=datetime.now()
        )

    def send_reply(
        self,
        reply_text: str,
        elapsed_time: Optional[float] = None,
        tool_rounds: Optional[int] = None,
        doc_count: Optional[int] = None,
        mode: Optional[str] = None,
        record_id: Optional[str] = None,
    ) -> bool:
        """
        回复消息（使用消息卡片样式，保持消息线程）

        Args:
            reply_text: 回复文本
            elapsed_time: 回复耗时（秒），可选
            tool_rounds: 工具调用轮数（Agentic 模式），可选
            doc_count: 阅读文档数量（RAG 模式），可选
            mode: 处理模式（"agentic" / "rag" / "standard"），可选
            record_id: 对话历史记录 ID，可选（用于查询已有反馈）

        Returns:
            是否成功
        """
        if not self.latest_message:
            logger.error("❌ No message to reply to")
            return False

        try:
            from ai_assistant.utils.feishu_message import FeishuMessageBuilder
            from ai_assistant.core.feedback_manager import FeedbackManager

            token = self.get_tenant_access_token()
            message_id = self.latest_message["message_id"]
            chat_id = self.latest_message.get("chat_id", "")
            user_query = self.latest_message.get("text", "")

            logger.info(f"📤 Sending reply to message_id={message_id}, chat_id={chat_id}")
            logger.debug(f"Reply content: {reply_text[:100]}")

            # 查询已有的反馈记录（如果提供了 record_id）
            feedbacks = None
            if record_id:
                try:
                    feedback_mgr = FeedbackManager()
                    feedbacks = feedback_mgr.get_feedbacks_by_record_id(record_id)
                    if feedbacks:
                        logger.info(f"📝 Found {len(feedbacks)} existing feedbacks for record_id={record_id}")
                except Exception as e:
                    logger.warning(f"Failed to fetch feedbacks: {e}")

            # 构建卡片（带反馈按钮、元数据和已有反馈）
            payload = FeishuMessageBuilder.ai_reply_card(
                reply_text,
                message_id="placeholder",
                elapsed_time=elapsed_time,
                tool_rounds=tool_rounds,
                doc_count=doc_count,
                mode=mode,
                feedbacks=feedbacks,
            )
            success, new_message_id = FeishuMessageBuilder.send(self.base_url, token, message_id, payload)

            # 注：record_id 映射由 main.py 的 _send_feishu_reply 统一处理，
            # 此处不再缓存 record_id（此方法为轮询兼容路径，飞书 webhook 不走）
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

    def process_card_action(self, event_data: dict) -> dict:
        """
        处理飞书卡片交互事件（按钮点击），返回卡片交互响应体

        Args:
            event_data: 卡片交互事件数据

        Returns:
            响应体 dict，由 webhook 返回给飞书。可能是：
            - toast 提示：{"toast": {"type": "success", "content": "..."}}
            - 表单卡片：{"card": {"type": "raw", "data": {...}}}
            - 空 dict {}（无需响应时）

        处理流程：
        1. 表单提交（action=submit_dislike_feedback）：解析文字 → 存反馈 → 返回 toast
        2. 点赞：反查 record_id → 存反馈 → 返回 toast
        3. 首次点踩：反查 record_id → 返回带输入框的表单卡片，引导用户填写文字反馈

        注意：返回 card 会替换掉用户点击的那张卡片（飞书回调更新机制）。
        """
        try:
            event = event_data.get("event", {})
            action = event.get("action", {})
            context = event.get("context", {})
            logger.info(f"📝 Card action: event={event}")

            # 提取关键信息
            action_value = action.get("value", {})
            feedback_type = action_value.get("feedback_type", "")

            # message_id 从事件上下文获取（被点击的消息 ID）
            message_id = context.get("open_message_id", "")
            user_id = event.get("operator", {}).get("open_id", "")
            chat_id = context.get("open_chat_id", "")

            # 通过飞书通讯录 API 获取反馈人昵称（无权限/失败时返回空串，不影响反馈保存）
            user_name = self.get_user_name(user_id)

            logger.info(f"📝 Card action: type={feedback_type}, message_id={message_id}, "
                       f"user={user_id}, user_name={user_name}, chat={chat_id}")

            from ai_assistant.core.feedback_manager import FeedbackManager
            feedback_mgr = FeedbackManager()

            # ===== 分支 0: 取消点踩表单 =====
            if action_value.get("action") == "cancel_dislike_feedback":
                record_id = action_value.get("record_id")
                if not record_id:
                    logger.warning("⚠️ cancel_dislike_feedback missing record_id")
                    return {"toast": {"type": "info", "content": "已取消"}}

                # 查询该 record_id 的对话历史和已有反馈，返回原始卡片
                from ai_assistant.core.chat_history import ChatHistoryManager
                from ai_assistant.utils.feishu_message import FeishuMessageBuilder

                chat_history_mgr = ChatHistoryManager()
                history_record = chat_history_mgr.get_record_by_id(record_id)

                if not history_record:
                    return {"toast": {"type": "error", "content": "无法找到原始消息"}}

                # 获取所有反馈
                feedbacks = feedback_mgr.get_feedbacks_by_record_id(record_id)

                # 提取元数据
                metadata = history_record.get("metadata", {})
                mode = metadata.get("mode")
                tool_rounds = metadata.get("tool_rounds")
                doc_count = metadata.get("doc_count")
                latency_ms = history_record.get("latency_ms", 0)
                elapsed_time = latency_ms / 1000.0 if latency_ms else None

                # 构建原始卡片（恢复到点踩之前的状态）
                original_card_payload = FeishuMessageBuilder.ai_reply_card(
                    reply_text=history_record.get("answer", ""),
                    message_id="placeholder",
                    elapsed_time=elapsed_time,
                    tool_rounds=tool_rounds,
                    doc_count=doc_count,
                    mode=mode,
                    feedbacks=feedbacks,
                )

                # 转换为卡片交互响应格式
                import json
                card_data = json.loads(original_card_payload["content"])
                original_card = {
                    "card": {
                        "type": "raw",
                        "data": card_data
                    }
                }

                # 返回原始卡片（飞书会用它替换表单卡片）
                logger.info(f"✅ Canceled dislike feedback for record_id={record_id}")
                return original_card

            # ===== 分支 1: 确认点踩（降级方案，不填写文字） =====
            if action_value.get("action") == "confirm_dislike_feedback":
                record_id = action_value.get("record_id")
                if not record_id:
                    logger.warning("⚠️ confirm_dislike_feedback missing record_id")
                    return {"toast": {"type": "error", "content": "暂无法提交反馈，请稍后再试"}}

                # 保存点踩反馈（不带文字）
                feedback_id = feedback_mgr.save_feedback(
                    record_id=record_id,
                    session_id=chat_id,
                    source="feishu",
                    feedback_type="dislike",
                    feedback_text="",  # 降级方案：不收集文字反馈
                    user_name=user_name,
                    user_id=user_id,
                )
                logger.info(f"✅ Dislike feedback confirmed (no text): {feedback_id}")

                # 查询该 record_id 的对话历史和所有反馈，返回更新后的完整卡片
                from ai_assistant.core.chat_history import ChatHistoryManager
                from ai_assistant.utils.feishu_message import FeishuMessageBuilder

                chat_history_mgr = ChatHistoryManager()
                history_record = chat_history_mgr.get_record_by_id(record_id)

                if not history_record:
                    return {"toast": {"type": "error", "content": "无法找到原始消息"}}

                # 获取所有反馈
                feedbacks = feedback_mgr.get_feedbacks_by_record_id(record_id)

                # 提取元数据
                metadata = history_record.get("metadata", {})
                mode = metadata.get("mode")
                tool_rounds = metadata.get("tool_rounds")
                doc_count = metadata.get("doc_count")
                latency_ms = history_record.get("latency_ms", 0)
                elapsed_time = latency_ms / 1000.0 if latency_ms else None

                # 构建更新后的卡片
                updated_card_payload = FeishuMessageBuilder.ai_reply_card(
                    reply_text=history_record.get("answer", ""),
                    message_id="placeholder",
                    elapsed_time=elapsed_time,
                    tool_rounds=tool_rounds,
                    doc_count=doc_count,
                    mode=mode,
                    feedbacks=feedbacks,
                )

                # 转换为卡片交互响应格式
                import json
                card_data = json.loads(updated_card_payload["content"])
                updated_card = {
                    "card": {
                        "type": "raw",
                        "data": card_data
                    }
                }

                # 返回更新后的卡片（飞书会用它替换确认卡片）
                return updated_card

            # ===== 分支 2: 点踩表单提交（保留，兼容公有云飞书） =====
            # record_id 由表单按钮 value 携带回来，不依赖 message_cache（更稳）
            if action_value.get("action") == "submit_dislike_feedback":
                record_id = action_value.get("record_id")
                if not record_id:
                    logger.warning("⚠️ submit_dislike_feedback missing record_id")
                    return {"toast": {"type": "error", "content": "暂无法提交反馈，请稍后再试"}}

                # 从表单解析用户填写的文字（飞书 form 提交事件的 form_value，键为 input 的 name）
                form_value = action.get("form_value", {}) or {}
                feedback_text = form_value.get("feedback_text", "") or ""

                feedback_id = feedback_mgr.save_feedback(
                    record_id=record_id,
                    session_id=chat_id,
                    source="feishu",
                    feedback_type="dislike",
                    feedback_text=feedback_text,
                    user_name=user_name,
                    user_id=user_id,
                )
                logger.info(f"✅ Dislike feedback with text saved: {feedback_id}, "
                            f"text_len={len(feedback_text)}")

                # 查询该 record_id 的对话历史和所有反馈，返回更新后的完整卡片
                from ai_assistant.core.chat_history import ChatHistoryManager
                from ai_assistant.utils.feishu_message import FeishuMessageBuilder

                chat_history_mgr = ChatHistoryManager()
                history_record = chat_history_mgr.get_record_by_id(record_id)

                if not history_record:
                    return {"toast": {"type": "error", "content": "无法找到原始消息"}}

                # 获取所有反馈
                feedbacks = feedback_mgr.get_feedbacks_by_record_id(record_id)

                # 提取元数据
                metadata = history_record.get("metadata", {})
                mode = metadata.get("mode")
                tool_rounds = metadata.get("tool_rounds")
                doc_count = metadata.get("doc_count")
                latency_ms = history_record.get("latency_ms", 0)
                elapsed_time = latency_ms / 1000.0 if latency_ms else None

                # 构建更新后的卡片
                updated_card = FeishuMessageBuilder.ai_reply_card(
                    reply_text=history_record.get("answer", ""),
                    message_id="placeholder",
                    elapsed_time=elapsed_time,
                    tool_rounds=tool_rounds,
                    doc_count=doc_count,
                    mode=mode,
                    feedbacks=feedbacks,
                )

                 # 转换为卡片交互响应格式
                import json
                card_data = json.loads(updated_card["content"])
                updated_card = {
                    "card": {
                        "type": "raw",
                        "data": card_data
                    }
                }

                # 返回更新后的卡片（飞书会用它替换确认卡片）
                return updated_card

            # ===== 分支 4: 首次点赞 / 点踩 =====
            if not feedback_type or not message_id:
                logger.warning("Missing feedback_type or message_id in card action")
                return {}

            # 用飞书 message_id 反查对话历史 record_id
            with self._cache_lock:
                record_id = self.message_cache.get(message_id)

            if not record_id:
                logger.warning(f"⚠️ record_id not found in cache for message_id={message_id}")
                return {"toast": {"type": "error", "content": "暂无法提交反馈，请稍后再试"}}

            logger.info(f"✅ Found record_id={record_id} for message_id={message_id}")

            # ===== 分支 2: 点赞 =====
            if feedback_type == "like":
                # 点赞：保存反馈 + 返回更新后的完整卡片（带反馈信息）
                feedback_id = feedback_mgr.save_feedback(
                    record_id=record_id,
                    session_id=chat_id,
                    source="feishu",
                    feedback_type="like",
                    user_name=user_name,
                    user_id=user_id,
                )
                logger.info(f"✅ Like feedback saved: {feedback_id}")

                # 查询该 record_id 的对话历史和所有反馈
                from ai_assistant.core.chat_history import ChatHistoryManager
                from ai_assistant.utils.feishu_message import FeishuMessageBuilder

                chat_history_mgr = ChatHistoryManager()
                history_record = chat_history_mgr.get_record_by_id(record_id)

                if not history_record:
                    return {"toast": {"type": "error", "content": "无法找到原始消息"}}

                # 获取所有反馈
                feedbacks = feedback_mgr.get_feedbacks_by_record_id(record_id)

                # 提取元数据
                metadata = history_record.get("metadata", {})
                mode = metadata.get("mode")
                tool_rounds = metadata.get("tool_rounds")
                doc_count = metadata.get("doc_count")
                latency_ms = history_record.get("latency_ms", 0)
                elapsed_time = latency_ms / 1000.0 if latency_ms else None

                # 构建更新后的卡片
                updated_card_payload = FeishuMessageBuilder.ai_reply_card(
                    reply_text=history_record.get("answer", ""),
                    message_id="placeholder",
                    elapsed_time=elapsed_time,
                    tool_rounds=tool_rounds,
                    doc_count=doc_count,
                    mode=mode,
                    feedbacks=feedbacks,
                )

                # 将 payload 转换为卡片交互响应格式
                # ai_reply_card 返回: {"msg_type": "interactive", "content": json.dumps(card)}
                # 卡片交互需要: {"card": {"type": "raw", "data": card}}
                import json
                card_data = json.loads(updated_card_payload["content"])
                updated_card = {
                    "card": {
                        "type": "raw",
                        "data": card_data
                    }
                }

                # 返回更新后的卡片（飞书会用它替换原卡片）
                return updated_card

            # ===== 分支 3: 点踩 =====
            elif feedback_type == "dislike":
                # 首次点踩：返回表单卡片，引导用户填写文字反馈
                # record_id 塞进表单按钮 value，提交时带回来
                logger.info(f"📝 Returning dislike feedback form for record_id={record_id}")
                return self._build_dislike_feedback_form(record_id)

            return {}

        except Exception as e:
            logger.error(f"Error processing card action: {e}", exc_info=True)
            return {}

    def _build_dislike_feedback_form(self, record_id: str) -> dict:
        """
        构建点踩后的文字反馈表单卡片响应体

        Args:
            record_id: 关联的对话历史记录主键，塞进提交按钮 value 携带回来

        Returns:
            飞书卡片交互响应体（降级方案：不使用 form，改用普通按钮）

        注意：私有化飞书可能不支持 form/input 组件，改用简单的按钮方案：
        - 点踩后直接保存（不填写文字）
        - 提供「确认」和「取消」按钮
        """
        return {
    "card": {
        "type": "raw",
        "data": {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": "orange",
                "title": {
                    "tag": "plain_text",
                    "content": "📝 请告诉我们哪里不准确"
                }
            },
            "body": {
                "direction": "vertical",
                "elements": [
                    {
                        "tag": "form",
                        "name": "dislike_feedback_form",
                        "elements": [
                            {
                                "tag": "input",
                                "name": "feedback_text",
                                "required": False,
                                "placeholder": {
                                    "tag": "plain_text",
                                    "content": "请描述不准确的地方（选填，直接点提交也可）"
                                },
                                "max_length": 500
                            },
                            {
                                "tag": "column_set",
                                "flex_mode": "none",
                                "horizontal_spacing": "medium",
                                "columns": [
                                    {
                                        "tag": "column",
                                        "width": "auto",
                                        "vertical_align": "top",
                                        "elements": [
                                            {
                                                "tag": "button",
                                                "text": {
                                                    "tag": "plain_text",
                                                    "content": "✅ 确认"
                                                },
                                                "type": "primary",
                                                "action_type": "form_submit",
                                                "name": "submit_dislike_feedback",
                                                "value": {
                                                    "feedback_type": "dislike",
                                                    "action": "submit_dislike_feedback",
                                                    "record_id": str(record_id)
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        "tag": "column",
                                        "width": "auto",
                                        "vertical_align": "top",
                                        "elements": [
                                            {
                                                "tag": "button",
                                                "text": {
                                                    "tag": "plain_text",
                                                    "content": "❌ 取消"
                                                },
                                                "type": "default",
                                                "action_type": "form_submit",
                                                "name": "cancel_dislike_feedback",
                                                "value": {
                                                    "action": "cancel_dislike_feedback",
                                                    "record_id": str(record_id)
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }
    }
}

    def _send_ephemeral_message(self, user_id: str, chat_id: str, text: str) -> bool:
        """
        发送临时消息（ephemeral message，仅发送者可见）

        Args:
            user_id: 用户 open_id
            chat_id: 会话 ID
            text: 消息文本

        Returns:
            是否成功
        """
        try:
            token = self.get_tenant_access_token()
            url = f"{self.base_url}/open-apis/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            params = {"receive_id_type": "open_id"}
            payload = {
                "receive_id": user_id,
                "msg_type": "text",
                "content": json.dumps({"text": text})
            }

            logger.debug(f"Sending ephemeral message to user={user_id}")
            response = requests.post(url, headers=headers, json=payload, params=params, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("code") == 0:
                logger.info(f"Ephemeral message sent successfully")
                return True
            else:
                logger.warning(f"Failed to send ephemeral message: {result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"Error sending ephemeral message: {e}")
            return False

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

