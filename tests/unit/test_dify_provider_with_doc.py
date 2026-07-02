"""测试 Dify Provider 的文档注入逻辑"""
from unittest.mock import Mock, patch
import pytest

from ai_assistant.providers.dify_provider import DifyProvider
from ai_assistant.core.models import Message, Content
from ai_assistant.core.ai_provider import DocIndexingInProgressError


def _make_provider():
    return DifyProvider(
        base_url="http://fake",
        api_key="k",
        app_type="chat",
        timeout=5,
    )


@patch("ai_assistant.providers.dify_provider.requests.post")
def test_dify_injects_doc_context_to_query(mock_post):
    """有 doc_context 时，query 应前置拼接文档内容"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"answer": "ok", "metadata": {"usage": {}}},
        raise_for_status=lambda: None,
    )
    p = _make_provider()
    msgs = [Message(role="user", content=[Content(type="text", data="问题")], timestamp=None)]
    p._send_with_context(msgs, doc_context="DOC-CONTENT")
    payload = mock_post.call_args.kwargs["json"]
    assert "[参考文档]" in payload["query"]
    assert "DOC-CONTENT" in payload["query"]
    assert "[用户问题]" in payload["query"]
    assert "问题" in payload["query"]


@patch("ai_assistant.providers.dify_provider.requests.post")
def test_dify_no_doc_context_no_prefix(mock_post):
    """无 doc_context 时，query 不应有前置拼接"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"answer": "ok", "metadata": {"usage": {}}},
        raise_for_status=lambda: None,
    )
    p = _make_provider()
    msgs = [Message(role="user", content=[Content(type="text", data="问题")], timestamp=None)]
    p._send_with_context(msgs, doc_context="")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["query"] == "问题"
    assert "[参考文档]" not in payload["query"]


@patch("ai_assistant.providers.dify_provider.requests.post")
def test_dify_truncates_doc_context_at_8000(mock_post):
    """doc_context 超过 8000 字符时应截断"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"answer": "ok", "metadata": {"usage": {}}},
        raise_for_status=lambda: None,
    )
    p = _make_provider()
    msgs = [Message(role="user", content=[Content(type="text", data="q")], timestamp=None)]
    long_doc = "A" * 9000
    p._send_with_context(msgs, doc_context=long_doc)
    payload = mock_post.call_args.kwargs["json"]
    # 前缀 + 8000 字符截断 + 用户问题
    assert len(payload["query"]) < len(long_doc) + 100


def test_dify_short_circuits_on_indexing():
    """索引更新时通过基类模板方法返回提示语，未调用底层 API"""
    p = _make_provider()
    p.doc_manager = Mock()
    p.doc_manager.get_documents_by_query.side_effect = DocIndexingInProgressError("x")
    with patch("ai_assistant.providers.dify_provider.requests.post") as mock_post:
        msgs = [Message(role="user", content=[Content(type="text", data="q")], timestamp=None)]
        reply = p.send_message(msgs)
        assert "文档索引正在更新中" in reply
        assert mock_post.call_count == 0
