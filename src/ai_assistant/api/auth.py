"""
认证接口

提供登录、登出、修改密码等认证相关接口
"""

from flask import Blueprint, request, jsonify
from loguru import logger
from typing import Optional
import jwt

from ai_assistant.api import success_response, error_response, generate_token, decode_token

# 创建认证 Blueprint
auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

# 全局变量（由 webhook_server 注入）
_user_manager = None
_jwt_secret = None
_jwt_expires_in = 86400  # 默认 24 小时


def init_auth(user_manager, jwt_secret: str, jwt_expires_in: int = 86400):
    """
    初始化认证模块

    Args:
        user_manager: UserManager 实例
        jwt_secret: JWT 密钥
        jwt_expires_in: Token 过期时间（秒）
    """
    global _user_manager, _jwt_secret, _jwt_expires_in
    _user_manager = user_manager
    _jwt_secret = jwt_secret
    _jwt_expires_in = jwt_expires_in
    logger.info(f"认证模块初始化完成，Token 过期时间: {jwt_expires_in / 3600:.1f} 小时")


@auth_bp.route('/login', methods=['POST'])
def login():
    """
    用户登录

    请求体：
    {
        "username": "admin",
        "password": "password123"
    }

    响应：
    {
        "success": true,
        "message": "登录成功",
        "data": {
            "token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
            "user": {
                "user_id": "uuid",
                "username": "admin",
                "display_name": "管理员",
                "role": "admin"
            }
        }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify(error_response("请求体不能为空", "INVALID_REQUEST")), 400

        username = data.get('username', '').strip()
        password = data.get('password', '')

        # 参数验证
        if not username or not password:
            return jsonify(error_response("用户名和密码不能为空", "MISSING_FIELDS")), 400

        # 用户认证
        if not _user_manager:
            return jsonify(error_response("认证服务未初始化", "SERVICE_UNAVAILABLE")), 503

        user = _user_manager.authenticate(username, password)
        if not user:
            logger.warning(f"登录失败: username={username}")
            return jsonify(error_response("用户名或密码错误", "AUTH_FAILED")), 401

        # 生成 Token
        token = generate_token(
            user_id=user['user_id'],
            username=user['username'],
            role=user.get('role', 'user'),
            secret_key=_jwt_secret,
            expires_in=_jwt_expires_in
        )

        logger.info(f"用户登录成功: username={username}, user_id={user['user_id']}")

        return jsonify(success_response({
            "token": token,
            "user": {
                "user_id": user['user_id'],
                "username": user['username'],
                "display_name": user.get('display_name', username),
                "role": user.get('role', 'user')
            }
        }, "登录成功")), 200

    except Exception as e:
        logger.error(f"登录接口异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """
    用户登出

    请求头：
    Authorization: Bearer <token>

    响应：
    {
        "success": true,
        "message": "登出成功"
    }

    注：JWT 是无状态的，登出只需客户端删除 Token 即可。
    此接口主要用于日志记录和未来可能的 Token 黑名单机制。
    """
    try:
        # 从请求头获取 Token
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify(error_response("缺少认证 Token", "MISSING_TOKEN")), 401

        token = auth_header[7:]  # 去掉 "Bearer " 前缀

        # 解码 Token（验证有效性）
        try:
            payload = decode_token(token, _jwt_secret)
            username = payload.get('username', 'unknown')
            logger.info(f"用户登出: username={username}")
        except jwt.ExpiredSignatureError:
            # Token 已过期，仍然返回成功（客户端目的已达成）
            logger.info("已过期的 Token 登出请求")
        except jwt.InvalidTokenError:
            return jsonify(error_response("Token 无效", "INVALID_TOKEN")), 401

        return jsonify(success_response(message="登出成功")), 200

    except Exception as e:
        logger.error(f"登出接口异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500


@auth_bp.route('/change-password', methods=['POST'])
def change_password():
    """
    修改密码

    请求头：
    Authorization: Bearer <token>

    请求体：
    {
        "old_password": "oldpass123",
        "new_password": "newpass456"
    }

    响应：
    {
        "success": true,
        "message": "密码修改成功"
    }
    """
    try:
        # 从请求头获取 Token
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify(error_response("缺少认证 Token", "MISSING_TOKEN")), 401

        token = auth_header[7:]

        # 解码 Token
        try:
            payload = decode_token(token, _jwt_secret)
            user_id = payload.get('user_id')
            username = payload.get('username')
        except jwt.ExpiredSignatureError:
            return jsonify(error_response("Token 已过期，请重新登录", "TOKEN_EXPIRED")), 401
        except jwt.InvalidTokenError:
            return jsonify(error_response("Token 无效", "INVALID_TOKEN")), 401

        # 获取请求体
        data = request.get_json()
        if not data:
            return jsonify(error_response("请求体不能为空", "INVALID_REQUEST")), 400

        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')

        # 参数验证
        if not old_password or not new_password:
            return jsonify(error_response("旧密码和新密码不能为空", "MISSING_FIELDS")), 400

        if len(new_password) < 6:
            return jsonify(error_response("新密码长度不能少于 6 位", "INVALID_PASSWORD")), 400

        if old_password == new_password:
            return jsonify(error_response("新密码不能与旧密码相同", "INVALID_PASSWORD")), 400

        # 更新密码
        if not _user_manager:
            return jsonify(error_response("认证服务未初始化", "SERVICE_UNAVAILABLE")), 503

        success = _user_manager.update_password(user_id, old_password, new_password)
        if not success:
            logger.warning(f"密码修改失败: username={username}")
            return jsonify(error_response("旧密码错误", "AUTH_FAILED")), 401

        logger.info(f"密码修改成功: username={username}")
        return jsonify(success_response(message="密码修改成功")), 200

    except Exception as e:
        logger.error(f"修改密码接口异常: {e}", exc_info=True)
        return jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500
