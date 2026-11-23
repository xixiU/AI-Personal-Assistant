"""
反馈数据库查询模块

提供对 feedback 表的查询、统计功能
支持分页、过滤、排序等操作
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy import func, desc
from loguru import logger

from ai_assistant.core.database import DatabaseManager
from ai_assistant.models.feedback_db import FeedbackDB, FeedbackType


class FeedbackDBQuery:
    """反馈数据库查询类"""

    def __init__(self, db_manager: DatabaseManager):
        """
        初始化查询类

        Args:
            db_manager: 数据库管理器
        """
        self.db_manager = db_manager
        logger.info("反馈数据库查询模块初始化完成")

    def query(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        feedback_type: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """
        查询反馈记录

        Args:
            user_id: 用户 ID 过滤
            session_id: 会话 ID 过滤
            feedback_type: 反馈类型过滤（like/dislike）
            start_time: 开始时间（ISO 8601 格式）
            end_time: 结束时间（ISO 8601 格式）
            source: 来源过滤（feishu/web/wechat）
            limit: 返回记录数上限
            offset: 偏移量（分页用）

        Returns:
            反馈记录列表
        """
        try:
            with self.db_manager.get_session() as session:
                # 构建查询
                query_obj = session.query(FeedbackDB)

                # 应用过滤条件
                if user_id:
                    query_obj = query_obj.filter(FeedbackDB.user_id == user_id)
                if session_id:
                    query_obj = query_obj.filter(FeedbackDB.session_id == session_id)
                if source:
                    query_obj = query_obj.filter(FeedbackDB.source == source)

                # 反馈类型过滤
                if feedback_type:
                    if feedback_type == 'like':
                        query_obj = query_obj.filter(FeedbackDB.feedback_type == FeedbackType.LIKE)
                    elif feedback_type == 'dislike':
                        query_obj = query_obj.filter(FeedbackDB.feedback_type == FeedbackType.DISLIKE)

                # 时间范围过滤
                if start_time:
                    try:
                        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(FeedbackDB.timestamp >= start_dt)
                    except ValueError:
                        logger.warning(f"无效的 start_time 格式: {start_time}")

                if end_time:
                    try:
                        end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(FeedbackDB.timestamp <= end_dt)
                    except ValueError:
                        logger.warning(f"无效的 end_time 格式: {end_time}")

                # 按时间倒序排序（最新的在前）
                query_obj = query_obj.order_by(desc(FeedbackDB.timestamp))

                # 分页
                query_obj = query_obj.limit(limit).offset(offset)

                # 执行查询
                records = query_obj.all()

                # 转换为字典列表
                result = [record.to_dict() for record in records]
                return result

        except Exception as e:
            logger.error(f"查询反馈记录失败: {e}", exc_info=True)
            return []

    def count(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        feedback_type: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        source: Optional[str] = None,
    ) -> int:
        """
        统计符合条件的记录数

        Args:
            user_id: 用户 ID 过滤
            session_id: 会话 ID 过滤
            feedback_type: 反馈类型过滤
            start_time: 开始时间
            end_time: 结束时间
            source: 来源过滤

        Returns:
            记录总数
        """
        try:
            with self.db_manager.get_session() as session:
                # 构建查询
                query_obj = session.query(func.count(FeedbackDB.feedback_id))

                # 应用过滤条件（与 query 方法相同）
                if user_id:
                    query_obj = query_obj.filter(FeedbackDB.user_id == user_id)
                if session_id:
                    query_obj = query_obj.filter(FeedbackDB.session_id == session_id)
                if source:
                    query_obj = query_obj.filter(FeedbackDB.source == source)

                if feedback_type:
                    if feedback_type == 'like':
                        query_obj = query_obj.filter(FeedbackDB.feedback_type == FeedbackType.LIKE)
                    elif feedback_type == 'dislike':
                        query_obj = query_obj.filter(FeedbackDB.feedback_type == FeedbackType.DISLIKE)

                if start_time:
                    try:
                        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(FeedbackDB.timestamp >= start_dt)
                    except ValueError:
                        pass

                if end_time:
                    try:
                        end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(FeedbackDB.timestamp <= end_dt)
                    except ValueError:
                        pass

                # 执行统计
                total = query_obj.scalar()
                return total or 0

        except Exception as e:
            logger.error(f"统计反馈记录失败: {e}", exc_info=True)
            return 0

    def get_stats(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Dict:
        """
        获取反馈统计信息

        Args:
            user_id: 用户 ID 过滤
            session_id: 会话 ID 过滤
            start_time: 开始时间
            end_time: 结束时间
            source: 来源过滤

        Returns:
            统计信息，格式: {"like_count": 30, "dislike_count": 20, "like_rate": 0.6}
        """
        try:
            with self.db_manager.get_session() as session:
                # 构建基础查询
                base_query = session.query(FeedbackDB)

                # 应用过滤条件
                if user_id:
                    base_query = base_query.filter(FeedbackDB.user_id == user_id)
                if session_id:
                    base_query = base_query.filter(FeedbackDB.session_id == session_id)
                if source:
                    base_query = base_query.filter(FeedbackDB.source == source)

                if start_time:
                    try:
                        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                        base_query = base_query.filter(FeedbackDB.timestamp >= start_dt)
                    except ValueError:
                        pass

                if end_time:
                    try:
                        end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                        base_query = base_query.filter(FeedbackDB.timestamp <= end_dt)
                    except ValueError:
                        pass

                # 统计点赞数
                like_count = base_query.filter(FeedbackDB.feedback_type == FeedbackType.LIKE).count()

                # 统计点踩数
                dislike_count = base_query.filter(FeedbackDB.feedback_type == FeedbackType.DISLIKE).count()

                # 计算点赞率
                total = like_count + dislike_count
                like_rate = like_count / total if total > 0 else 0.0

                return {
                    "like_count": like_count,
                    "dislike_count": dislike_count,
                    "like_rate": round(like_rate, 2)
                }

        except Exception as e:
            logger.error(f"获取反馈统计失败: {e}", exc_info=True)
            return {
                "like_count": 0,
                "dislike_count": 0,
                "like_rate": 0.0
            }

    def get_feedback_types(self, record_ids: List[str]) -> Dict[str, Optional[str]]:
        """
        批量获取记录的反馈类型

        Args:
            record_ids: 记录 ID 列表

        Returns:
            {record_id: feedback_type} 字典，feedback_type 为 'like'/'dislike'/None
        """
        if not record_ids:
            return {}

        try:
            with self.db_manager.get_session() as session:
                # 查询这些 record_id 的反馈
                query_obj = session.query(
                    FeedbackDB.record_id,
                    FeedbackDB.feedback_type
                ).filter(
                    FeedbackDB.record_id.in_(record_ids)
                )

                results = query_obj.all()

                # 构建结果字典
                feedback_map = {}
                for record_id, feedback_type in results:
                    # 转换枚举为字符串
                    feedback_map[record_id] = feedback_type.value if feedback_type else None

                # 对于没有反馈的记录，返回 None
                for rid in record_ids:
                    if rid not in feedback_map:
                        feedback_map[rid] = None

                return feedback_map

        except Exception as e:
            logger.error(f"批量获取反馈类型失败: {e}", exc_info=True)
            return {rid: None for rid in record_ids}

    def check_has_feedback(self, record_ids: List[str]) -> Dict[str, bool]:
        """
        批量检查记录是否有反馈（向后兼容方法）

        Args:
            record_ids: 记录 ID 列表

        Returns:
            {record_id: has_feedback} 字典
        """
        feedback_types = self.get_feedback_types(record_ids)
        return {rid: (ftype is not None) for rid, ftype in feedback_types.items()}
