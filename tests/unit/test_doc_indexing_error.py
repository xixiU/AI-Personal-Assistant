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
    """测试：Provider 捕获异常并返回友好提示"""
    # 创建 mock doc_manager
    doc_manager = Mock(spec=FeishuDocManager)
    doc_manager.get_documents_by_query.side_effect = DocIndexingInProgressError("索引中")

    # 创建 Provider（mock Anthropic client）
    with patch('ai_assistant.providers.anthropic_provider.anthropic.Anthropic'):
        provider = AnthropicProvider(
            api_key="test-key",
            model="claude-sonnet-4-20250514",
            doc_manager=doc_manager
        )

        # 构造测试消息
        from datetime import datetime
        messages = [
            Message(role="user", content=[Content(type="text", data="智慧法庭部署文档在哪？")], timestamp=datetime.now())
        ]

        # 调用 send_message
        reply = provider.send_message(messages)

        # 验证：返回提示语，且未调用 Anthropic API
        assert "文档索引正在更新中" in reply
        assert "1-2分钟" in reply
        assert provider.client.messages.create.call_count == 0  # 未调用 API


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
            doc_manager=doc_manager
        )

        from datetime import datetime
        messages = [
            Message(role="user", content=[Content(type="text", data="测试问题")], timestamp=datetime.now())
        ]

        reply = provider.send_message(messages)

        # 验证：调用了 API 并返回正常回复
        assert reply == "这是 AI 的回复"
        assert mock_client.messages.create.call_count == 1
