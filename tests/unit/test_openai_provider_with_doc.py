"""测试 OpenAI Provider 的文档注入逻辑"""
from unittest.mock import Mock, patch
import pytest

from ai_assistant.providers.openai_provider import OpenAIProvider
from ai_assistant.core.models import Message, Content
from ai_assistant.core.ai_provider import ANSWER_PERSPECTIVE_PROMPT, DocIndexingInProgressError


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
def test_openai_no_doc_context_still_has_answer_perspective(mock_post):
    """无 doc_context 时也要带回答视角 system 消息（与 Anthropic RAG 共用同一常量）"""
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        raise_for_status=lambda: None,
    )
    p = _make_provider()
    msgs = [Message(role="user", content=[Content(type="text", data="q")], timestamp=None)]
    p._send_with_context(msgs, doc_context="")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["messages"][0] == {"role": "system", "content": ANSWER_PERSPECTIVE_PROMPT}
    assert payload["messages"][1]["role"] == "user"
    # 绑指令句独有短语：版本视角与数据库方言视角
    system = payload["messages"][0]["content"]
    assert "升级后是否漏执行了数据库升级脚本" in system
    assert "先考虑数据库类型与版本差异" in system


def test_openai_short_circuits_on_indexing():
    """索引更新时基类模板方法向上抛出异常，未调用底层 API"""
    p = _make_provider()
    p.doc_manager = Mock()
    p.doc_manager.get_documents_by_query.side_effect = DocIndexingInProgressError("x")
    with patch("ai_assistant.providers.openai_provider.requests.post") as mock_post:
        msgs = [Message(role="user", content=[Content(type="text", data="q")], timestamp=None)]
        with pytest.raises(DocIndexingInProgressError):
            p.send_message(msgs)
        # 验证：未调用底层 API
        assert mock_post.call_count == 0
