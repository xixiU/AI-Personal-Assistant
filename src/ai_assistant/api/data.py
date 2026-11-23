"""
数据查询接口

提供问答历史和反馈数据的查询接口
使用服务端会话鉴权
"""

from flask import Blueprint, request, jsonify
from loguru import logger
from functools import wraps

from ai_assistant.api import success_response, error_response

# 创建数据查询 Blueprint
data_bp = Blueprint('data', __name__, url_prefix='/api')

# 全局变量（由 webhook_server 注入）
_chat_history_db = None
_feedback_manager = None
_user_manager = None


def init_data(chat_history_db, feedback_manager, user_manager):
    """
    初始化数据查询模块

    Args:
        chat_history_db: ChatHistoryDBQuery 实例（数据库查询类）
        feedback_manager: FeedbackDBQuery 实例（数据库查询类）
        user_manager: UserManager 实例
    """
    global _chat_history_db, _feedback_manager, _user_manager
    _chat_history_db = chat_history_db
    _feedback_manager = feedback_manager
    _user_manager = user_manager
    logger.info("数据查询模块初始化完成（服务端会话）")


def require_auth(f):
    """
    认证装饰器：验证会话有效性
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

        # 保存到 request 上下文
        request.current_user = user
        return f(*args, **kwargs)

    return decorated_function


@data_bp.route('/chat-history', methods=['GET'])
@require_auth
def get_chat_history():
    """
    查询问答历史

    请求参数：
    - page: 页码（默认 1）
    - page_size: 每页数量（默认 20，最大 100）
    - user_id: 用户 ID 过滤（可选，管理员可查所有用户）
    - session_id: 会话 ID 过滤（可选）
    - start_time: 开始时间（可选，ISO 8601 格式）
    - end_time: 结束时间（可选，ISO 8601 格式）
    - source: 来源过滤（可选，feishu/web/wechat）
    - keyword: 关键词搜索（可选，在 query 或 answer 中模糊匹配）

    响应：
    {
        "success": true,
        "message": "Success",
        "data": {
            "records": [
                {
                    "record_id": "uuid",
                    "session_id": "session-123",
                    "user_id": "user-456",
                    "user_name": "张三",
                    "query": "用户问题",
                    "response": "AI 回复",
                    "mode": "rag",
                    "source": "feishu",
                    "created_at": "2024-01-01T12:00:00",
                    "has_feedback": true
                }
            ],
            "total": 100,
            "page": 1,
            "page_size": 20
        }
    }
    """
    try:
        # 获取查询参数
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        user_id = request.args.get('user_id', None, type=str)
        session_id = request.args.get('session_id', None, type=str)
        start_time = request.args.get('start_time', None, type=str)
        end_time = request.args.get('end_time', None, type=str)
        source = request.args.get('source', None, type=str)
        keyword = request.args.get('keyword', None, type=str)

        # 参数验证
        if page < 1:
            return jsonify(error_response("页码必须大于 0", "INVALID_PARAMETER")), 400
        if page_size < 1 or page_size > 100:
            return jsonify(error_response("每页数量必须在 1-100 之间", "INVALID_PARAMETER")), 400
        if source and source not in ['feishu', 'web', 'wechat']:
            return jsonify(error_response("来源参数无效", "INVALID_PARAMETER")), 400

        # 权限检查：普通用户只能查自己的数据
        current_user = request.current_user
        if current_user.get('role') != 'admin':
            if user_id and user_id != current_user.get('user_id'):
                return jsonify(error_response("无权查询其他用户的数据", "PERMISSION_DENIED")), 403
            user_id = current_user.get('user_id')  # 强制只查自己的

        # 查询问答历史
        if not _chat_history_db:
            return jsonify(error_response("问答历史服务未初始化", "SERVICE_UNAVAILABLE")), 503

        offset = (page - 1) * page_size
        records = _chat_history_db.query(
            user_id=user_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            source=source,
            keyword=keyword,
            limit=page_size,
            offset=offset
        )
        total = _chat_history_db.count(
            user_id=user_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            source=source,
            keyword=keyword
        )

        # 批量检查反馈状态和类型
        if records and _feedback_manager:
            record_ids = [r['record_id'] for r in records]
            feedback_types = _feedback_manager.get_feedback_types(record_ids)
            for record in records:
                rid = record['record_id']
                feedback_type = feedback_types.get(rid)
                record['has_feedback'] = feedback_type is not None
                record['feedback_type'] = feedback_type  # 'like'/'dislike'/None

        logger.info(f"查询问答历史: page={page}, user_id={user_id}, session={session_id}, keyword={keyword}, total={total}")

        return jsonify(success_response({
            "records": records,
            "total": total,
            "page": page,
            "page_size": page_size
        })), 200

    except Exception as e:
        logger.error(f"查询问答历史异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@data_bp.route('/feedback', methods=['GET'])
@require_auth
def get_feedback():
    """
    查询反馈数据

    请求参数：
    - page: 页码（默认 1）
    - page_size: 每页数量（默认 20，最大 100）
    - user_id: 用户 ID 过滤（可选，管理员可查所有用户）
    - session_id: 会话 ID 过滤（可选）
    - feedback_type: 反馈类型过滤（可选，like/dislike）
    - start_time: 开始时间（可选，ISO 8601 格式）
    - end_time: 结束时间（可选，ISO 8601 格式）
    - source: 来源过滤（可选，feishu/web/wechat）

    响应：
    {
        "success": true,
        "message": "Success",
        "data": {
            "feedbacks": [
                {
                    "feedback_id": "uuid",
                    "record_id": "chat-record-uuid",
                    "session_id": "session-123",
                    "user_id": "user-456",
                    "user_name": "张三",
                    "feedback_type": "like",
                    "feedback_text": "回答很有帮助",
                    "source": "feishu",
                    "created_at": "2024-01-01T12:00:00",
                    "query": "原问题",
                    "response": "AI 回复"
                }
            ],
            "total": 50,
            "page": 1,
            "page_size": 20,
            "stats": {
                "like_count": 30,
                "dislike_count": 20,
                "like_rate": 0.6
            }
        }
    }
    """
    try:
        # 获取查询参数
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        user_id = request.args.get('user_id', None, type=str)
        session_id = request.args.get('session_id', None, type=str)
        feedback_type = request.args.get('feedback_type', None, type=str)
        start_time = request.args.get('start_time', None, type=str)
        end_time = request.args.get('end_time', None, type=str)
        source = request.args.get('source', None, type=str)

        # 参数验证
        if page < 1:
            return jsonify(error_response("页码必须大于 0", "INVALID_PARAMETER")), 400
        if page_size < 1 or page_size > 100:
            return jsonify(error_response("每页数量必须在 1-100 之间", "INVALID_PARAMETER")), 400
        if feedback_type and feedback_type not in ['like', 'dislike']:
            return jsonify(error_response("反馈类型参数无效", "INVALID_PARAMETER")), 400
        if source and source not in ['feishu', 'web', 'wechat']:
            return jsonify(error_response("来源参数无效", "INVALID_PARAMETER")), 400

        # 权限检查：普通用户只能查自己的数据
        current_user = request.current_user
        if current_user.get('role') != 'admin':
            if user_id and user_id != current_user.get('user_id'):
                return jsonify(error_response("无权查询其他用户的数据", "PERMISSION_DENIED")), 403
            user_id = current_user.get('user_id')

        # 查询反馈数据
        if not _feedback_manager:
            return jsonify(error_response("反馈服务未初始化", "SERVICE_UNAVAILABLE")), 503

        offset = (page - 1) * page_size
        feedbacks = _feedback_manager.query(
            user_id=user_id,
            session_id=session_id,
            feedback_type=feedback_type,
            start_time=start_time,
            end_time=end_time,
            source=source,
            limit=page_size,
            offset=offset
        )
        total = _feedback_manager.count(
            user_id=user_id,
            session_id=session_id,
            feedback_type=feedback_type,
            start_time=start_time,
            end_time=end_time,
            source=source
        )
        stats = _feedback_manager.get_stats(
            user_id=user_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            source=source
        )

        # 如果需要关联查询对话记录的 query 和 response，可以在这里补充
        # 当前先返回反馈记录本身
        logger.info(f"查询反馈数据: page={page}, user_id={user_id}, type={feedback_type}, total={total}")

        return jsonify(success_response({
            "feedbacks": feedbacks,
            "total": total,
            "page": page,
            "page_size": page_size,
            "stats": stats
        })), 200

    except Exception as e:
        logger.error(f"查询反馈数据异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500
