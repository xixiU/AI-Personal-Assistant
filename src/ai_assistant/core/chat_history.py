"""
用户提问历史持久化

按天保存所有用户提问和 AI 回复到 JSONL 文件，用于后续分析和产品优化。
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from loguru import logger


class ChatHistoryManager:
    """用户提问历史持久化管理"""

    def __init__(self, history_dir: str = "./data/chat_history"):
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        logger.info(f"对话历史管理器初始化: {self.history_dir}")

    def save(
        self,
        session_id: str,
        query: str,
        answer: str,
        latency_ms: Optional[int] = None,
    ):
        """
        保存一条对话记录

        Args:
            session_id: 会话 ID（飞书 chat_id / web session / 微信窗口名）
            query: 用户提问
            answer: AI 回复
            latency_ms: 响应耗时（毫秒）
        """
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": session_id,
            "query": query,
            "answer": answer,
            "latency_ms": latency_ms,
        }

        today = datetime.now().strftime("%Y-%m-%d")
        file_path = self.history_dir / f"{today}.jsonl"

        with self._lock:
            try:
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"保存对话历史失败: {e}")
