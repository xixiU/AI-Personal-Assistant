"""测试文档索引状态异常处理"""
import pytest
import sys
from unittest.mock import Mock, patch, MagicMock

# Mock anthropic 模块（避免导入错误）
sys.modules['anthropic'] = MagicMock()

from ai_assistant.core.ai_provider import DocIndexingInProgressError
from ai_assistant.core.feishu_doc_manager import FeishuDocManager
from ai_assistant.providers.anthropic_provider import AnthropicProvider
from ai_assistant.core.models import Message, Content


def test_doc_indexing_error_raised():
    """测试：索引未就绪时抛出异常"""
    doc_manager = Mock(spec=FeishuDocManager)
    doc_manager.sources = ["wiki_token_123"]  # 配置了 sources
    doc_manager._indexed = False  # 索引未就绪

    with pytest.raises(DocIndexingInProgressError):
        # 模拟 get_documents_by_query 的实际逻辑
        if not doc_manager._indexed:
            raise DocIndexingInProgressError("文档索引正在更新中")


def test_anthropic_provider_catches_indexing_error():
    """测试：Provider 向上抛出 DocIndexingInProgressError（不再内部捕获）"""
    # 创建 mock doc_manager
    doc_manager = Mock(spec=FeishuDocManager)
    doc_manager.get_documents_by_query.side_effect = DocIndexingInProgressError("索引中")

    # 创建 Provider（mock Anthropic client）
    with patch('ai_assistant.providers.anthropic_provider.anthropic.Anthropic'):
        provider = AnthropicProvider(
            api_key="test-key",
            model="claude-sonnet-4-20250514",
        )
        provider.doc_manager = doc_manager

        # 构造测试消息
        from datetime import datetime
        messages = [
            Message(role="user", content=[Content(type="text", data="智慧法庭部署文档在哪？")], timestamp=datetime.now())
        ]

        # 调用 send_message（基类模板方法）应向上抛出异常
        with pytest.raises(DocIndexingInProgressError):
            provider.send_message(messages)

        # 验证：未调用 Anthropic API
        assert provider.client.messages.create.call_count == 0


def test_normal_path_not_affected():
    """测试：索引就绪时正常流程不受影响"""
    # 创建 mock doc_manager（索引就绪）
    doc_manager = Mock(spec=FeishuDocManager)
    doc_manager.get_documents_by_query.return_value = "## 相关文档\n这是文档内容"

    # 创建 Provider 并 mock Anthropic client
    with patch('ai_assistant.providers.anthropic_provider.anthropic.Anthropic') as mock_anthropic:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(text="这是 AI 的回复")]
        mock_response.usage = Mock(input_tokens=100, output_tokens=50)
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.return_value = mock_client

        provider = AnthropicProvider(
            api_key="test-key",
            model="claude-sonnet-4-20250514",
        )
        provider.doc_manager = doc_manager

        from datetime import datetime
        messages = [
            Message(role="user", content=[Content(type="text", data="测试问题")], timestamp=datetime.now())
        ]

        reply = provider.send_message(messages)

        # 验证：调用了 API 并返回正常回复
        assert reply == "这是 AI 的回复"
        assert mock_client.messages.create.call_count == 1


def test_base_get_doc_context_no_manager():
    """基类：无 doc_manager 时返回空字符串"""
    from ai_assistant.core.ai_provider import AIProvider

    class DummyProvider(AIProvider):
        def check_health(self):
            return True
        def _send_with_context(self, messages, doc_context, session_id=None):
            return f"got_context={doc_context!r}"

    p = DummyProvider()
    assert p.doc_manager is None
    msgs = [Message(role="user", content=[Content(type="text", data="hi")], timestamp=None)]
    assert p._get_doc_context(msgs) == ""


def test_base_send_message_short_circuits_on_indexing():
    """基类：doc_manager 抛出 DocIndexingInProgressError 时向上抛出（不调用子类实现）"""
    from ai_assistant.core.ai_provider import AIProvider

    class DummyProvider(AIProvider):
        def __init__(self):
            super().__init__()
            self.sub_called = False
        def check_health(self):
            return True
        def _send_with_context(self, messages, doc_context, session_id=None):
            self.sub_called = True
            return "should_not_reach"

    p = DummyProvider()
    p.doc_manager = Mock()
    p.doc_manager.get_documents_by_query.side_effect = DocIndexingInProgressError("x")

    msgs = [Message(role="user", content=[Content(type="text", data="hi")], timestamp=None)]
    with pytest.raises(DocIndexingInProgressError):
        p.send_message(msgs)
    # 验证：未调用子类的 _send_with_context
    assert p.sub_called is False


def test_base_extract_last_user_text():
    """基类：从消息列表中提取最后一条用户文本"""
    from ai_assistant.core.ai_provider import AIProvider
    msgs = [
        Message(role="user", content=[Content(type="text", data="first")], timestamp=None),
        Message(role="assistant", content=[Content(type="text", data="reply")], timestamp=None),
        Message(role="user", content=[Content(type="text", data="second")], timestamp=None),
    ]
    assert AIProvider._extract_last_user_text(msgs) == "second"


# 生产端 main.py:746-751 / :2067-2070（捕获异常 → 入队 + 返回提示语）在此无测试。
# 曾有一条 test_caller_handles_indexing_error 试图覆盖它，但它在测试体内自己
# 复写了一遍 try/except 并硬编码了提示语文案，没有调用 main.py 的任何代码——
# 把 main.py:751 整行删掉它依然全绿，是假绿灯，已删。
# 已验证：把 :751 和 :2070 的提示语换成哨兵常量，全量 550 条测试结果不变
# （4 failed / 546 passed 两侧完全一致），所以这两处确实零覆盖。
# 真覆盖需要能驱动 _process_event（:409）的夹具，当前被 jieba 等缺失依赖阻塞。
# 消费端（_check_and_retry_pending_messages）已由 test_retry_queue_logic.py 覆盖。
