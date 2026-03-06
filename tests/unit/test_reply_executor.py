import pyperclip
from ai_assistant.core.reply_executor import ReplyExecutor


def test_reply_executor_clipboard_mode():
    """测试剪贴板模式"""
    executor = ReplyExecutor(mode="clipboard", notification=False)

    reply = "This is a test reply"
    result = executor.execute(reply)

    assert result == True

    # 验证剪贴板内容
    clipboard_content = pyperclip.paste()
    assert clipboard_content == reply


def test_reply_executor_with_notification():
    """测试带通知的执行"""
    executor = ReplyExecutor(mode="clipboard", notification=True)

    reply = "Test with notification"
    result = executor.execute(reply)

    assert result == True
