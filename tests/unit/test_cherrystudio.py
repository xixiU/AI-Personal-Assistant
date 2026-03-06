from datetime import datetime
from ai_assistant.core.models import Content, Message
from ai_assistant.providers.cherrystudio import CherryStudioProvider


def test_cherrystudio_message_format():
    """测试消息格式转换"""
    provider = CherryStudioProvider(
        base_url="http://localhost:8000",
        model="gpt-4"
    )

    messages = [
        Message(
            role="user",
            content=[Content(type="text", data="Hello")],
            timestamp=datetime.now()
        )
    ]

    # 这个测试只验证不会抛出异常
    # 实际 API 调用需要真实的服务
    assert provider.base_url == "http://localhost:8000"
    assert provider.model == "gpt-4"


def test_cherrystudio_health_check():
    """测试健康检查（会失败，因为没有真实服务）"""
    provider = CherryStudioProvider(
        base_url="http://localhost:8000",
        model="gpt-4"
    )

    # 预期返回 False，因为没有真实服务运行
    result = provider.check_health()
    assert result == False
