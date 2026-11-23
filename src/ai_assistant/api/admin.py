"""
管理员接口

提供用户管理相关接口（需要管理员权限）
"""

from flask import Blueprint, request, jsonify
from loguru import logger

from ai_assistant.api import success_response, error_response

# 创建管理员 Blueprint
admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')

# 全局变量（由 webhook_server 注入）
_user_manager = None


def init_admin(user_manager):
    """
    初始化管理员模块

    Args:
        user_manager: UserManager 实例
    """
    global _user_manager
    _user_manager = user_manager
    logger.info("管理员模块初始化完成")


@admin_bp.before_request
def check_admin_permission():
    """
    Blueprint 级别的权限检查：所有管理接口都需要管理员权限

    使用会话验证方案（从 ai_session cookie 读取 token）
    """
    # 从 cookie 中获取会话 token
    session_token = request.cookies.get('ai_session')
    if not session_token:
        return jsonify(error_response("未登录或会话已过期", "MISSING_SESSION")), 401

    # 验证会话
    if not _user_manager:
        return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

    user = _user_manager.validate_session(session_token)
    if not user:
        return jsonify(error_response("会话无效或已过期", "INVALID_SESSION")), 401

    # 验证管理员权限
    if user.get('role') != 'admin':
        logger.warning(f"非管理员尝试访问管理接口: user_id={user.get('user_id')}, path={request.path}")
        return jsonify(error_response("需要管理员权限", "PERMISSION_DENIED")), 403

    # 将用户信息附加到 request 对象
    request.current_user = user


@admin_bp.route('/users', methods=['GET'])
def list_users():
    """
    查询用户列表

    请求参数：
    - page: 页码（默认 1）
    - page_size: 每页数量（默认 20，最大 100）
    - keyword: 搜索关键词（username 或 display_name）
    - role: 角色过滤（可选，admin/user）
    - status: 状态过滤（可选，active/disabled）

    响应：
    {
        "success": true,
        "message": "Success",
        "data": {
            "users": [UserPublic...],
            "total": 100,
            "page": 1,
            "page_size": 20
        }
    }
    """
    try:
        # 获取分页参数
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        keyword = request.args.get('keyword', None, type=str)
        role = request.args.get('role', None, type=str)
        status = request.args.get('status', None, type=str)

        # 参数验证
        if page < 1:
            return jsonify(error_response("页码必须大于 0", "INVALID_PARAMETER")), 400
        if page_size < 1 or page_size > 100:
            return jsonify(error_response("每页数量必须在 1-100 之间", "INVALID_PARAMETER")), 400
        if role and role not in ['admin', 'user']:
            return jsonify(error_response("角色参数无效", "INVALID_PARAMETER")), 400
        if status and status not in ['active', 'disabled']:
            return jsonify(error_response("状态参数无效", "INVALID_PARAMETER")), 400

        # 转换 status 为 is_active 布尔值
        is_active = None
        if status == 'active':
            is_active = True
        elif status == 'disabled':
            is_active = False

        # 查询用户列表
        if not _user_manager:
            return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

        offset = (page - 1) * page_size
        users = _user_manager.list_users(
            limit=page_size,
            offset=offset,
            keyword=keyword,
            role=role,
            is_active=is_active
        )

        # 获取总数
        total = _user_manager.count_users(
            keyword=keyword,
            role=role,
            is_active=is_active
        )

        logger.info(f"查询用户列表: page={page}, page_size={page_size}, keyword={keyword}, role={role}, status={status}, total={total}")

        return jsonify(success_response({
            "users": users,
            "total": total,
            "page": page,
            "page_size": page_size
        })), 200

    except Exception as e:
        logger.error(f"查询用户列表异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@admin_bp.route('/users', methods=['POST'])
def create_user():
    """
    创建用户

    请求体：
    {
        "username": "newuser",
        "password": "password123",  # 可选，不填则自动生成
        "display_name": "新用户",
        "role": "user"  # admin 或 user
    }

    响应：
    {
        "success": true,
        "message": "用户创建成功",
        "data": {
            "user_id": "uuid",
            "username": "newuser",
            "password": "auto-generated-pass",  # 仅在自动生成时返回
            "display_name": "新用户",
            "role": "user",
            ...
        }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify(error_response("请求体不能为空", "INVALID_REQUEST")), 400

        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        display_name = data.get('display_name', '').strip()
        role = data.get('role', 'user').strip()

        # 参数验证
        if not username:
            return jsonify(error_response("用户名不能为空", "MISSING_FIELDS")), 400
        if len(username) < 3 or len(username) > 32:
            return jsonify(error_response("用户名长度必须在 3-32 之间", "INVALID_PARAMETER")), 400
        if role not in ['admin', 'user']:
            return jsonify(error_response("角色参数无效", "INVALID_PARAMETER")), 400

        # 密码处理：不填则自动生成
        auto_generated = False
        if not password:
            password = _user_manager.generate_random_password()
            auto_generated = True
            logger.info(f"为用户 {username} 自动生成密码")
        elif len(password) < 6:
            return jsonify(error_response("密码长度不能少于 6 位", "INVALID_PASSWORD")), 400

        # 创建用户（注意：不传 email 参数）
        if not _user_manager:
            return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

        user_id = _user_manager.create_user(
            username=username,
            password=password,
            display_name=display_name or username,
            role=role
        )

        if not user_id:
            logger.warning(f"用户创建失败: username={username}（可能已存在）")
            return jsonify(error_response("用户创建失败（用户名可能已存在）", "CREATE_FAILED")), 400

        logger.info(f"用户创建成功: username={username}, user_id={user_id}, role={role}")

        # 获取完整用户信息
        user_info = _user_manager.get_user(user_id)
        if not user_info:
            # 理论上不应该发生
            user_info = {
                "user_id": user_id,
                "username": username,
                "display_name": display_name or username,
                "role": role
            }

        # 仅在自动生成密码时添加 password 字段
        if auto_generated:
            user_info["password"] = password

        return jsonify(success_response(user_info, "用户创建成功")), 201

    except Exception as e:
        logger.error(f"创建用户异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@admin_bp.route('/users/<user_id>', methods=['PUT'])
def update_user(user_id: str):
    """
    更新用户信息

    请求体：
    {
        "display_name": "新名称",  # 可选
        "role": "admin",  # 可选
        "is_active": true  # 可选
    }

    响应：
    {
        "success": true,
        "message": "用户信息更新成功",
        "data": UserPublic
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify(error_response("请求体不能为空", "INVALID_REQUEST")), 400

        display_name = data.get('display_name')
        role = data.get('role')
        is_active = data.get('is_active')

        # 参数验证
        if role and role not in ['admin', 'user']:
            return jsonify(error_response("角色参数无效", "INVALID_PARAMETER")), 400

        if not _user_manager:
            return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

        # 获取当前用户信息
        current_user_id = request.current_user.get('user_id')
        target_user = _user_manager.get_user(user_id)
        if not target_user:
            return jsonify(error_response("用户不存在", "USER_NOT_FOUND")), 404

        # 业务规则检查

        # 1. 不能修改自己的角色
        if role and user_id == current_user_id and role != target_user.get('role'):
            return jsonify(error_response("不能修改自己的角色", "INVALID_OPERATION")), 400

        # 2. 不能停用或降级最后一个管理员
        if target_user.get('role') == 'admin':
            # 如果要降级或停用管理员
            will_demote = role and role != 'admin'
            will_disable = is_active is False

            if will_demote or will_disable:
                # 检查是否是最后一个管理员
                admin_count = _user_manager.count_admins(exclude_user_id=user_id)
                if admin_count == 0:
                    return jsonify(error_response("不能停用或降级最后一个管理员", "INVALID_OPERATION")), 400

        # 执行更新（注意：UserManager 的参数名是 name，不是 display_name）
        success = _user_manager.update_user(
            user_id=user_id,
            name=display_name,
            role=role,
            is_active=is_active
        )

        if not success:
            logger.warning(f"用户更新失败: user_id={user_id}")
            return jsonify(error_response("用户更新失败", "UPDATE_FAILED")), 500

        # 如果停用了用户，删除其所有会话
        if is_active is False:
            deleted_count = _user_manager.delete_all_sessions_for_user(user_id)
            logger.info(f"停用用户，删除其会话: user_id={user_id}, session_count={deleted_count}")

        logger.info(f"用户信息更新成功: user_id={user_id}, admin={request.current_user.get('username')}")

        # 返回更新后的用户信息
        updated_user = _user_manager.get_user(user_id)
        return jsonify(success_response(updated_user, "用户信息更新成功")), 200

    except Exception as e:
        logger.error(f"更新用户异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@admin_bp.route('/users/<user_id>', methods=['DELETE'])
def delete_user(user_id: str):
    """
    删除用户

    前端通过 PUT is_active=false 停用账号，不调用此接口。
    此接口返回 405 Method Not Allowed。
    """
    return jsonify(error_response("不支持删除操作，请使用停用功能", "METHOD_NOT_ALLOWED")), 405


@admin_bp.route('/stats', methods=['GET'])
def get_stats():
    """
    获取管理后台数据统计

    请求参数：
    - range: 统计范围（可选，7d/30d/90d，默认 7d）

    响应：
    {
        "success": true,
        "message": "Success",
        "data": {
            "summary": {
                "total_questions": int,
                "total_conversations": int,
                "active_users": int,
                "total_feedbacks": int,
                "feedback_rate": float
            },
            "daily": [{"date": "2024-01-01", "count": 100}, ...],
            "top_questions": [{"query": "...", "count": 50}, ...],
            "top_users": [{"user_name": "...", "count": 30}, ...],
            "sources": {"feishu": 100, "web": 50, "wechat": 20},
            "range": "7d"
        }
    }
    """
    try:
        # 获取查询参数
        range_param = request.args.get('range', '7d', type=str)

        # 参数验证
        if range_param not in ['7d', '30d', '90d']:
            return jsonify(error_response("范围参数无效", "INVALID_PARAMETER")), 400

        # 解析天数
        days_map = {'7d': 7, '30d': 30, '90d': 90}
        days = days_map[range_param]

        # 需要注入的数据库查询模块
        from ai_assistant.api.data import _chat_history_db, _feedback_manager

        if not _chat_history_db or not _feedback_manager:
            return jsonify(error_response("数据服务未初始化", "SERVICE_UNAVAILABLE")), 503

        # 1. 汇总统计
        total_questions = _chat_history_db.count()
        total_conversations = _chat_history_db.count()  # 暂时与问答数相同，后续可按 session 去重
        active_users = _chat_history_db.get_active_users_count(days=days)
        total_feedbacks = _feedback_manager.count()

        # 计算反馈率
        feedback_rate = total_feedbacks / total_questions if total_questions > 0 else 0.0

        summary = {
            "total_questions": total_questions,
            "total_conversations": total_conversations,
            "active_users": active_users,
            "total_feedbacks": total_feedbacks,
            "feedback_rate": round(feedback_rate, 2)
        }

        # 2. 每日问答趋势
        daily = _chat_history_db.get_daily_stats(days=days)

        # 3. 高频问题 Top 10
        top_questions = _chat_history_db.get_top_questions(limit=10, days=days)

        # 4. 活跃用户 Top 10
        top_users = _chat_history_db.get_top_users(limit=10, days=days)

        # 5. 来源分布
        sources = _chat_history_db.get_source_distribution(days=days)

        logger.info(f"查询管理后台统计: range={range_param}, total_questions={total_questions}")

        return jsonify(success_response({
            "summary": summary,
            "daily": daily,
            "top_questions": top_questions,
            "top_users": top_users,
            "sources": sources,
            "range": range_param
        })), 200

    except Exception as e:
        logger.error(f"查询管理后台统计异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@admin_bp.route('/users/<user_id>/reset-password', methods=['POST'])
def reset_user_password(user_id: str):
    """
    重置用户密码（管理员操作）

    请求体：
    {
        "new_password": "newpass123"  # 可选，不填则自动生成
    }

    响应：
    {
        "success": true,
        "message": "密码重置成功",
        "data": {
            "password": "auto-generated-pass"  # 仅在自动生成时返回
        }
    }
    """
    try:
        data = request.get_json() or {}
        new_password = data.get('new_password', '').strip()

        # 密码处理：不填则自动生成
        auto_generated = False
        if not new_password:
            new_password = _user_manager.generate_random_password()
            auto_generated = True
            logger.info(f"为用户 {user_id} 自动生成密码")
        elif len(new_password) < 6:
            return jsonify(error_response("密码长度不能少于 6 位", "INVALID_PASSWORD")), 400

        # 重置密码
        if not _user_manager:
            return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

        success = _user_manager.reset_password(user_id, new_password)
        if not success:
            logger.warning(f"密码重置失败: user_id={user_id}")
            return jsonify(error_response("密码重置失败（用户不存在）", "RESET_FAILED")), 404

        # 重置密码后，删除用户的所有会话（强制重新登录）
        deleted_count = _user_manager.delete_all_sessions_for_user(user_id)
        logger.info(f"重置密码后删除会话: user_id={user_id}, session_count={deleted_count}")

        logger.info(f"管理员重置用户密码: user_id={user_id}, admin={request.current_user.get('username')}")

        response_data = {}
        if auto_generated:
            response_data["password"] = new_password

        return jsonify(success_response(response_data, "密码重置成功")), 200

    except Exception as e:
        logger.error(f"重置密码异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500
