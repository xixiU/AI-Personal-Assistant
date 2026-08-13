"""
反馈数据管理器

按天保存用户对 AI 回答的反馈数据，支持查询指定 session 的负反馈记录。
用于在同 session 的后续对话中通过 Prompt 注入方式改进 AI 回答质量。
"""

import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger


class FeedbackManager:
    """用户反馈数据持久化管理"""

    def __init__(self, feedback_dir: str = "./data/feedback"):
        """
        初始化反馈管理器

        Args:
            feedback_dir: 反馈数据保存目录
        """
        self.feedback_dir = Path(feedback_dir)
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        logger.info(f"反馈管理器初始化: {self.feedback_dir}")

    def save_feedback(
        self,
        record_id: str,
        session_id: str,
        source: str,
        feedback_type: str,
        feedback_text: Optional[str] = None,
    ) -> str:
        """
        保存用户反馈记录

        Args:
            record_id: 关联的 chat_history 记录主键
            session_id: 会话 ID（飞书 chat_id / web session / 微信窗口名）
            source: 反馈来源（"feishu" / "web" / "wechat"）
            feedback_type: 反馈类型（"like" / "dislike"）
            feedback_text: 用户填写的反馈内容（可选，点踩时填写）

        Returns:
            feedback_id: 反馈记录的唯一标识（UUID）

        Raises:
            ValueError: 参数验证失败
        """
        # 参数验证
        if feedback_type not in ["like", "dislike"]:
            raise ValueError(f"无效的反馈类型: {feedback_type}，仅支持 'like' 或 'dislike'")

        if not record_id or not session_id:
            raise ValueError("record_id 和 session_id 不能为空")

        # 生成唯一 ID
        feedback_id = str(uuid.uuid4())

        # 构建反馈记录
        record = {
            "feedback_id": feedback_id,
            "record_id": record_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "session_id": session_id,
            "source": source,
            "feedback_type": feedback_type,
            "feedback_text": feedback_text or "",
        }

        # 按天保存到 JSONL 文件
        today = datetime.now().strftime("%Y-%m-%d")
        file_path = self.feedback_dir / f"{today}.jsonl"

        with self._lock:
            try:
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                logger.info(
                    f"保存反馈成功: feedback_id={feedback_id}, "
                    f"type={feedback_type}, session={session_id}, "
                    f"record_id={record_id}"
                )
            except Exception as e:
                logger.error(f"保存反馈失败: {e}", exc_info=True)
                raise

        return feedback_id

    def get_session_negative_feedbacks(
        self, session_id: str, limit: int = 3
    ) -> List[Dict]:
        """
        查询指定 session 的负反馈记录（用于 Prompt 注入）

        Args:
            session_id: 会话 ID
            limit: 返回记录数上限（默认 3 条，按时间倒序）

        Returns:
            负反馈记录列表，每条包含：
            - feedback_id
            - record_id（关联的 chat_history 主键）
            - timestamp
            - feedback_text
        """
        if not session_id:
            logger.warning("session_id 为空，返回空列表")
            return []

        feedbacks = []

        # 遍历最近几天的反馈文件（从今天往前找，最多查 7 天）
        today = datetime.now()
        for days_back in range(7):
            target_date = today - timedelta(days=days_back)
            date_str = target_date.strftime("%Y-%m-%d")
            file_path = self.feedback_dir / f"{date_str}.jsonl"

            if not file_path.exists():
                continue

            # 读取该天的所有反馈记录
            with self._lock:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for line in f:
                            record = json.loads(line.strip())
                            # 筛选：同 session + 负反馈
                            if (
                                record.get("session_id") == session_id
                                and record.get("feedback_type") == "dislike"
                            ):
                                feedbacks.append(record)
                except Exception as e:
                    logger.error(f"读取反馈文件失败: {file_path}, {e}")
                    continue

            # 如果已经找到足够的记录，提前退出
            if len(feedbacks) >= limit:
                break

        # 按时间戳倒序排序，取最近的 N 条
        feedbacks.sort(key=lambda x: x["timestamp"], reverse=True)
        result = feedbacks[:limit]

        logger.debug(
            f"查询 session={session_id} 的负反馈: 找到 {len(result)} 条记录"
        )
        return result

    def get_all_feedbacks(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None
    ) -> List[Dict]:
        """
        查询指定日期范围内的所有反馈记录（用于统计分析）

        Args:
            start_date: 开始日期（格式 "YYYY-MM-DD"），默认为 7 天前
            end_date: 结束日期（格式 "YYYY-MM-DD"），默认为今天

        Returns:
            反馈记录列表
        """
        if not end_date:
            end_date = datetime.now().strftime("%Y-%m-%d")
        if not start_date:
            start_dt = datetime.now() - timedelta(days=7)
            start_date = start_dt.strftime("%Y-%m-%d")

        feedbacks = []
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        # 遍历日期范围内的所有文件
        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            file_path = self.feedback_dir / f"{date_str}.jsonl"

            if file_path.exists():
                with self._lock:
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            for line in f:
                                record = json.loads(line.strip())
                                feedbacks.append(record)
                    except Exception as e:
                        logger.error(f"读取反馈文件失败: {file_path}, {e}")

            current += timedelta(days=1)

        logger.debug(f"查询日期范围 {start_date} ~ {end_date}: 共 {len(feedbacks)} 条记录")
        return feedbacks
