from datetime import datetime
from ai_assistant.core.models import Content, Message, Session
from ai_assistant.core.context_manager import ContextManager


def test_get_or_create_session():
    manager = ContextManager(max_messages=10, session_timeout=3600)

    session = manager.get_or_create_session("test_session")
    assert session.session_id == "test_session"
    assert len(session.messages) == 0


def test_add_message_to_session():
    manager = ContextManager(max_messages=10, session_timeout=3600)

    content = Content(type="text", data="Hello")
    message = Message(role="user", content=[content], timestamp=datetime.now())

    manager.add_message("test_session", message)

    session = manager.get_or_create_session("test_session")
    assert len(session.messages) == 1
    assert session.messages[0].content[0].data == "Hello"


def test_get_context():
    manager = ContextManager(max_messages=10, session_timeout=3600)

    for i in range(5):
        content = Content(type="text", data=f"Message {i}")
        message = Message(role="user", content=[content], timestamp=datetime.now())
        manager.add_message("test_session", message)

    context = manager.get_context("test_session")
    assert len(context) == 5


def test_cleanup_expired_sessions():
    manager = ContextManager(max_messages=10, session_timeout=1)

    content = Content(type="text", data="Test")
    message = Message(role="user", content=[content], timestamp=datetime.now())
    manager.add_message("session1", message)

    import time
    time.sleep(2)

    manager.cleanup_expired_sessions()

    # 过期会话应该被清理
    assert "session1" not in manager.sessions
