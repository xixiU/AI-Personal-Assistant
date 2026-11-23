"""
对话历史数据库查询模块

提供对 chat_history 表的查询、统计功能
支持分页、过滤、排序等操作
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy import func, desc, or_
from loguru import logger

from ai_assistant.core.database import DatabaseManager
from ai_assistant.models.chat_history_db import ChatHistoryDB


class ChatHistoryDBQuery:
    """对话历史数据库查询类"""

    def __init__(self, db_manager: DatabaseManager):
        """
        初始化查询类

        Args:
            db_manager: 数据库管理器
        """
        self.db_manager = db_manager
        logger.info("对话历史数据库查询模块初始化完成")

    def query(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        source: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict]:
        """
        查询对话历史记录

        Args:
            user_id: 用户 ID 过滤
            session_id: 会话 ID 过滤
            start_time: 开始时间（ISO 8601 格式）
            end_time: 结束时间（ISO 8601 格式）
            source: 来源过滤（feishu/web/wechat）
            keyword: 关键词搜索（在 query 或 answer 中模糊匹配）
            limit: 返回记录数上限
            offset: 偏移量（分页用）

        Returns:
            记录列表，每条记录包含对话历史和反馈状态
        """
        try:
            with self.db_manager.get_session() as session:
                # 构建查询
                query_obj = session.query(ChatHistoryDB)

                # 应用过滤条件
                if user_id:
                    query_obj = query_obj.filter(ChatHistoryDB.user_id == user_id)
                if session_id:
                    query_obj = query_obj.filter(ChatHistoryDB.session_id == session_id)
                if source:
                    query_obj = query_obj.filter(ChatHistoryDB.source == source)

                # 时间范围过滤
                if start_time:
                    try:
                        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(ChatHistoryDB.timestamp >= start_dt)
                    except ValueError:
                        logger.warning(f"无效的 start_time 格式: {start_time}")

                if end_time:
                    try:
                        end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(ChatHistoryDB.timestamp <= end_dt)
                    except ValueError:
                        logger.warning(f"无效的 end_time 格式: {end_time}")

                # 关键词搜索（在 query 或 answer 中模糊匹配）
                if keyword:
                    search_pattern = f"%{keyword}%"
                    query_obj = query_obj.filter(
                        or_(
                            ChatHistoryDB.query.like(search_pattern),
                            ChatHistoryDB.answer.like(search_pattern)
                        )
                    )

                # 按时间倒序排序（最新的在前）
                query_obj = query_obj.order_by(desc(ChatHistoryDB.timestamp))

                # 分页
                query_obj = query_obj.limit(limit).offset(offset)

                # 执行查询
                records = query_obj.all()

                # 转换为字典列表
                result = []
                for record in records:
                    record_dict = record.to_dict()
                    # 添加 has_feedback 字段（需要关联查询 feedback 表）
                    # 这里先设置为 False，实际值需要在 API 层补充
                    record_dict['has_feedback'] = False
                    result.append(record_dict)

                return result

        except Exception as e:
            logger.error(f"查询对话历史失败: {e}", exc_info=True)
            return []

    def count(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        source: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> int:
        """
        统计符合条件的记录数

        Args:
            user_id: 用户 ID 过滤
            session_id: 会话 ID 过滤
            start_time: 开始时间
            end_time: 结束时间
            source: 来源过滤
            keyword: 关键词搜索

        Returns:
            记录总数
        """
        try:
            with self.db_manager.get_session() as session:
                # 构建查询
                query_obj = session.query(func.count(ChatHistoryDB.record_id))

                # 应用过滤条件（与 query 方法相同）
                if user_id:
                    query_obj = query_obj.filter(ChatHistoryDB.user_id == user_id)
                if session_id:
                    query_obj = query_obj.filter(ChatHistoryDB.session_id == session_id)
                if source:
                    query_obj = query_obj.filter(ChatHistoryDB.source == source)

                if start_time:
                    try:
                        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(ChatHistoryDB.timestamp >= start_dt)
                    except ValueError:
                        pass

                if end_time:
                    try:
                        end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                        query_obj = query_obj.filter(ChatHistoryDB.timestamp <= end_dt)
                    except ValueError:
                        pass

                if keyword:
                    search_pattern = f"%{keyword}%"
                    query_obj = query_obj.filter(
                        or_(
                            ChatHistoryDB.query.like(search_pattern),
                            ChatHistoryDB.answer.like(search_pattern)
                        )
                    )

                # 执行统计
                total = query_obj.scalar()
                return total or 0

        except Exception as e:
            logger.error(f"统计对话历史失败: {e}", exc_info=True)
            return 0

    def get_daily_stats(self, days: int = 7) -> List[Dict]:
        """
        获取每日问答趋势统计

        Args:
            days: 统计最近 N 天

        Returns:
            每日统计列表，格式: [{"date": "2024-01-01", "count": 100}, ...]
            注意：会补全空白日期（count=0）
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算开始日期
                start_date = datetime.now() - timedelta(days=days)

                # 按日期分组统计
                query_obj = session.query(
                    func.date(ChatHistoryDB.timestamp).label('date'),
                    func.count(ChatHistoryDB.record_id).label('count')
                ).filter(
                    ChatHistoryDB.timestamp >= start_date
                ).group_by(
                    func.date(ChatHistoryDB.timestamp)
                ).order_by('date')

                results = query_obj.all()

                # 构建日期到计数的映射
                data_map = {str(row.date): row.count for row in results}

                # 补全所有日期（包括没有数据的日期）
                complete_data = []
                current_date = start_date.date()
                end_date = datetime.now().date()

                while current_date <= end_date:
                    date_str = str(current_date)
                    complete_data.append({
                        "date": date_str,
                        "count": data_map.get(date_str, 0)
                    })
                    current_date += timedelta(days=1)

                return complete_data

        except Exception as e:
            logger.error(f"获取每日统计失败: {e}", exc_info=True)
            return []

    def get_top_questions(self, limit: int = 10, days: int = 30, use_clustering: bool = False, embedding_function=None) -> List[Dict]:
        """
        获取高频问题 Top N

        Args:
            limit: 返回记录数
            days: 统计最近 N 天
            use_clustering: 是否使用语义聚类（默认 False）
            embedding_function: embedding 函数（use_clustering=True 时必须提供）

        Returns:
            高频问题列表，格式: [{"query": "...", "count": 50}, ...]
            或聚类结果: [{
                "representative": "代表性问题",
                "count": 5,
                "similar_questions": ["相似问题1", "相似问题2"]
            }, ...]
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算开始日期
                start_date = datetime.now() - timedelta(days=days)

                # 按问题内容分组统计（获取更多数据用于聚类）
                query_limit = limit * 3 if use_clustering else limit
                query_obj = session.query(
                    ChatHistoryDB.query,
                    func.count(ChatHistoryDB.record_id).label('count')
                ).filter(
                    ChatHistoryDB.timestamp >= start_date
                ).group_by(
                    ChatHistoryDB.query
                ).order_by(
                    desc('count')
                ).limit(query_limit)

                results = query_obj.all()

                # 如果不使用聚类，直接返回
                if not use_clustering or not embedding_function:
                    return [
                        {"query": row.query, "count": row.count}
                        for row in results[:limit]
                    ]

                # 使用语义聚类
                from ai_assistant.core.question_clusterer import QuestionClusterer

                # 准备数据
                questions = [(row.query, row.count) for row in results]
                question_texts = [q[0] for q in questions]

                # 生成 embeddings
                try:
                    embeddings = embedding_function(question_texts)

                    # 聚类
                    clusterer = QuestionClusterer(similarity_threshold=0.8)
                    clusters = clusterer.cluster_questions(questions, embeddings)

                    # 返回前 N 个簇
                    return clusters[:limit]

                except Exception as e:
                    logger.error(f"语义聚类失败，降级到普通模式: {e}", exc_info=True)
                    # 失败时返回原始数据
                    return [
                        {"query": row.query, "count": row.count}
                        for row in results[:limit]
                    ]

        except Exception as e:
            logger.error(f"获取高频问题失败: {e}", exc_info=True)
            return []

    def get_top_users(self, limit: int = 10, days: int = 30) -> List[Dict]:
        """
        获取活跃用户 Top N

        Args:
            limit: 返回记录数
            days: 统计最近 N 天

        Returns:
            活跃用户列表，格式: [{"user_name": "...", "count": 30}, ...]
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算开始日期
                start_date = datetime.now() - timedelta(days=days)

                # 按用户分组统计
                query_obj = session.query(
                    ChatHistoryDB.user_name,
                    func.count(ChatHistoryDB.record_id).label('count')
                ).filter(
                    ChatHistoryDB.timestamp >= start_date,
                    ChatHistoryDB.user_name.isnot(None),
                    ChatHistoryDB.user_name != ''
                ).group_by(
                    ChatHistoryDB.user_name
                ).order_by(
                    desc('count')
                ).limit(limit)

                results = query_obj.all()

                # 转换为字典列表
                return [
                    {"user_name": row.user_name, "count": row.count}
                    for row in results
                ]

        except Exception as e:
            logger.error(f"获取活跃用户失败: {e}", exc_info=True)
            return []

    def get_source_distribution(self, days: int = 30) -> Dict[str, int]:
        """
        获取来源分布统计

        Args:
            days: 统计最近 N 天

        Returns:
            来源分布字典，格式: {"feishu": 100, "web": 50, "wechat": 20}
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算开始日期
                start_date = datetime.now() - timedelta(days=days)

                # 按来源分组统计
                query_obj = session.query(
                    ChatHistoryDB.source,
                    func.count(ChatHistoryDB.record_id).label('count')
                ).filter(
                    ChatHistoryDB.timestamp >= start_date
                ).group_by(
                    ChatHistoryDB.source
                )

                results = query_obj.all()

                # 转换为字典
                return {row.source: row.count for row in results}

        except Exception as e:
            logger.error(f"获取来源分布失败: {e}", exc_info=True)
            return {}

    def get_active_users_count(self, days: int = 30) -> int:
        """
        获取活跃用户数（近 N 天有提问的用户数）

        Args:
            days: 统计最近 N 天

        Returns:
            活跃用户数
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算开始日期
                start_date = datetime.now() - timedelta(days=days)

                # 统计不重复的 user_id
                query_obj = session.query(
                    func.count(func.distinct(ChatHistoryDB.user_id))
                ).filter(
                    ChatHistoryDB.timestamp >= start_date,
                    ChatHistoryDB.user_id.isnot(None),
                    ChatHistoryDB.user_id != ''
                )

                total = query_obj.scalar()
                return total or 0

        except Exception as e:
            logger.error(f"获取活跃用户数失败: {e}", exc_info=True)
            return 0

    def get_daily_active_users(self, days: int = 7) -> List[Dict]:
        """
        获取每日活跃用户数趋势统计

        Args:
            days: 统计最近 N 天

        Returns:
            每日统计列表，格式: [{"date": "2024-01-01", "count": 5}, ...]
            注意：会补全空白日期（count=0）
        """
        try:
            with self.db_manager.get_session() as session:
                # 计算开始日期
                start_date = datetime.now() - timedelta(days=days)

                # 按日期分组统计不重复的用户数
                query_obj = session.query(
                    func.date(ChatHistoryDB.timestamp).label('date'),
                    func.count(func.distinct(ChatHistoryDB.user_id)).label('count')
                ).filter(
                    ChatHistoryDB.timestamp >= start_date,
                    ChatHistoryDB.user_id.isnot(None),
                    ChatHistoryDB.user_id != ''
                ).group_by(
                    func.date(ChatHistoryDB.timestamp)
                ).order_by('date')

                results = query_obj.all()

                # 构建日期到计数的映射
                data_map = {str(row.date): row.count for row in results}

                # 补全所有日期（包括没有数据的日期）
                complete_data = []
                current_date = start_date.date()
                end_date = datetime.now().date()

                while current_date <= end_date:
                    date_str = str(current_date)
                    complete_data.append({
                        "date": date_str,
                        "count": data_map.get(date_str, 0)
                    })
                    current_date += timedelta(days=1)

                return complete_data

        except Exception as e:
            logger.error(f"获取每日活跃用户数失败: {e}", exc_info=True)
            return []

    def get_all_queries(self, user_id: Optional[str] = None, days: Optional[int] = None, limit: int = 1000) -> List[str]:
        """
        获取所有问题文本（用于词云分析）

        Args:
            user_id: 用户 ID 过滤（可选，不传则获取所有用户）
            days: 统计最近 N 天（可选，不传则获取全部）
            limit: 最多返回条数（默认 1000）

        Returns:
            问题文本列表
        """
        try:
            with self.db_manager.get_session() as session:
                # 构建查询
                query_obj = session.query(ChatHistoryDB.query)

                # 用户过滤
                if user_id:
                    query_obj = query_obj.filter(ChatHistoryDB.user_id == user_id)

                # 时间范围过滤
                if days:
                    start_date = datetime.now() - timedelta(days=days)
                    query_obj = query_obj.filter(ChatHistoryDB.timestamp >= start_date)

                # 只获取非空问题
                query_obj = query_obj.filter(
                    ChatHistoryDB.query.isnot(None),
                    ChatHistoryDB.query != ''
                )

                # 按时间倒序，限制数量
                query_obj = query_obj.order_by(desc(ChatHistoryDB.timestamp)).limit(limit)

                results = query_obj.all()

                # 返回文本列表
                return [row.query for row in results]

        except Exception as e:
            logger.error(f"获取问题文本失败: {e}", exc_info=True)
            return []
