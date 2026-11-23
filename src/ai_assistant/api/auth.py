"""
认证接口

提供登录、登出、修改密码等认证相关接口
使用服务端会话（cookie）+ CSRF double-submit 防护
"""

from flask import Blueprint, request, jsonify, make_response
from loguru import logger
from typing import Optional
import secrets
import hmac

from ai_assistant.api import success_response, error_response

# 创建认证 Blueprint
auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

# 全局变量（由 webhook_server 注入）
_user_manager = None

# Cookie 配置
COOKIE_SESSION = "ai_session"  # 会话 cookie 名
COOKIE_CSRF = "ai_csrf"  # CSRF token cookie 名
CSRF_HEADER = "X-CSRF-Token"  # CSRF token 请求头名


def init_auth(user_manager):
    """
    初始化认证模块

    Args:
        user_manager: UserManager 实例
    """
    global _user_manager
    _user_manager = user_manager
    logger.info("认证模块初始化完成（服务端会话 + CSRF）")


def _set_session_cookie(response, token: str, max_age: int = 7 * 86400):
    """
    设置会话 cookie

    Args:
        response: Flask response 对象
        token: 会话 token
        max_age: 有效期（秒，默认 7 天）
    """
    response.set_cookie(
        COOKIE_SESSION,
        token,
        max_age=max_age,
        httponly=True,
        samesite='Lax',
        secure=False  # 生产环境建议改为 True（需要 HTTPS）
    )


def _clear_session_cookie(response):
    """清除会话 cookie"""
    response.set_cookie(
        COOKIE_SESSION,
        '',
        max_age=0,
        httponly=True,
        samesite='Lax'
    )


def _ensure_csrf_cookie(response):
    """
    确保响应中有 CSRF cookie。
    如果请求中已有合法的 CSRF cookie，保持不变；否则生成新的。

    Args:
        response: Flask response 对象
    """
    existing_csrf = request.cookies.get(COOKIE_CSRF, '')
    # 只有当前 cookie 不存在或格式不合法时才生成新值
    if not existing_csrf or len(existing_csrf) < 16:
        new_csrf = secrets.token_urlsafe(32)
        response.set_cookie(
            COOKIE_CSRF,
            new_csrf,
            max_age=86400,  # 24 小时
            httponly=False,  # 前端需要读取
            samesite='Lax',
            secure=False
        )


def _validate_csrf():
    """
    验证 CSRF token（写操作必须调用）

    Returns:
        (is_valid, error_response_tuple)
        is_valid=True 时 error_response_tuple 为 None
    """
    cookie_token = request.cookies.get(COOKIE_CSRF, '')
    header_token = request.headers.get(CSRF_HEADER, '')

    if not cookie_token or not header_token:
        return False, (jsonify(error_response("CSRF token 缺失", "CSRF_MISSING")), 403)

    if not hmac.compare_digest(cookie_token, header_token):
        return False, (jsonify(error_response("CSRF token 验证失败", "CSRF_INVALID")), 403)

    return True, None


@auth_bp.route('/login', methods=['POST'])
def login():
    """
    用户登录

    请求体：
    {
        "username": "admin",
        "password": "password123",
        "scope": "admin"  // 可选，管理员登录时传 "admin"
    }

    响应：
    {
        "success": true,
        "message": "登录成功",
        "data": {
            "user": {
                "user_id": "uuid",
                "username": "admin",
                "display_name": "管理员",
                "role": "admin"
            }
        }
    }

    Set-Cookie: ai_session=<token>; HttpOnly; SameSite=Lax
    Set-Cookie: ai_csrf=<csrf_token>; SameSite=Lax
    """
    try:
        # 验证 CSRF（写操作）
        is_valid, error_resp = _validate_csrf()
        if not is_valid:
            return error_resp

        data = request.get_json()
        if not data:
            resp = make_response(jsonify(error_response("请求体不能为空", "INVALID_REQUEST")), 400)
            _ensure_csrf_cookie(resp)
            return resp

        username = data.get('username', '').strip()
        password = data.get('password', '')
        scope = data.get('scope', 'user')

        # 参数验证
        if not username or not password:
            resp = make_response(jsonify(error_response("用户名和密码不能为空", "MISSING_FIELDS")), 400)
            _ensure_csrf_cookie(resp)
            return resp

        # 用户认证
        if not _user_manager:
            resp = make_response(jsonify(error_response("认证服务未初始化", "SERVICE_UNAVAILABLE")), 503)
            _ensure_csrf_cookie(resp)
            return resp

        user = _user_manager.authenticate(username, password)
        if not user:
            logger.warning(f"登录失败: username={username}")
            resp = make_response(jsonify(error_response("用户名或密码错误", "AUTH_FAILED")), 401)
            _ensure_csrf_cookie(resp)
            return resp

        # 管理员登录：scope='admin' 时检查 role
        if scope == 'admin' and user.get('role') != 'admin':
            logger.warning(f"非管理员尝试管理员登录: username={username}, role={user.get('role')}")
            resp = make_response(jsonify(error_response("该账号不是管理员", "NOT_ADMIN")), 403)
            _ensure_csrf_cookie(resp)
            return resp

        # 创建会话
        token = _user_manager.create_session(user['user_id'])
        if not token:
            logger.error(f"创建会话失败: user_id={user['user_id']}")
            resp = make_response(jsonify(error_response("创建会话失败", "SESSION_CREATE_FAILED")), 500)
            _ensure_csrf_cookie(resp)
            return resp

        logger.info(f"用户登录成功: username={username}, user_id={user['user_id']}, scope={scope}")

        # 构造响应
        resp = make_response(jsonify(success_response({
            "user": {
                "user_id": user['user_id'],
                "username": user['username'],
                "display_name": user.get('display_name', username),
                "role": user.get('role', 'user')
            }
        }, "登录成功")), 200)

        # 设置会话 cookie
        _set_session_cookie(resp, token)

        # 确保 CSRF cookie 存在
        _ensure_csrf_cookie(resp)

        return resp

    except Exception as e:
        logger.error(f"登录接口异常: {e}", exc_info=True)
        resp = make_response(jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500)
        _ensure_csrf_cookie(resp)
        return resp


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """
    用户登出

    Cookie: ai_session=<token>

    响应：
    {
        "success": true,
        "message": "登出成功"
    }
    """
    try:
        # 验证 CSRF
        is_valid, error_resp = _validate_csrf()
        if not is_valid:
            return error_resp

        token = request.cookies.get(COOKIE_SESSION, '')

        if token and _user_manager:
            # 删除会话（即使删除失败也返回成功，客户端清除 cookie 即可）
            _user_manager.delete_session(token)
            logger.info("用户登出")

        # 构造响应
        resp = make_response(jsonify(success_response(message="登出成功")), 200)

        # 清除会话 cookie
        _clear_session_cookie(resp)

        # 确保 CSRF cookie 存在
        _ensure_csrf_cookie(resp)

        return resp

    except Exception as e:
        logger.error(f"登出接口异常: {e}", exc_info=True)
        resp = make_response(jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500)
        _ensure_csrf_cookie(resp)
        return resp


@auth_bp.route('/me', methods=['GET'])
def me():
    """
    获取当前登录用户信息

    Cookie: ai_session=<token>

    响应：
    {
        "success": true,
        "data": {
            "user_id": "uuid",
            "username": "admin",
            "display_name": "管理员",
            "role": "admin"
        }
    }

    未登录返回 401
    """
    try:
        token = request.cookies.get(COOKIE_SESSION, '')

        if not token:
            resp = make_response(jsonify(error_response("未登录", "NOT_LOGGED_IN")), 401)
            _ensure_csrf_cookie(resp)
            return resp

        if not _user_manager:
            resp = make_response(jsonify(error_response("认证服务未初始化", "SERVICE_UNAVAILABLE")), 503)
            _ensure_csrf_cookie(resp)
            return resp

        user = _user_manager.validate_session(token)
        if not user:
            resp = make_response(jsonify(error_response("会话已过期或无效", "SESSION_INVALID")), 401)
            _clear_session_cookie(resp)
            _ensure_csrf_cookie(resp)
            return resp

        # 构造响应
        resp = make_response(jsonify(success_response({
            "user_id": user['user_id'],
            "username": user['username'],
            "display_name": user.get('display_name', user['username']),
            "role": user.get('role', 'user')
        })), 200)

        # 确保 CSRF cookie 存在
        _ensure_csrf_cookie(resp)

        return resp

    except Exception as e:
        logger.error(f"获取用户信息异常: {e}", exc_info=True)
        resp = make_response(jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500)
        _ensure_csrf_cookie(resp)
        return resp


@auth_bp.route('/change-password', methods=['POST'])
def change_password():
    """
    修改密码

    Cookie: ai_session=<token>

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

    修改成功后，该用户的所有会话（包括当前会话）将被删除，需要重新登录
    """
    try:
        # 验证 CSRF
        is_valid, error_resp = _validate_csrf()
        if not is_valid:
            return error_resp

        token = request.cookies.get(COOKIE_SESSION, '')

        if not token:
            resp = make_response(jsonify(error_response("未登录", "NOT_LOGGED_IN")), 401)
            _ensure_csrf_cookie(resp)
            return resp

        if not _user_manager:
            resp = make_response(jsonify(error_response("认证服务未初始化", "SERVICE_UNAVAILABLE")), 503)
            _ensure_csrf_cookie(resp)
            return resp

        # 验证会话
        user = _user_manager.validate_session(token)
        if not user:
            resp = make_response(jsonify(error_response("会话已过期或无效", "SESSION_INVALID")), 401)
            _clear_session_cookie(resp)
            _ensure_csrf_cookie(resp)
            return resp

        # 获取请求体
        data = request.get_json()
        if not data:
            resp = make_response(jsonify(error_response("请求体不能为空", "INVALID_REQUEST")), 400)
            _ensure_csrf_cookie(resp)
            return resp

        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')

        # 参数验证
        if not old_password or not new_password:
            resp = make_response(jsonify(error_response("旧密码和新密码不能为空", "MISSING_FIELDS")), 400)
            _ensure_csrf_cookie(resp)
            return resp

        if len(new_password) < 6:
            resp = make_response(jsonify(error_response("新密码长度不能少于 6 位", "INVALID_PASSWORD")), 400)
            _ensure_csrf_cookie(resp)
            return resp

        if old_password == new_password:
            resp = make_response(jsonify(error_response("新密码不能与旧密码相同", "INVALID_PASSWORD")), 400)
            _ensure_csrf_cookie(resp)
            return resp

        # 更新密码
        user_id = user['user_id']
        username = user['username']

        success = _user_manager.update_password(user_id, old_password, new_password)
        if not success:
            logger.warning(f"密码修改失败: username={username}")
            resp = make_response(jsonify(error_response("旧密码错误", "AUTH_FAILED")), 401)
            _ensure_csrf_cookie(resp)
            return resp

        # 删除该用户的所有会话（包括当前会话）
        deleted_count = _user_manager.delete_user_sessions(user_id)
        logger.info(f"密码修改成功: username={username}, 已删除 {deleted_count} 个会话")

        # 构造响应
        resp = make_response(jsonify(success_response(message="密码修改成功，请重新登录")), 200)

        # 清除当前会话 cookie
        _clear_session_cookie(resp)

        # 确保 CSRF cookie 存在
        _ensure_csrf_cookie(resp)

        return resp

    except Exception as e:
        logger.error(f"修改密码接口异常: {e}", exc_info=True)
        resp = make_response(jsonify(error_response("服务器内部错误", "INTERNAL_ERROR")), 500)
        _ensure_csrf_cookie(resp)
        return resp
