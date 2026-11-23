"""
用户个人接口

提供用户个人的对话历史和反馈记录查询接口
使用服务端会话鉴权，只能查询当前登录用户自己的数据
"""

from flask import Blueprint, request, jsonify, g
from loguru import logger
from functools import wraps

from ai_assistant.api import success_response, error_response

# 创建用户 Blueprint
user_bp = Blueprint('user', __name__, url_prefix='/api/user')

# 全局变量（由 webhook_server 注入）
_chat_history_db = None
_feedback_manager = None
_user_manager = None


def init_user(chat_history_db, feedback_manager, user_manager):
    """
    初始化用户接口模块

    Args:
        chat_history_db: ChatHistoryDBQuery 实例（数据库查询类）
        feedback_manager: FeedbackDBQuery 实例（数据库查询类）
        user_manager: UserManager 实例
    """
    global _chat_history_db, _feedback_manager, _user_manager
    _chat_history_db = chat_history_db
    _feedback_manager = feedback_manager
    _user_manager = user_manager
    logger.info("用户接口模块初始化完成")


def require_login(f):
    """
    登录装饰器：验证用户登录状态，将用户信息保存到 g.current_user
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 从 cookie 读取会话 token
        token = request.cookies.get('ai_session', '')
        if not token:
            return jsonify(error_response("未登录", "NOT_LOGGED_IN")), 401

        if not _user_manager:
            return jsonify(error_response("认证服务未初始化", "SERVICE_UNAVAILABLE")), 503

        # 验证会话
        user = _user_manager.validate_session(token)
        if not user:
            return jsonify(error_response("会话已过期或无效", "SESSION_INVALID")), 401

        # 保存到 g 对象（Flask 的请求上下文）
        g.current_user = user
        return f(*args, **kwargs)

    return decorated_function


@user_bp.route('/chat-history', methods=['GET'])
@require_login
def get_user_chat_history():
    """
    获取当前登录用户的对话历史

    请求参数：
    - page: 页码（默认 1）
    - page_size: 每页数量（默认 20，最大 100）
    - keyword: 关键词搜索（可选，在问题或回答中模糊匹配）

    响应：
    {
        "success": true,
        "message": "Success",
        "data": {
            "records": [
                {
                    "record_id": "uuid",
                    "timestamp": "2026-11-23T14:20:36",
                    "query": "用户的问题",
                    "answer": "AI的回答",
                    "source": "feishu",
                    "session_id": "...",
                    "has_feedback": true,
                    "feedback_type": "like"
                }
            ],
            "total": 150,
            "page": 1,
            "page_size": 20,
            "total_pages": 8
        }
    }
    """
    try:
        # 获取查询参数
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        keyword = request.args.get('keyword', None, type=str)

        # 参数验证
        if page < 1:
            return jsonify(error_response("页码必须大于 0", "INVALID_PARAMETER")), 400
        if page_size < 1 or page_size > 100:
            return jsonify(error_response("每页数量必须在 1-100 之间", "INVALID_PARAMETER")), 400

        # 获取当前用户
        current_user = g.current_user
        user_id = current_user.get('user_id')

        # 查询对话历史
        if not _chat_history_db:
            return jsonify(error_response("对话历史服务未初始化", "SERVICE_UNAVAILABLE")), 503

        offset = (page - 1) * page_size
        records = _chat_history_db.query(
            user_id=user_id,
            keyword=keyword,
            limit=page_size,
            offset=offset
        )
        total = _chat_history_db.count(
            user_id=user_id,
            keyword=keyword
        )

        # 批量检查反馈状态，并关联反馈类型
        if records and _feedback_manager:
            record_ids = [r['record_id'] for r in records]

            # 直接查询数据库，获取这些 record_id 的反馈信息
            with _feedback_manager.db_manager.get_session() as session:
                from ai_assistant.models.feedback_db import FeedbackDB
                feedbacks = session.query(FeedbackDB).filter(
                    FeedbackDB.record_id.in_(record_ids),
                    FeedbackDB.user_id == user_id
                ).all()

                # 构建 record_id -> feedback 的映射
                feedback_map = {fb.record_id: fb.to_dict() for fb in feedbacks}

            # 填充反馈信息
            for record in records:
                rid = record['record_id']
                if rid in feedback_map:
                    record['has_feedback'] = True
                    record['feedback_type'] = feedback_map[rid]['feedback_type']
                else:
                    record['has_feedback'] = False
                    record['feedback_type'] = None

        # 计算总页数
        total_pages = (total + page_size - 1) // page_size

        logger.info(f"查询用户对话历史: user_id={user_id}, page={page}, keyword={keyword}, total={total}")

        return jsonify(success_response({
            "records": records,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages
        })), 200

    except Exception as e:
        logger.error(f"查询用户对话历史异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@user_bp.route('/feedback', methods=['GET'])
@require_login
def get_user_feedback():
    """
    获取当前登录用户的反馈记录

    请求参数：
    - page: 页码（默认 1）
    - page_size: 每页数量（默认 20，最大 100）
    - feedback_type: 反馈类型过滤（可选：like/dislike）

    响应：
    {
        "success": true,
        "message": "Success",
        "data": {
            "records": [
                {
                    "feedback_id": "uuid",
                    "timestamp": "2026-11-23T14:20:36",
                    "feedback_type": "like",
                    "feedback_text": "回答很准确",
                    "record_id": "uuid",
                    "query": "相关的问题",
                    "answer": "相关的回答（前100字）"
                }
            ],
            "total": 50,
            "page": 1,
            "page_size": 20,
            "total_pages": 3
        }
    }
    """
    try:
        # 获取查询参数
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        feedback_type = request.args.get('feedback_type', None, type=str)

        # 参数验证
        if page < 1:
            return jsonify(error_response("页码必须大于 0", "INVALID_PARAMETER")), 400
        if page_size < 1 or page_size > 100:
            return jsonify(error_response("每页数量必须在 1-100 之间", "INVALID_PARAMETER")), 400
        if feedback_type and feedback_type not in ['like', 'dislike']:
            return jsonify(error_response("反馈类型参数无效", "INVALID_PARAMETER")), 400

        # 获取当前用户
        current_user = g.current_user
        user_id = current_user.get('user_id')

        # 查询反馈记录
        if not _feedback_manager:
            return jsonify(error_response("反馈服务未初始化", "SERVICE_UNAVAILABLE")), 503

        offset = (page - 1) * page_size
        feedbacks = _feedback_manager.query(
            user_id=user_id,
            feedback_type=feedback_type,
            limit=page_size,
            offset=offset
        )
        total = _feedback_manager.count(
            user_id=user_id,
            feedback_type=feedback_type
        )

        # 关联查询相关的问题和回答
        if feedbacks and _chat_history_db:
            record_ids = [fb['record_id'] for fb in feedbacks]

            # 批量查询对话历史（通过 record_id）
            # 注意：这里需要支持按 record_id 列表查询，目前 ChatHistoryDBQuery 没有这个方法
            # 我们使用一个简单的方法：逐个查询或者直接在数据库查询时添加
            # 为了性能，这里直接查询数据库
            with _chat_history_db.db_manager.get_session() as session:
                from ai_assistant.models.chat_history_db import ChatHistoryDB
                chat_records = session.query(ChatHistoryDB).filter(
                    ChatHistoryDB.record_id.in_(record_ids)
                ).all()

                # 构建 record_id -> chat_record 的映射
                chat_map = {record.record_id: record.to_dict() for record in chat_records}

            # 填充反馈记录的问答信息
            for feedback in feedbacks:
                rid = feedback['record_id']
                if rid in chat_map:
                    chat = chat_map[rid]
                    feedback['query'] = chat['query']
                    # 回答只显示前 100 字
                    answer = chat.get('answer', '')
                    feedback['answer'] = answer[:100] if len(answer) > 100 else answer
                else:
                    feedback['query'] = None
                    feedback['answer'] = None

        # 计算总页数
        total_pages = (total + page_size - 1) // page_size

        logger.info(f"查询用户反馈记录: user_id={user_id}, page={page}, feedback_type={feedback_type}, total={total}")

        return jsonify(success_response({
            "records": feedbacks,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages
        })), 200

    except Exception as e:
        logger.error(f"查询用户反馈记录异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500
