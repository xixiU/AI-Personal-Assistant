"""
微信适配器 - 支持微信 3.9 和 4.1 版本

使用 UI 自动化方式操作微信客户端，无需协议破解，封号风险低。
- 微信 3.9: 使用 pywechat 模块
- 微信 4.1: 使用 pyweixin 模块
"""

import time
from typing import List, Optional, Dict, Any
from datetime import datetime
from loguru import logger

from ai_assistant.adapters.base import IMAdapter
from ai_assistant.core.models import Message, Content

# 尝试导入两个版本的库
PYWEIXIN_AVAILABLE = False
PYWECHAT_AVAILABLE = False

try:
    from pyweixin import Monitor, Navigator, Messages
    PYWEIXIN_AVAILABLE = True
    logger.info("pyweixin (微信 4.1+) 已加载")
except ImportError:
    pass

try:
    import pywechat.WechatAuto as wx_auto
    PYWECHAT_AVAILABLE = True
    logger.info("pywechat (微信 3.9+) 已加载")
except ImportError:
    pass

if not PYWEIXIN_AVAILABLE and not PYWECHAT_AVAILABLE:
    logger.warning("未安装 pywechat 库")
    logger.warning("微信 4.1+: pip install git+https://github.com/Hello-Mr-Crab/pywechat.git")
    logger.warning("微信 3.9+: pip install pywechat127==1.9.7")


class WeChatAdapter(IMAdapter):
    """微信适配器 - 支持 3.9 和 4.1 版本"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化微信适配器

        Args:
            config: 适配器配置
        """
        self.config = config
        self.wechat_version = config.get("wechat_version", "4.1")
        self.poll_interval = config.get("poll_interval", 1.0)
        self.monitored_chats = config.get("monitored_chats", [])

        # 根据版本选择实现
        if self.wechat_version == "3.9":
            if not PYWECHAT_AVAILABLE:
                raise ImportError("pywechat 未安装。请运行: pip install pywechat127==1.9.7")
            self.impl = WeChat39Adapter(config)
            logger.info("使用微信 3.9 适配器")
        else:
            if not PYWEIXIN_AVAILABLE:
                raise ImportError("pyweixin 未安装。请运行: pip install git+https://github.com/Hello-Mr-Crab/pywechat.git")
            self.impl = WeChat41Adapter(config)
            logger.info("使用微信 4.1 适配器")

    def detect_active_window(self) -> bool:
        return self.impl.detect_active_window()

    def extract_messages(self, count: int = 10) -> List[Message]:
        return self.impl.extract_messages(count)

    def check_trigger(self, keyword: str) -> bool:
        return self.impl.check_trigger(keyword)

    def get_session_id(self) -> Optional[str]:
        return self.impl.get_session_id()

    def send_message(self, message: str, chat_name: Optional[str] = None) -> bool:
        return self.impl.send_message(message, chat_name)

    def get_last_message_as_message(self) -> Optional[Message]:
        return self.impl.get_last_message_as_message()


class WeChat39Adapter:
    """微信 3.9 版本适配器实现"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.poll_interval = config.get("poll_interval", 1.0)
        self.monitored_chats = config.get("monitored_chats", [])

        self.processed_messages = set()
        self.last_message = None
        self.current_chat_name = None

        logger.info("微信 3.9 适配器初始化成功")

    def detect_active_window(self) -> bool:
        """检测微信窗口"""
        try:
            # pywechat 会自动检测微信窗口
            return True
        except Exception as e:
            logger.debug(f"微信窗口检测失败: {e}")
            return False

    def extract_messages(self, count: int = 10) -> List[Message]:
        """提取消息"""
        messages = []
        if self.last_message:
            msg = Message(
                role="user",
                content=[Content(type="text", data=self.last_message)],
                timestamp=datetime.now()
            )
            messages.append(msg)
        logger.info(f"获取到消息: {msg}")
        return messages

    def check_trigger(self, keyword: str) -> bool:
        """检查触发关键词"""
        try:
            if not self.monitored_chats:
                return self._check_all_chats(keyword)

            for chat_name in self.monitored_chats:
                if self._check_chat(chat_name, keyword):
                    return True

            return False

        except Exception as e:
            logger.error(f"检查触发失败: {e}")
            return False

    def _check_all_chats(self, keyword: str) -> bool:
        """检查所有聊天（使用 check_new_message）"""
        try:
            # 使用 check_new_message 检查新消息
            new_msgs = wx_auto.check_new_message()
            logger.info(f"检查所有聊天: {new_msgs}")

            if not new_msgs:
                return False

            # 检查是否有包含关键词的消息
            # new_msgs 格式: [{'好友名称': 'xxx', '消息内容': ['msg1', 'msg2'], ...}]
            for msg_info in new_msgs:
                if not isinstance(msg_info, dict):
                    continue

                who = msg_info.get('好友名称', 'Unknown')
                msg_contents = msg_info.get('消息内容', [])

                # 检查每条消息内容
                for msg_text in msg_contents:
                    if keyword in str(msg_text):
                        # 避免重复处理
                        msg_key = f"{who}:{msg_text}"
                        if msg_key not in self.processed_messages:
                            logger.info(f"检测到触发关键词: {keyword} (来自 {who})")
                            self.last_message = str(msg_text)
                            self.current_chat_name = who
                            self.processed_messages.add(msg_key)

                            if len(self.processed_messages) > 100:
                                self.processed_messages.clear()

                            return True

            return False

        except Exception as e:
            logger.error(f"检查所有聊天失败: {e}")
            return False

    def _check_chat(self, chat_name: str, keyword: str) -> bool:
        """检查指定聊天"""
        try:
            # 使用 listen_on_chat 监听指定聊天
            result = wx_auto.listen_on_chat(
                friend=chat_name,
                duration=f"{int(self.poll_interval)}s"
            )

            if not result:
                return False

            # result 是消息列表
            for msg_text in result:
                if keyword in str(msg_text):
                    msg_key = f"{chat_name}:{msg_text}"
                    if msg_key not in self.processed_messages:
                        logger.info(f"在 {chat_name} 中检测到触发: {keyword}")
                        self.last_message = str(msg_text)
                        self.current_chat_name = chat_name
                        self.processed_messages.add(msg_key)

                        if len(self.processed_messages) > 100:
                            self.processed_messages.clear()

                        return True

            return False

        except Exception as e:
            logger.error(f"检查聊天 {chat_name} 失败: {e}")
            return False

    def get_session_id(self) -> Optional[str]:
        """获取会话ID"""
        return self.current_chat_name

    def send_message(self, message: str, chat_name: Optional[str] = None) -> bool:
        """发送消息"""
        try:
            target = chat_name or self.current_chat_name

            if not target:
                logger.error("未指定目标聊天")
                return False

            # 使用 send_message_to_friend 发送消息
            # is_maximize=False: 不最大化窗口，避免影响用户
            # close_wechat=False: 不关闭微信窗口
            wx_auto.send_message_to_friend(
                friend=target,
                message=message,
                is_maximize=False,
                close_wechat=False
            )
            logger.info(f"消息已发送到 {target}")
            return True

        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    def get_last_message_as_message(self) -> Optional[Message]:
        """获取最后一条消息"""
        if not self.last_message:
            return None

        return Message(
            role="user",
            content=[Content(type="text", data=self.last_message)],
            timestamp=datetime.now()
        )


class WeChat41Adapter:
    """微信 4.1 版本适配器实现（保持原有实现）"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.poll_interval = config.get("poll_interval", 1.0)
        self.monitored_chats = config.get("monitored_chats", [])

        self.processed_messages = set()
        self.last_message = None
        self.current_dialog = None
        self.current_chat_name = None

    def detect_active_window(self) -> bool:
        try:
            return True
        except Exception as e:
            logger.debug(f"微信窗口检测失败: {e}")
            return False

    def extract_messages(self, count: int = 10) -> List[Message]:
        messages = []
        if self.last_message:
            msg = Message(
                role="user",
                content=[Content(type="text", data=self.last_message)],
                timestamp=datetime.now()
            )
            messages.append(msg)
        return messages

    def check_trigger(self, keyword: str) -> bool:
        try:
            if not self.monitored_chats:
                return self._check_all_chats(keyword)

            for chat_name in self.monitored_chats:
                if self._check_chat(chat_name, keyword):
                    return True

            return False

        except Exception as e:
            logger.error(f"检查触发失败: {e}")
            return False

    def _check_all_chats(self, keyword: str) -> bool:
        try:
            import pyperclip
            clipboard_text = pyperclip.paste()

            if clipboard_text and keyword in clipboard_text:
                if clipboard_text not in self.processed_messages:
                    logger.info(f"检测到触发关键词: {keyword}")
                    self.last_message = clipboard_text
                    self.processed_messages.add(clipboard_text)

                    if len(self.processed_messages) > 100:
                        self.processed_messages.clear()

                    return True

            return False

        except Exception as e:
            logger.error(f"检查剪贴板失败: {e}")
            return False

    def _check_chat(self, chat_name: str, keyword: str) -> bool:
        try:
            dialog = Navigator.open_seperate_dialog_window(friend=chat_name)

            result = Monitor.listen_on_chat(
                dialog_window=dialog,
                duration=f"{int(self.poll_interval)}s"
            )

            if result and isinstance(result, list):
                for msg_data in result:
                    msg_text = str(msg_data)

                    if keyword in msg_text:
                        if msg_text not in self.processed_messages:
                            logger.info(f"在 {chat_name} 中检测到触发: {keyword}")
                            self.last_message = msg_text
                            self.current_dialog = dialog
                            self.current_chat_name = chat_name
                            self.processed_messages.add(msg_text)

                            if len(self.processed_messages) > 100:
                                self.processed_messages.clear()

                            return True

            return False

        except Exception as e:
            logger.error(f"检查聊天 {chat_name} 失败: {e}")
            return False

    def get_session_id(self) -> Optional[str]:
        return self.current_chat_name

    def send_message(self, message: str, chat_name: Optional[str] = None) -> bool:
        try:
            target = chat_name or self.current_chat_name

            if not target:
                logger.error("未指定目标聊天")
                return False

            Messages.send_text(friend=target, text=message)
            logger.info(f"消息已发送到 {target}")
            return True

        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    def get_last_message_as_message(self) -> Optional[Message]:
        if not self.last_message:
            return None

        return Message(
            role="user",
            content=[Content(type="text", data=self.last_message)],
            timestamp=datetime.now()
        )
