from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Union


@dataclass
class Content:
    """消息内容（支持多模态）"""
    # log_file 的 data 为 dict：{"name","path","text","note"}
    type: str  # "text" | "image" | "video" | "log_file"
    data: Union[str, bytes]

    def summary(self) -> str:
        """
        日志用摘要：不输出图片 base64 本体（动辄几十~上百 KB，会刷爆日志），
        图片只打类型+大小，文本只打长度，日志文件只打文件名与正文长度。
        """
        if self.type == "log_file" and isinstance(self.data, dict):
            name = self.data.get("name", "?")
            text_len = len(self.data.get("text") or "")
            return f"log_file({name}, text_len={text_len})"
        if self.type == "image" and isinstance(self.data, dict):
            b64 = self.data.get("data", "")
            media = self.data.get("media_type", "?")
            # base64 长度 * 3/4 约等于原始字节数
            approx = int(len(b64) * 3 / 4)
            path = self.data.get("path") or self.data.get("file_path")
            path_part = f", path={path}" if path else ""
            return f"image({media}, ~{approx}B{path_part})"
        if self.type == "image":
            return f"image(BAD:{type(self.data).__name__})"
        if isinstance(self.data, str):
            return f"text(len={len(self.data)})"
        return f"{self.type}({type(self.data).__name__})"


@dataclass
class Message:
    """单条消息"""
    role: str  # "user" | "assistant"
    content: List[Content]
    timestamp: datetime
    metadata: dict = field(default_factory=dict)  # 存储模式信息等元数据（如 mode: "agentic"）

    def summary(self) -> str:
        """日志用摘要：role + 各 content 的脱敏摘要（不含图片 base64）。"""
        return f"{self.role}[{', '.join(c.summary() for c in self.content)}]"


@dataclass
class Session:
    """会话上下文"""
    session_id: str
    messages: List[Message] = field(default_factory=list)
    context_mode: str = "short"
    max_messages: int = 10
    last_active: datetime = field(default_factory=datetime.now)

    def add_message(self, message: Message) -> None:
        """添加消息到会话，自动维护最大消息数限制"""
        self.messages.append(message)
        self.last_active = datetime.now()

        # 保持消息数量在限制内
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]

    def get_context_messages(self) -> List[Message]:
        """获取上下文消息"""
        return self.messages.copy()
