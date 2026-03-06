from datetime import datetime
from ai_assistant.core.models import Content, Message, Session


def test_content_text_creation():
    content = Content(type="text", data="Hello")
    assert content.type == "text"
    assert content.data == "Hello"


def test_message_creation():
    content = Content(type="text", data="Test message")
    message = Message(
        role="user",
        content=[content],
        timestamp=datetime.now()
    )
    assert message.role == "user"
    assert len(message.content) == 1
    assert message.content[0].data == "Test message"


def test_session_add_message():
    session = Session(
        session_id="test_session",
        messages=[],
        context_mode="short",
        max_messages=10
    )

    content = Content(type="text", data="Message 1")
    message = Message(role="user", content=[content], timestamp=datetime.now())

    session.add_message(message)
    assert len(session.messages) == 1


def test_session_max_messages_limit():
    session = Session(
        session_id="test_session",
        messages=[],
        context_mode="short",
        max_messages=3
    )

    for i in range(5):
        content = Content(type="text", data=f"Message {i}")
        message = Message(role="user", content=[content], timestamp=datetime.now())
        session.add_message(message)

    assert len(session.messages) == 3
    assert session.messages[0].content[0].data == "Message 2"
