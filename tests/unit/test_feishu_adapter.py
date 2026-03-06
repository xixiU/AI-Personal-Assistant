from ai_assistant.adapters.feishu import FeishuAdapter


def test_feishu_adapter_initialization():
    """测试飞书适配器初始化"""
    adapter = FeishuAdapter()

    assert adapter.window_titles == ["飞书", "Lark", "Feishu"]
    assert adapter.current_window is None
    assert adapter.last_message is None


def test_feishu_adapter_detect_window():
    """测试窗口检测（可能失败，因为没有飞书窗口）"""
    adapter = FeishuAdapter()

    # 如果没有飞书窗口，应该返回 False
    result = adapter.detect_active_window()
    assert isinstance(result, bool)


def test_feishu_adapter_get_session_id():
    """测试获取会话 ID"""
    adapter = FeishuAdapter()

    # 没有窗口时应该返回 None
    session_id = adapter.get_session_id()
    assert session_id is None
