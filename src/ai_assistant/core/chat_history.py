"""
用户提问历史持久化

按天保存所有用户提问和 AI 回复到 JSONL 文件，用于后续分析和产品优化。
"""

import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
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
        source: str = "unknown",
        metadata: Optional[dict] = None,
        context_key: str = "",
        chain_info: Optional[dict] = None,
    ) -> str:
        """
        保存一条对话记录

        Args:
            session_id: 会话 ID（飞书 chat_id / web session / 微信窗口名），归档/聚合维度
            query: 用户提问
            answer: AI 回复
            latency_ms: 响应耗时（毫秒）
            source: 提问来源（"feishu", "wechat", "web"）
            metadata: 附加元数据（如 mode、tool_rounds、doc_count 等）
            context_key: 引用链标识（飞书 root_id 或首条 message_id），用于按引用链
                重建完整历史。同一条引用链的所有轮共享同一 context_key。
            chain_info: 引用链排查字段（message_id / parent_id / root_id），仅用于
                核对飞书 root_id 行为，不参与检索。

        Returns:
            record_id: 本条记录的唯一标识
        """
        record_id = str(uuid.uuid4())
        record = {
            "record_id": record_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": session_id,
            "source": source,
            "query": query,
            "answer": answer,
            "latency_ms": latency_ms,
        }

        # 引用链标识（路径 B：按引用链重建完整历史的检索键）
        if context_key:
            record["context_key"] = context_key
        # 引用链排查字段（便于核对飞书 root_id 行为）
        if chain_info:
            record["chain_info"] = chain_info

        # 添加 metadata（如果有）
        if metadata:
            record["metadata"] = metadata

        today = datetime.now().strftime("%Y-%m-%d")
        file_path = self.history_dir / f"{today}.jsonl"

        with self._lock:
            try:
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"保存对话历史失败: {e}")

        return record_id

    def get_record_by_id(self, record_id: str) -> Optional[dict]:
        """
        根据 record_id 查询对话记录

        Args:
            record_id: 记录唯一标识

        Returns:
            找到的记录字典，未找到返回 None
        """
        with self._lock:
            # 从今天往前遍历最多 7 天
            for days_ago in range(7):
                date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
                file_path = self.history_dir / f"{date}.jsonl"

                if not file_path.exists():
                    continue

                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                record = json.loads(line.strip())
                                if record.get("record_id") == record_id:
                                    return record
                            except json.JSONDecodeError:
                                # 跳过损坏的行
                                continue
                except Exception as e:
                    logger.warning(f"读取历史文件 {file_path} 失败: {e}")
                    continue

        return None

    def get_records_by_ids(self, record_ids: List[str]) -> Dict[str, dict]:
        """
        批量查询对话记录

        Args:
            record_ids: 记录 ID 列表

        Returns:
            {record_id: record} 字典，未找到的 ID 不在结果中
        """
        if not record_ids:
            return {}

        result = {}
        remaining_ids = set(record_ids)

        with self._lock:
            # 从今天往前遍历最多 7 天
            for days_ago in range(7):
                if not remaining_ids:
                    break

                date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
                file_path = self.history_dir / f"{date}.jsonl"

                if not file_path.exists():
                    continue

                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                record = json.loads(line.strip())
                                rid = record.get("record_id")
                                if rid and rid in remaining_ids:
                                    result[rid] = record
                                    remaining_ids.remove(rid)
                            except json.JSONDecodeError:
                                # 跳过损坏的行
                                continue
                except Exception as e:
                    logger.warning(f"读取历史文件 {file_path} 失败: {e}")
                    continue

        return result

    def get_records_by_context_key(
        self, context_key: str, days: int = 7, max_records: int = 50
    ) -> List[dict]:
        """
        按引用链标识 context_key 捞出该链的全部对话记录（路径 B 的核心）。

        用于"用户引用某条消息追问"时，从持久化历史里重建整条引用链的完整问答，
        不受内存上下文窗口(max_messages)、会话过期(session_timeout)、进程重启影响。

        Args:
            context_key: 引用链标识（飞书 root_id 或链首 message_id）
            days: 向前回溯的天数（默认 7 天，覆盖 jsonl 按天归档）
            max_records: 单链最多返回条数上限（防御异常长链，默认 50 轮）

        Returns:
            该链的记录列表，按 timestamp 升序（最早的问答在前）。未命中返回 []。
        """
        if not context_key:
            return []

        matched: List[dict] = []
        with self._lock:
            # 从今天往前遍历；同一条链可能跨天，需合并多个 jsonl
            for days_ago in range(days):
                date = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
                file_path = self.history_dir / f"{date}.jsonl"

                if not file_path.exists():
                    continue

                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                record = json.loads(line.strip())
                                if record.get("context_key") == context_key:
                                    matched.append(record)
                            except json.JSONDecodeError:
                                continue
                except Exception as e:
                    logger.warning(f"读取历史文件 {file_path} 失败: {e}")
                    continue

        # 按 timestamp 升序（最早在前）；timestamp 缺失的排最后
        matched.sort(key=lambda r: r.get("timestamp", "9999"))

        # 防御异常长链：只保留最近 max_records 轮
        if len(matched) > max_records:
            matched = matched[-max_records:]

        return matched
