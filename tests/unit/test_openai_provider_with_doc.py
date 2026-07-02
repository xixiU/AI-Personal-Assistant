"""测试 OpenAI Provider 的文档注入逻辑"""
from unittest.mock import Mock, patch
import pytest

from ai_assistant.providers.openai_provider import OpenAIProvider
from ai_assistant.core.models import Message, Content
from ai_assistant.core.ai_provider import DocIndexingInProgressError


def _make_provider():
    return OpenAIProvider(
        base_url="http://fake",
        api_key="k",
        model="gpt-4",
        timeout=5,
    )


@patch("ai_assistant.providers.openai_provider.requests.post")
def test_openai_injects_doc_context_as_system(mock_post):
    """有 doc_context 时，api_messages 头部应插入 system 消息"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        raise_for_status=lambda: None,
    )
    p = _make_provider()
    msgs = [Message(role="user", content=[Content(type="text", data="q")], timestamp=None)]
    p._send_with_context(msgs, doc_context="DOC-CONTENT")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["messages"][0]["role"] == "system"
    assert "DOC-CONTENT" in payload["messages"][0]["content"]


@patch("ai_assistant.providers.openai_provider.requests.post")
def test_openai_no_doc_context_no_system(mock_post):
    """无 doc_context 时，不应插入 system 消息"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        raise_for_status=lambda: None,
    )
    p = _make_provider()
    msgs = [Message(role="user", content=[Content(type="text", data="q")], timestamp=None)]
    p._send_with_context(msgs, doc_context="")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["messages"][0]["role"] == "user"


def test_openai_short_circuits_on_indexing():
    """索引更新时通过基类模板方法返回提示语，未调用底层 API"""
    p = _make_provider()
    p.doc_manager = Mock()
    p.doc_manager.get_documents_by_query.side_effect = DocIndexingInProgressError("x")
    with patch("ai_assistant.providers.openai_provider.requests.post") as mock_post:
        msgs = [Message(role="user", content=[Content(type="text", data="q")], timestamp=None)]
        reply = p.send_message(msgs)
        assert "文档索引正在更新中" in reply
        assert mock_post.call_count == 0
