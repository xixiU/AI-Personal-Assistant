"""
assistant prefill 拼接测试（防"双前缀"回归）

背景（防回归）：
子调用用 assistant prefill 强制输出格式起手。Anthropic 原生接口把 prefill 当成
已输出的前缀、只回补全部分，所以需要拼回；但本项目支持自定义 base_url
（new-api/one-api 这类 OpenAI 兼容网关），部分网关会忽略尾部 assistant 消息、
直接返回**完整**结果。此时无条件拼接会得到 '{"attack":{"attack": true, ...}'，
JSON 解析必败。

更糟的是网关返回 200（不抛异常），_call_subcall 的"去 prefill 重试"永远不触发：
- 安全判定 → JSON 解析失败 → fail-open → **真实攻击被静默放行**
- 关键词提取 → keywords=[] → RAG 退化为无关键词检索

两层防御：
1. _join_prefill 在拼接处判断是否需要拼
2. extract_first_json_object 兜底剥掉重复的 '{"键":' 前缀
"""
from ai_assistant.core.ai_provider import (
    _join_prefill,
    extract_first_json_object,
    _parse_prompt_safety_response,
    _parse_keyword_extraction_response,
    _PROMPT_SAFETY_PREFILL,
    _KEYWORD_EXTRACTION_PREFILL,
)


# ============ _join_prefill ============

def test_native_api_returns_completion_only():
    """原生 Anthropic：只回补全部分，必须拼回 prefill"""
    out = _join_prefill('{"attack":', ' false, "type": "none", "reason": ""}')
    assert out == '{"attack": false, "type": "none", "reason": ""}'


def test_gateway_returns_full_json_not_double_prefixed():
    """网关回完整 JSON：不能再拼 prefill，否则双前缀解析必败"""
    out = _join_prefill('{"attack":', '{"attack": true, "type": "jailbreak"}')
    assert out == '{"attack": true, "type": "jailbreak"}'
    assert '{"attack":{"attack"' not in out


def test_gateway_full_json_with_leading_whitespace():
    """网关回完整 JSON 且带前导空白，同样不应拼接"""
    out = _join_prefill('{"attack":', '  \n{"attack": true}')
    assert out == '{"attack": true}'


def test_gateway_echoes_prefill_verbatim():
    """网关原样带上 prefill 全文时也不应重复拼"""
    out = _join_prefill('{"keywords":', '{"keywords": ["线程"], "generic": false}')
    assert out.count('"keywords"') == 1


def test_numeric_prefill_completion():
    """编号类 prefill：只回补全部分时正常拼回"""
    assert _join_prefill("编号：", " 1,2") == "编号： 1,2"


def test_numeric_prefill_echoed():
    """编号类 prefill 被网关原样返回时不重复"""
    out = _join_prefill("编号：", "编号：1,2")
    assert out.count("编号：") == 1


def test_empty_prefill_passthrough():
    """prefill 为空时原样返回正文"""
    assert _join_prefill("", '{"a": 1}') == '{"a": 1}'


def test_empty_body():
    """正文为空时返回 prefill 本身，不抛异常"""
    assert _join_prefill('{"attack":', "") == '{"attack":'
    assert _join_prefill('{"attack":', None) == '{"attack":'


# ============ extract_first_json_object 兜底 ============

def test_double_prefix_stripped_by_extractor():
    """兜底层：双前缀被剥掉一层，解析出真正的对象"""
    bad = '{"attack":{"attack": true, "type": "jailbreak", "reason": "忽略设定"}'
    assert extract_first_json_object(bad) == (
        '{"attack": true, "type": "jailbreak", "reason": "忽略设定"}'
    )


def test_double_prefix_keywords():
    """关键词提取的双前缀同样能剥"""
    bad = '{"keywords":{"keywords": ["线程", "日志"], "generic": false}'
    out = extract_first_json_object(bad)
    assert out.count('"keywords"') == 1


def test_normal_json_untouched():
    """正常 JSON 不应被误剥"""
    ok = '{"attack": false, "type": "none"}'
    assert extract_first_json_object(ok) == ok


# ============ 端到端：真实攻击不得被放行 ============

def test_real_attack_not_fail_open_via_join():
    """网关回完整 JSON 时，真实攻击必须仍被识别（而非 fail-open 放行）"""
    gateway_body = '{"attack": true, "type": "jailbreak", "reason": "要求忽略设定"}'
    text = _join_prefill(_PROMPT_SAFETY_PREFILL, gateway_body)
    result = _parse_prompt_safety_response(text, "q")
    assert result.is_attack is True
    assert result.attack_type == "jailbreak"


def test_real_attack_not_fail_open_via_extractor():
    """即使双前缀已经产生，兜底层也要能救回来"""
    bad = '{"attack":{"attack": true, "type": "prompt_injection", "reason": "x"}'
    result = _parse_prompt_safety_response(bad, "q")
    assert result.is_attack is True


def test_keywords_survive_gateway_full_json():
    """网关回完整 JSON 时关键词不应退化为空"""
    gateway_body = '{"keywords": ["线程信息", "线程"], "generic": false}'
    text = _join_prefill(_KEYWORD_EXTRACTION_PREFILL, gateway_body)
    result = _parse_keyword_extraction_response(text, "q")
    assert result.keywords == ["线程信息", "线程"]


def test_safety_fail_open_preserved_on_garbage():
    """完全非 JSON 时保持 fail-open（不得改成 fail-close 误杀正常请求）"""
    garbage = "你还没有贴线程信息呢，把线程内容发给我，我来帮你分析。"
    result = _parse_prompt_safety_response(garbage, "q")
    assert result.is_attack is False
