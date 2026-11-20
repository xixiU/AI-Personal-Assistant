"""
API 模块

提供 RESTful API 接口，包括：
- 认证接口（登录、登出、修改密码）
- 管理员接口（用户管理）
- 数据查询接口（问答历史、反馈数据）
"""

from flask import Blueprint, jsonify
from functools import wraps
from typing import Callable, Any
import jwt
from loguru import logger

# 统一响应格式


def success_response(data: Any = None, message: str = "Success") -> dict:
    """
    成功响应格���

    Args:
        data: 响应数据
        message: 响应消息

    Returns:
        统一格式的响应字典
    """
    return {
        "success": True,
        "message": message,
        "data": data
    }


def error_response(message: str, code: str = "ERROR", details: Any = None) -> dict:
    """
    错误响应格式

    Args:
        message: 错误消息
        code: 错误码
        details: 详细错误信息

    Returns:
        统一格式的错误响应字典
    """
    response = {
        "success": False,
        "message": message,
        "code": code
    }
    if details:
        response["details"] = details
    return response


# JWT 工具函数

def generate_token(user_id: str, username: str, role: str, secret_key: str, expires_in: int = 86400) -> str:
    """
    生成 JWT Token

    Args:
        user_id: 用户 ID
        username: 用户名
        role: 用户角色
        secret_key: JWT 密钥
        expires_in: 过期时间（秒，默认 24 小时）

    Returns:
        JWT Token 字符串
    """
    import time
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": int(time.time()) + expires_in,
        "iat": int(time.time())
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def decode_token(token: str, secret_key: str) -> dict:
    """
    解码 JWT Token

    Args:
        token: JWT Token 字符串
        secret_key: JWT 密钥

    Returns:
        Token payload 字典

    Raises:
        jwt.ExpiredSignatureError: Token 过期
        jwt.InvalidTokenError: Token 无效
    """
    return jwt.decode(token, secret_key, algorithms=["HS256"])
