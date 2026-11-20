"""
管理员接口

提供用户管理相关接口（需要管理员权限）
"""

from flask import Blueprint, request, jsonify
from loguru import logger
from functools import wraps
import jwt

from ai_assistant.api import success_response, error_response, decode_token

# 创建管理员 Blueprint
admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')

# 全局变量（由 webhook_server 注入）
_user_manager = None
_jwt_secret = None


def init_admin(user_manager, jwt_secret: str):
    """
    初始化管理员模块

    Args:
        user_manager: UserManager 实例
        jwt_secret: JWT 密钥
    """
    global _user_manager, _jwt_secret
    _user_manager = user_manager
    _jwt_secret = jwt_secret
    logger.info("管理员模块初始化完成")


def require_auth(f):
    """
    认证装饰器：验证 Token 有效性
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify(error_response("缺少认证 Token", "MISSING_TOKEN")), 401

        token = auth_header[7:]
        try:
            payload = decode_token(token, _jwt_secret)
            request.current_user = payload  # 将用户信息附加到 request 对象
            return f(*args, **kwargs)
        except jwt.ExpiredSignatureError:
            return jsonify(error_response("Token 已过期，请重新登录", "TOKEN_EXPIRED")), 401
        except jwt.InvalidTokenError:
            return jsonify(error_response("Token 无效", "INVALID_TOKEN")), 401

    return decorated_function


def require_admin(f):
    """
    管理员权限装饰器：验证用户是否为管理员
    """
    @wraps(f)
    @require_auth
    def decorated_function(*args, **kwargs):
        user = request.current_user
        if user.get('role') != 'admin':
            logger.warning(f"非管理员尝试访问管理接口: user_id={user.get('user_id')}")
            return jsonify(error_response("需要管理员权限", "PERMISSION_DENIED")), 403
        return f(*args, **kwargs)

    return decorated_function


@admin_bp.route('/users', methods=['GET'])
@require_admin
def list_users():
    """
    查询用户列表

    请求参数：
    - page: 页码（默认 1）
    - page_size: 每页数量（默认 20，最大 100）
    - role: 角色过滤（可选，admin/user）

    响应：
    {
        "success": true,
        "message": "Success",
        "data": {
            "users": [
                {
                    "user_id": "uuid",
                    "username": "admin",
                    "display_name": "管理员",
                    "email": "admin@example.com",
                    "role": "admin",
                    "created_at": "2024-01-01T00:00:00"
                }
            ],
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
        role = request.args.get('role', None, type=str)

        # 参数验证
        if page < 1:
            return jsonify(error_response("页码必须大于 0", "INVALID_PARAMETER")), 400
        if page_size < 1 or page_size > 100:
            return jsonify(error_response("每页数量必须在 1-100 之间", "INVALID_PARAMETER")), 400
        if role and role not in ['admin', 'user']:
            return jsonify(error_response("角色参数无效", "INVALID_PARAMETER")), 400

        # 查询用户列表
        if not _user_manager:
            return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

        offset = (page - 1) * page_size
        users = _user_manager.list_users(limit=page_size, offset=offset, role=role)

        # TODO: 需要从数据库获取总数，当前返回假数据
        total = len(users)

        logger.info(f"查询用户列表: page={page}, page_size={page_size}, role={role}, count={len(users)}")

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
@require_admin
def create_user():
    """
    创建用户

    请求体：
    {
        "username": "newuser",
        "password": "password123",  # 可选，不填则自动生成
        "display_name": "新用户",
        "email": "user@example.com",
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
            "role": "user"
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
        email = data.get('email', '').strip()
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

        # 创建用户
        if not _user_manager:
            return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

        user_id = _user_manager.create_user(
            username=username,
            password=password,
            display_name=display_name or username,
            email=email or None,
            role=role
        )

        if not user_id:
            logger.warning(f"用户创建失败: username={username}（可能已存在）")
            return jsonify(error_response("用户创建失败（用户名可能已存在）", "CREATE_FAILED")), 400

        logger.info(f"用户创建成功: username={username}, user_id={user_id}, role={role}")

        response_data = {
            "user_id": user_id,
            "username": username,
            "display_name": display_name or username,
            "role": role
        }
        # 仅在自动生成密码时返回密码
        if auto_generated:
            response_data["password"] = password

        return jsonify(success_response(response_data, "用户创建成功")), 201

    except Exception as e:
        logger.error(f"创建用户异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@admin_bp.route('/users/<user_id>', methods=['PUT'])
@require_admin
def update_user(user_id: str):
    """
    更新用户信息

    请求体：
    {
        "display_name": "新名称",  # 可选
        "email": "newemail@example.com",  # 可选
        "role": "admin"  # 可选
    }

    响应：
    {
        "success": true,
        "message": "用户信息更新成功"
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify(error_response("请求体不能为空", "INVALID_REQUEST")), 400

        # TODO: 等待 Backend-DB-Agent 实现数据库模型后，调用 UserManager.update_user
        logger.warning(f"更新用户接口调用（待实现）: user_id={user_id}")

        return jsonify(error_response("功能开发中，等待数据库模型实现", "NOT_IMPLEMENTED")), 501

    except Exception as e:
        logger.error(f"更新用户异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@admin_bp.route('/users/<user_id>', methods=['DELETE'])
@require_admin
def delete_user(user_id: str):
    """
    删除用户（软删除）

    响应：
    {
        "success": true,
        "message": "用户删除成功"
    }
    """
    try:
        # 不允许删除自己
        current_user_id = request.current_user.get('user_id')
        if user_id == current_user_id:
            return jsonify(error_response("不能删除自己", "INVALID_OPERATION")), 400

        # 删除用户
        if not _user_manager:
            return jsonify(error_response("用户管理服务未初始化", "SERVICE_UNAVAILABLE")), 503

        success = _user_manager.delete_user(user_id)
        if not success:
            logger.warning(f"用户删除失败: user_id={user_id}")
            return jsonify(error_response("用户删除失败（用户不存在）", "DELETE_FAILED")), 404

        logger.info(f"用户删除成功: user_id={user_id}")
        return jsonify(success_response(message="用户删除成功")), 200

    except Exception as e:
        logger.error(f"删除用户异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@admin_bp.route('/users/<user_id>/reset-password', methods=['POST'])
@require_admin
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

        logger.info(f"管理员重置用户密码: user_id={user_id}, admin={request.current_user.get('username')}")

        response_data = {}
        if auto_generated:
            response_data["password"] = new_password

        return jsonify(success_response(response_data, "密码重置成功")), 200

    except Exception as e:
        logger.error(f"重置密码异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500
