"""
Anthropic Claude Provider

使用 Anthropic API 调用 Claude 模型，支持集成飞书文档管理器。
文档内容在调用前注入 system prompt，Claude 基于文档生成回复。

支持 Agentic 模式：Claude 可以主动调用工具（如代码搜索、文件读取）进行多轮排查。
"""

from typing import Any, Dict, List, Optional
from loguru import logger
import anthropic
import json
import re

from ai_assistant.core.ai_provider import (
    AIProvider,
    KeywordExtractionResult,
    PromptSafetyResult,
    classify_api_error,
    friendly_api_error_message,
    _KEYWORD_EXTRACTION_SYSTEM_PROMPT,
    _KEYWORD_EXTRACTION_PREFILL,
    _parse_keyword_extraction_response,
    _PROMPT_SAFETY_SYSTEM_PROMPT,
    _PROMPT_SAFETY_PREFILL,
    _parse_prompt_safety_response,
    _DOC_FILTER_SYSTEM_PROMPT,
    _DOC_FILTER_PREFILL,
    build_keyword_extraction_messages,
    build_prompt_safety_messages,
    build_doc_filter_messages,
    parse_doc_index_list,
    log_subcall_degraded,
    _join_prefill,
)
from ai_assistant.core.models import Message
from ai_assistant.utils.log_summary import summarize_obj, summarize_text


# ============================================================================
# 工具调用请求体的结构自检与诊断日志
#
# === 背景（2026-10-27 10:38 线上故障）===
# Agentic 排查第 3 轮被上游拒绝，日志里只有这一行：
#   ERROR Anthropic API 错误: status=400, message=Error code: 400 -
#   {'error': {'type': '<nil>', 'message': 'Invalid tool use format. |
#   REQUEST_BODY_INVALID (request id: 2026...iT2rjM5r) (request id: 2026...JXABB)'}}
# 请求体一个字节都没打，所以无法判断「是哪一轮、哪条消息、哪个字段不合法」，
# 只能靠猜。本节的函数就是为了让下一次同样的 400 可以被直接定位。
#
# === 设计取舍 ===
# 1. 只打【结构骨架】，不打内容。这类 400 几乎都是结构问题（空 text block、
#    tool_use/tool_result 配不上、content 类型不对），知道 role 序列 +
#    content block 类型序列 + 各 block 长度 + tool_use_id 配对关系就够定位；
#    而把 3442 字符的用户堆栈或 6677 字符的 Java 源码倒进日志既违反
#    CLAUDE.md「不得记录完整用户消息」，也会把真正有用的那几行冲掉。
# 2. 自检（audit_api_messages）在【发请求前】就能跑，所以它既是日志素材，
#    也是容错决策依据：自检有发现 → 请求体真的坏了，重试同一份没意义，
#    直接裁掉最后一轮；自检无发现 → 大概率是上游瞬时失败，原样重试一次。
# 3. 网关返回的 request id 必须留着。它是找网关方对账的唯一凭据，而且
#    一次 400 里嵌了两个不同格式的 id，说明请求穿过了多级代理。
# ============================================================================

# 骨架日志最多展开的消息条数（超长对话只打前 N 条，避免一行日志撑爆终端）
_SKELETON_MAX_MSGS = 40

# tool_use_id 在骨架里的展示长度（Anthropic 的 id 形如 toolu_01Abc...，
# 只需要能在同一条日志里区分和配对，不需要全长）
_TOOL_ID_CHARS = 14

# error_detail（会进飞书告警）里上游错误文案的摘要窗口。
#
# 【先记住一件事】summarize_text 打的是**头窗 + 尾窗**两段，不是只打头部：
#     label(len=N,lines=L,sha1=XX):'头 limit 字符'…'尾 40 字符'
# 判断"某个短语在不在摘要里"时，命中可能来自尾窗而不是头窗。下面的推导和
# 配套测试都绕着这个特性走，改动前请先看 log_summary 的 PREVIEW_TAIL_CHARS。
#
# 为什么是 200 而不是默认值（80）：默认窗口下，现场那条文案的
# "Invalid tool use format" 会被**截在词中间** —— 实测头窗输出止于
# `'Invalid tool use forma`（前缀 `Error code: 400 - {'error': {'type':
# '<nil>', 'message': '` 占掉 58 字符，剩给短语的只有 22 个）。
#
# 而另一条短语 "REQUEST_BODY_INVALID" 是否可见，**取决于上游文案尾部有没有
# 恰好重复它**：现场这条（208 字符，该短语只在偏移 85 出现一次）默认窗口下
# 完全看不到；换成网关把 message 又塞进 'code' 字段那种形态（140 字符，短语
# 重复两次），尾窗就把它捞回来了。两者在默认窗口下的差别纯粹是巧合。
#
# 这才是必须给足余量的真正理由：网关文案格式不受我们控制，默认窗口下告警的
# 可判别性**依赖上游巧合**。不要据此算出"那把窗口调到 100/120 就够了" ——
# 那是在赌下一种文案形态的偏移量。实测三种形态里判别短语起始于 58 / 55 / 86，
# 200 对三者都有余量。
#
# 为什么不是 600：limit 同时抬高"整段原样显示"的门槛（见 log_summary 的
# PREVIEW_HEAD_CHARS 注释），600 会让任何 ≤600 字符的文案原样打全。若网关
# 把用户请求片段回显进 400 文案，那就是用户正文整段进告警。实测一条 582
# 字符的回显文案：limit=600 整段落盘，limit=200 被压成摘要。
# 200 是"够判断错误类型"和"不足以整段回显用户正文"之间的取值。
_ALERT_MSG_CHARS = 200

# 上游错误文案里出现这些词，判定为"工具调用格式类 400"
_TOOL_FORMAT_400_HINTS = (
    "tool_result",
    "tool_use",
    "tool use",
    "tool_calls",
    "invalid tool",
    "request_body_invalid",
)


def _short_id(value: Any) -> str:
    """tool_use_id 的短形式（仅用于日志配对，不是业务标识）"""
    s = "" if value is None else str(value)
    if not s:
        return "<缺失>"
    return s if len(s) <= _TOOL_ID_CHARS else s[:_TOOL_ID_CHARS] + ".."


def _describe_block(block: Any) -> str:
    """
    描述单个 content block 的"结构骨架"：类型 + 规模 + 配对标识，不含正文。

    text 块特意区分 EMPTY / BLANK：Anthropic 会以 400 拒绝空的或纯空白的
    text block，而这种块只有在它被回传到【下一轮】请求里时才会触发报错，
    光看当轮响应是发现不了的。
    """
    if not isinstance(block, dict):
        return f"<{type(block).__name__}>"

    btype = block.get("type", "<无 type>")

    if btype == "text":
        text = block.get("text")
        if text is None:
            return "text(NONE)"
        if not isinstance(text, str):
            return f"text(<{type(text).__name__}>)"
        if not text:
            return "text(EMPTY)"
        if not text.strip():
            return f"text(BLANK:{len(text)})"
        return f"text({len(text)})"

    if btype == "tool_use":
        tool_input = block.get("input")
        if isinstance(tool_input, dict):
            # 参数名后面带上【值类型】。这不是凑数：现场第 1、2 轮的 tool_use
            # 参数全是字符串且通过，第 3 轮首次出现整数参数
            # （read_file 的 start_line/end_line）就被拒。若上游网关把 input
            # 当成 map[string]string 转换，故障点正好落在类型上，而只打参数名
            # 是看不出来的。
            keys = "+".join(
                f"{k}:{type(v).__name__}" for k, v in tool_input.items()
            ) or "无参数"
        else:
            keys = f"<{type(tool_input).__name__}>"
        return f"tool_use(id={_short_id(block.get('id'))},name={block.get('name')},keys={keys})"

    if btype == "tool_result":
        content = block.get("content")
        if content is None:
            kind = "NONE"
        elif isinstance(content, str):
            kind = f"str,{len(content)}" if content else "str,EMPTY"
        elif isinstance(content, list):
            kind = f"list,{len(content)}块"
        else:
            kind = f"<{type(content).__name__}>"
        flag = ",is_error" if block.get("is_error") else ""
        return f"tool_result(id={_short_id(block.get('tool_use_id'))},{kind}{flag})"

    if btype == "image":
        source = block.get("source") or {}
        data = source.get("data") if isinstance(source, dict) else None
        media = source.get("media_type") if isinstance(source, dict) else "?"
        return f"image({media},b64={len(data) if isinstance(data, str) else '?'})"

    return f"{btype}(?)"


def _describe_content(content: Any) -> str:
    """描述一条消息的 content（字符串形态 / block 数组形态都支持）"""
    if content is None:
        return "<None>"
    if isinstance(content, str):
        return f"str({len(content)})" if content else "str(EMPTY)"
    if isinstance(content, list):
        if not content:
            return "<空数组>"
        return ",".join(_describe_block(b) for b in content)
    return f"<{type(content).__name__}>"


def describe_api_messages(api_messages: Any) -> str:
    """
    把即将发给上游的 messages 渲染成单行"结构骨架"，用于诊断 400。

    形如：
        msgs=5 | [0]user:text(3442) | [1]assistant:text(57),tool_use(id=toolu_01Abc..,
        name=find_files,keys=name_pattern) | [2]user:tool_result(id=toolu_01Abc..,str,142) | ...

    刻意压成一行：loguru 一条记录一行，日志采集按行切分，多行会被拆散。

    Args:
        api_messages: 传给 client.messages.create(messages=...) 的那个列表

    Returns:
        单行骨架描述（绝不包含消息正文）
    """
    try:
        if not isinstance(api_messages, list):
            return f"<非列表:{type(api_messages).__name__}>"

        parts = [f"msgs={len(api_messages)}"]
        for i, msg in enumerate(api_messages):
            if i >= _SKELETON_MAX_MSGS:
                parts.append(f"…(+{len(api_messages) - _SKELETON_MAX_MSGS} msgs 未展开)")
                break
            if not isinstance(msg, dict):
                parts.append(f"[{i}]<{type(msg).__name__}>")
                continue
            parts.append(f"[{i}]{msg.get('role', '<无 role>')}:{_describe_content(msg.get('content'))}")
        return " | ".join(parts)
    except Exception as e:  # pragma: no cover - 纯兜底，诊断日志不许影响主流程
        return f"<骨架生成失败:{type(e).__name__}>"


def _iter_blocks(msg: Any) -> List[Dict[str, Any]]:
    """取出一条消息里的 dict 形态 block（content 为字符串时返回空列表）"""
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def audit_api_messages(api_messages: Any, *, expects_prefill: bool = False) -> List[str]:
    """
    发请求前自检 messages 结构，返回"会被上游以 400 拒绝"的问题清单。

    检查项都来自 Anthropic messages API 的硬约束，以及工具调用多轮回传时
    实际踩过/可能踩到的坑：
      - 消息序列：非空、首条为 user、末条为 user、同角色不得相邻
      - content：不得为空数组 / 空字符串
      - text block：不得为空或纯空白（最隐蔽的一条，详见 _describe_block）
      - tool_use：必须有 id / name，input 必须是对象
      - tool_result：必须有 tool_use_id，content 不得为空
      - 配对：assistant 里每个 tool_use 都要在【紧随其后那条 user 消息】里
        有同 id 的 tool_result；反向也不能有找不到 tool_use 的孤立 tool_result
      - 位置：tool_use 只能在 assistant、tool_result 只能在 user

    本函数只报【按 Anthropic 规范确定非法】的项，因为返回值会参与调用方的
    裁剪决策（`(tool_format_hit or post_issues)` 非空即裁）。"合法但可疑"
    的观察（如并行工具导致的 tool_result 顺序不一致）一律放在
    audit_api_messages_advisory 里，只打日志不参与决策 —— 混在一起会让
    与工具格式无关的 400 稳定误触发裁剪，详见那个函数的 docstring。

    === 为什么 expects_prefill 必须显式传，且默认 False ===
    本项目有两类消息列表，对"末条角色"的要求正好相反：
      1. 工具循环（Agentic / Operations）：必须以 user 结尾。末条是 assistant
         意味着拼装逻辑漏了 tool_result，是真实畸形体。
      2. AI 子调用（关键词提取 / 标题过滤 / 安全判定，见 core/ai_provider.py
         的 build_*_messages）：**故意**以 `{"role":"assistant", "content": PREFILL}`
         结尾，用 assistant prefill 钉死输出格式。
    若不区分，把本函数接到子调用路径上会稳定产生一条假的
    "末条消息 role='assistant'"。这不只是脏日志——调用方的裁剪条件是
    `(tool_format_hit or post_issues)`，一个假阳性就足以触发
    `api_messages[:-2]`，把本来正常的 prefill 请求剪坏。
    所以这里用**显式入参 + 默认 False**：工具循环保持最严，
    只有明确知道自己在走 prefill 路径的调用方才放宽，
    不会因为"默认宽松"让真实畸形体悄悄漏过去。

    注意：即使 expects_prefill=True，"末条 assistant 里带 tool_use"仍会被
    下面的配对检查抓出来（报"后面没有任何消息，缺少 tool_result"），
    所以放宽的只是"角色"这一点，不是整条尾部约束。

    Args:
        api_messages: 传给 client.messages.create(messages=...) 的那个列表
        expects_prefill: 该列表是否刻意以 assistant prefill 结尾。
                         工具循环传 False（默认），子调用路径传 True。

    Returns:
        问题描述列表（中文，带 msg 下标）。空列表表示未发现结构问题。
    """
    issues: List[str] = []
    try:
        if not isinstance(api_messages, list):
            return [f"messages 不是列表而是 {type(api_messages).__name__}"]
        if not api_messages:
            return ["messages 为空数组"]

        first_role = api_messages[0].get("role") if isinstance(api_messages[0], dict) else None
        if first_role != "user":
            issues.append(f"首条消息 role={first_role!r}，Anthropic 要求首条必须是 user")

        last_role = api_messages[-1].get("role") if isinstance(api_messages[-1], dict) else None
        if last_role != "user" and not (expects_prefill and last_role == "assistant"):
            issues.append(f"末条消息 role={last_role!r}，请求必须以 user 消息结尾")

        prev_role = None
        for i, msg in enumerate(api_messages):
            if not isinstance(msg, dict):
                issues.append(f"msg[{i}] 不是 dict 而是 {type(msg).__name__}")
                prev_role = None
                continue

            role = msg.get("role")
            if role not in ("user", "assistant"):
                issues.append(f"msg[{i}] role={role!r} 非法（只能是 user / assistant）")
            if role is not None and role == prev_role:
                issues.append(f"msg[{i}] 与 msg[{i-1}] 同为 {role}，角色必须交替")
            prev_role = role

            content = msg.get("content")
            if content is None:
                issues.append(f"msg[{i}]({role}) content 为 None")
                continue
            if isinstance(content, str):
                if not content.strip():
                    issues.append(f"msg[{i}]({role}) content 为空字符串")
                continue
            if not isinstance(content, list):
                issues.append(f"msg[{i}]({role}) content 类型为 {type(content).__name__}，应为字符串或数组")
                continue
            if not content:
                issues.append(f"msg[{i}]({role}) content 为空数组，上游会拒绝")
                continue

            for j, block in enumerate(content):
                if not isinstance(block, dict):
                    issues.append(f"msg[{i}].block[{j}] 不是 dict 而是 {type(block).__name__}")
                    continue
                btype = block.get("type")

                if btype == "text":
                    text = block.get("text")
                    if not isinstance(text, str):
                        issues.append(
                            f"msg[{i}]({role}).block[{j}] text 类型为 "
                            f"{type(text).__name__}，应为字符串"
                        )
                    elif not text:
                        issues.append(f"msg[{i}]({role}).block[{j}] 是空 text block，上游会以 400 拒绝")
                    elif not text.strip():
                        issues.append(
                            f"msg[{i}]({role}).block[{j}] 是纯空白 text block"
                            f"({len(text)} 字符)，上游会以 400 拒绝"
                        )

                elif btype == "tool_use":
                    if not block.get("id"):
                        issues.append(f"msg[{i}].block[{j}] tool_use 缺少 id")
                    if not block.get("name"):
                        issues.append(f"msg[{i}].block[{j}] tool_use 缺少 name")
                    if not isinstance(block.get("input"), dict):
                        issues.append(
                            f"msg[{i}].block[{j}] tool_use(id={_short_id(block.get('id'))}) 的 input "
                            f"类型为 {type(block.get('input')).__name__}，必须是对象"
                        )
                    if role != "assistant":
                        issues.append(f"msg[{i}] role={role} 里出现 tool_use，只能出现在 assistant 消息")

                elif btype == "tool_result":
                    if not block.get("tool_use_id"):
                        issues.append(f"msg[{i}].block[{j}] tool_result 缺少 tool_use_id")
                    rc = block.get("content")
                    if rc is None or (isinstance(rc, str) and not rc) or (isinstance(rc, list) and not rc):
                        issues.append(
                            f"msg[{i}].block[{j}] tool_result"
                            f"(id={_short_id(block.get('tool_use_id'))}) 的 content 为空"
                        )
                    if role != "user":
                        issues.append(f"msg[{i}] role={role} 里出现 tool_result，只能出现在 user 消息")

                elif btype is None:
                    issues.append(f"msg[{i}].block[{j}] 缺少 type 字段")

        # 配对检查：tool_use 必须在紧随其后的 user 消息里拿到同 id 的 tool_result
        for i, msg in enumerate(api_messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            use_ids = [b.get("id") for b in _iter_blocks(msg) if b.get("type") == "tool_use"]
            if not use_ids:
                continue

            nxt = api_messages[i + 1] if i + 1 < len(api_messages) else None
            if nxt is None:
                issues.append(
                    f"msg[{i}] 的 tool_use({','.join(_short_id(x) for x in use_ids)}) "
                    f"后面没有任何消息，缺少 tool_result"
                )
                continue
            if not isinstance(nxt, dict) or nxt.get("role") != "user":
                issues.append(
                    f"msg[{i}] 的 tool_use 之后不是 user 消息"
                    f"（msg[{i+1}] role={nxt.get('role') if isinstance(nxt, dict) else '?'}），"
                    f"tool_result 必须紧跟在后"
                )
                continue

            result_ids = [
                b.get("tool_use_id") for b in _iter_blocks(nxt) if b.get("type") == "tool_result"
            ]
            missing = [x for x in use_ids if x not in result_ids]
            orphan = [x for x in result_ids if x not in use_ids]
            if missing:
                issues.append(
                    f"msg[{i}] 的 tool_use({','.join(_short_id(x) for x in missing)}) "
                    f"在 msg[{i+1}] 中没有对应的 tool_result"
                )
            if orphan:
                issues.append(
                    f"msg[{i+1}] 的 tool_result({','.join(_short_id(x) for x in orphan)}) "
                    f"在 msg[{i}] 中找不到对应的 tool_use"
                )
        return issues
    except Exception as e:  # pragma: no cover - 纯兜底
        return [f"<自检失败:{type(e).__name__}: {e}>"]


def audit_api_messages_advisory(api_messages: Any) -> List[str]:
    """
    收集"合法但可疑"的结构观察，**仅供日志**，绝不参与任何决策。

    === 为什么必须和 audit_api_messages 分开 ===
    本函数的观察项按 Anthropic 规范都是**允许**的，所以它们不是"请求体坏了"
    的证据。而调用方的裁剪条件是 `(tool_format_hit or post_issues)` —— 列表
    非空即成立。一旦把这类观察混进 audit_api_messages 的返回值，任何一个
    【与工具格式无关】的 400（max_tokens 超限、模型名写错、网关额度耗尽回
    成 400）都会被判成"本地自检发现结构问题"，于是白扔一轮正常的工具交互、
    扣掉一份重试预算，还在日志里写下一个把排查方向带偏的结论。

    而触发条件是常态不是边角：并行工具用 `concurrent.futures.as_completed`
    收集结果（见 _send_with_context_agentic 的并行分支），tool_result 的顺序
    按完成先后排列，和 tool_use 的调用顺序天然不一致。模型一轮要两三个只读
    查询太常见，也就是说"顺序不一致"几乎每次并行都会出现。

    诊断价值保留（严格校验的中间网关可能按位置配对，排查时值得知道），
    决策权剥离。

    Args:
        api_messages: 传给 client.messages.create(messages=...) 的那个列表

    Returns:
        观察描述列表（中文，带 msg 下标）。空列表表示没有可疑之处。
    """
    notes: List[str] = []
    try:
        if not isinstance(api_messages, list):
            return []

        for i, msg in enumerate(api_messages):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            use_ids = [b.get("id") for b in _iter_blocks(msg) if b.get("type") == "tool_use"]
            if not use_ids:
                continue

            nxt = api_messages[i + 1] if i + 1 < len(api_messages) else None
            if not isinstance(nxt, dict) or nxt.get("role") != "user":
                # 这些情况 audit_api_messages 已经当作真问题报了，不重复
                continue

            result_ids = [
                b.get("tool_use_id") for b in _iter_blocks(nxt) if b.get("type") == "tool_result"
            ]
            # 只在"配对齐全、仅顺序不同"时才算可疑；缺失/孤立由 audit 负责
            if sorted(map(str, use_ids)) != sorted(map(str, result_ids)):
                continue
            if use_ids != result_ids:
                notes.append(
                    f"msg[{i+1}] 的 tool_result 顺序与 msg[{i}] 的 tool_use 不一致"
                    f"（Anthropic 允许，并行工具按完成顺序收集时属正常；"
                    f"严格按位置配对的中间网关可能拒绝）"
                )

        return notes
    except Exception as e:  # pragma: no cover - 纯兜底
        return [f"<可疑项收集失败:{type(e).__name__}: {e}>"]


def extract_upstream_request_ids(text: Any) -> List[str]:
    """
    从上游错误文案里抠出所有 `request id: xxx`。

    现场那条 400 里嵌了两个不同格式、不同时区时间戳的 id，说明请求穿过了
    多级代理。这些 id 是找网关方对账的唯一凭据，必须单独成列打出来，
    不能埋在一长串报文里。
    """
    try:
        return re.findall(r"request[\s_-]?id[:=]\s*([A-Za-z0-9_\-]+)", str(text or ""), re.I)
    except Exception:  # pragma: no cover - 纯兜底
        return []


def is_tool_format_400(message: Any) -> bool:
    """
    判断一条 400 错误文案是否属于"工具调用格式类"。

    旧实现只匹配 `tool_result` 子串，而现场真正收到的文案是
    "Invalid tool use format. | REQUEST_BODY_INVALID"，不含 `tool_result`，
    于是整段 400 容错分支根本没进去，一次失败就直接放弃了。
    这里按一组同义词判定，覆盖 Anthropic 原生文案和网关自造文案。
    """
    text = str(message or "").lower()
    return any(hint in text for hint in _TOOL_FORMAT_400_HINTS)


def log_api_status_error(
    *,
    mode: str,
    round_num: int,
    error: Any,
    api_messages: Any,
    base_url: Any,
    model: Any,
    audit_issues: Optional[List[str]] = None,
) -> None:
    """
    统一打印上游 APIStatusError 的可定位诊断信息。

    原来只有一行 `status=... message=...`，请求体零信息，排查无从下手。
    现在固定打五条，每条解决一个具体问题：
      1. 是哪种模式的第几轮失败（现场日志里"轮次"和"报错"之间隔了几十行）
      2. 走的是哪个端点、哪个模型 + 上游 request id（判断是网关还是 Anthropic）
      3. 上游响应体原文（截断）—— 错误码和细节往往只在这里
      4. 请求体结构骨架 —— 定位到具体哪条消息、哪个 block
      5. 请求体自检结果 —— 直接给出"哪个字段不合法"的结论

    Args:
        mode: 调用模式标识，当前接线的三处为 agentic / operations / rag。
              若要接到 AI 子调用（关键词提取 / 标题过滤 / 安全判定）路径上，
              必须同时自己算好 audit_issues 并传
              `audit_api_messages(msgs, expects_prefill=True)`：
              那三条路径刻意以 assistant prefill 结尾，用默认的严格自检会
              稳定产生一条假的"末条消息 role='assistant'"（详见
              audit_api_messages 的 docstring）。
        round_num: 工具循环轮次（非循环模式传 0）
        error: anthropic.APIStatusError 实例
        api_messages: 本次发出的 messages
        base_url: 客户端 base_url（用于区分官方端点和兼容网关）
        model: 模型名
        audit_issues: 已算好的自检结果；为 None 时按【工具循环】的严格规则
                      现场重算（即 expects_prefill=False）
    """
    status = getattr(error, "status_code", "?")
    message = getattr(error, "message", None) or str(error)

    # === 这里和下面"上游响应体"的 limit=600 是刻意放宽的窗口，有已知风险 ===
    # limit 不只是截断长度，它同时抬高"整段原样显示"的门槛（见 log_summary
    # 的 PREVIEW_HEAD_CHARS），所以 600 意味着任何 ≤600 字符的上游文案会原样
    # 落进日志。
    #
    # 已知风险：本项目跑在兼容网关（config.example.yaml 说明 base_url 填了就
    # 走代理/兼容服务），现场那条 "Invalid tool use format. |
    # REQUEST_BODY_INVALID (request id: ...)" 是网关自造格式而非 Anthropic
    # 原生。网关在 400 响应体里是否回显请求片段**无法假设** —— 若回显，
    # 用户提问正文就会连带进日志。
    #
    # 为什么仍然接受：
    #   1. 这两行是专门用来诊断故障的，600 有真实排查价值。降到默认 80 会把
    #      关键短语切掉（实测现场文案的 "Invalid tool use format" 起始于第 58
    #      字符、"REQUEST_BODY_INVALID" 在第 85~105），下次 400 又变成看不清。
    #   2. 400 响应体不是常态路径，只在上游拒绝请求时才打。
    #   3. 日志留在本机，而进飞书告警的 error_detail 走的是更紧的
    #      _ALERT_MSG_CHARS（200），已在源头收口。
    # 若将来实证该网关确实回显请求片段，这里应改成默认 limit 并靠 sha1 指纹
    # 跨日志串联，而不是继续放宽。
    logger.error(
        f"上游 API 错误: mode={mode}, round={round_num}, status={status}, "
        f"message={summarize_text(message, limit=600, label='upstream_msg')}"
    )

    # SDK 从响应头拿到的 request id（官方端点有，网关可能没有）
    sdk_request_id = getattr(error, "request_id", None)
    # 报文里内嵌的 request id（网关风格，可能有多个，代表穿过了多级代理）
    embedded_ids = extract_upstream_request_ids(message)
    body_text = ""
    try:
        response = getattr(error, "response", None)
        body_text = getattr(response, "text", "") or ""
    except Exception:
        body_text = "<响应体读取失败>"
    embedded_ids += [x for x in extract_upstream_request_ids(body_text) if x not in embedded_ids]

    logger.error(
        f"上游标识: base_url={base_url}, model={model}, "
        f"sdk_request_id={sdk_request_id}, 报文内嵌 request_id={embedded_ids or '无'}"
    )
    # limit=600 的取舍与风险见上面那段注释（同一个决定，不要只改一处）
    logger.error(f"上游响应体: {summarize_text(body_text, limit=600, label='resp_body')}")
    logger.error(f"请求体骨架(mode={mode},round={round_num}): {describe_api_messages(api_messages)}")

    issues = audit_api_messages(api_messages) if audit_issues is None else audit_issues
    if issues:
        logger.error(f"请求体自检: 发现 {len(issues)} 处问题 -> " + "; ".join(issues))
    else:
        logger.error(
            "请求体自检: 未发现结构问题（请求体符合 Anthropic messages API 约束，"
            "问题更可能在上游网关的转换/校验环节，请拿上面的 request_id 找网关方核对）"
        )

    # 合法但可疑的观察单独一行，并明确标注"不作为判据"：排查时有价值
    # （严格网关可能按位置配对），但它不能让人误以为请求体坏了。
    advisory = audit_api_messages_advisory(api_messages)
    if advisory:
        logger.warning(
            f"请求体可疑项（合法，不作为 400 判据）-> " + "; ".join(advisory)
        )


def try_trim_last_tool_round(api_messages: List[Dict[str, Any]]) -> tuple:
    """
    为 400 自救裁掉最后一轮工具交互，并**验证裁剪结果**后才允许重试。

    Returns:
        (trimmed, reject_reason)
        - 成功：(裁剪后的新列表, None)
        - 放弃：(None, 中文原因)  调用方必须不再重试

    === 为什么必须验证，不能闭眼裁 ===
    `[:-2]` 隐含假设"末两条正好是 assistant(tool_use) + user(tool_result)、
    且裁完剩下的仍然合法"。历史不交替时这个假设就不成立，实测两个反例：

        [user, assistant, assistant(tool_use), user(tool_result)]
            --[:-2]--> [user, assistant]        末条是 assistant
        [user, user, assistant(tool_use), user(tool_result)]
            --[:-2]--> [user, user]             连续两条 user

    两者 len 都是 4，`>= 3` 的长度门槛放行了它们 —— 那个门槛只管长度不管角色。
    前者在引用链历史重建路径下完全可能（历史末条是 assistant），后者在
    main.py 的 deferred / pending_lone_images 合并路径下可能产出。

    裁出畸形体比不裁更糟：裁完必然再 400，而自检非空又会再触发一次裁剪、
    吃掉第二份预算 —— 两份预算全废，还多烧两轮超时时间。

    注意上面两个反例裁剪【前】自检就已经报问题了，且问题位置在列表**头部**
    （msg[0..1] 角色不交替），而 `[:-2]` 只动尾部，对头部缺陷无能为力。这正是
    "验证后再重试"而不是"换一种裁法"的理由：这类请求体不是裁得对不对的问题，
    是自救动作根本救不了，该如实放弃并把结论写进告警。
    """
    if not isinstance(api_messages, list) or len(api_messages) < 3:
        return None, f"消息数不足（{len(api_messages) if isinstance(api_messages, list) else '非列表'}），无可裁空间"

    trimmed = api_messages[:-2]
    if not trimmed:
        return None, "裁剪后为空列表"

    residual = audit_api_messages(trimmed)
    if residual:
        return None, "裁剪后仍不合法 -> " + "; ".join(residual)
    return trimmed, None


def collect_assistant_content(response_content: Any) -> tuple:
    """
    收集一轮响应里的 assistant content，并剔除会让【下一轮】请求 400 的块。

    关键点：空的或纯空白的 text block 在【当轮】是无害的（SDK 不校验响应），
    它只在被回传进下一轮请求时才触发 400 "Invalid tool use format"/
    "text content blocks must be non-empty"。所以这里是唯一能拦住它的位置，
    而不是等到发请求时才发现。

    Args:
        response_content: response.content（block 列表）

    Returns:
        (assistant_content, tool_uses, notes) 三元组
        - assistant_content: 可安全回传的 content block 列表
        - tool_uses: 需要执行的 tool_use 原始 block 列表
        - notes: 被丢弃/可疑块的中文说明（调用方打 WARNING）
    """
    assistant_content: List[Dict[str, Any]] = []
    tool_uses: List[Any] = []
    notes: List[str] = []

    for idx, block in enumerate(response_content or []):
        btype = getattr(block, "type", None)

        if btype == "text":
            text = getattr(block, "text", None)
            if not isinstance(text, str) or not text.strip():
                shown = 0 if not isinstance(text, str) else len(text)
                notes.append(
                    f"block[{idx}] 是空/纯空白 text block({shown} 字符)，已丢弃"
                    f"（原样回传会让下一轮请求被 400 拒绝）"
                )
                continue
            assistant_content.append({"type": "text", "text": text})

        elif btype == "tool_use":
            tool_input = getattr(block, "input", None)
            if not isinstance(tool_input, dict):
                # 不改写：改写等于伪造模型意图，工具会拿着空参数去执行。
                # 只留痕，让自检日志把它报出来。
                notes.append(
                    f"block[{idx}] tool_use(id={_short_id(getattr(block, 'id', None))}) 的 input "
                    f"类型为 {type(tool_input).__name__} 而非对象，上游返回格式异常"
                )
            assistant_content.append({
                "type": "tool_use",
                "id": getattr(block, "id", None),
                "name": getattr(block, "name", None),
                "input": tool_input,
            })
            tool_uses.append(block)

        elif btype is not None:
            notes.append(f"block[{idx}] 类型 {btype} 未被处理，不回传")

    return assistant_content, tool_uses, notes


class AnthropicProvider(AIProvider):
    """Anthropic Claude Provider"""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        base_url: str = None,
        timeout: int = 90,
        feedback_manager=None,
    ):
        """
        Args:
            api_key: Anthropic API Key
            model: 模型名称
            base_url: API 基础 URL（可选，用于代理或兼容服务）
            timeout: 请求超时时间（秒）
            feedback_manager: 反馈管理器实例（可选）
        """
        super().__init__()
        self.model = model
        self.timeout = timeout
        self.feedback_manager = feedback_manager

        # 初始化 Anthropic 客户端
        client_kwargs = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
            logger.info(f"使用自定义 base_url: {base_url}")

        self.client = anthropic.Anthropic(**client_kwargs)
        logger.info(f"Anthropic Provider 初始化: model={model}, base_url={base_url or '默认'}")

        # Git 工具（由外部注入）
        self.git_tools = None
        self.git_tools_enabled = False
        self.branch_hint = ""  # 版本号→分支映射提示
        self.max_rounds = 6  # Agentic 最大轮数，由外部注入覆盖
        self.timeout_mode = "time"  # 超时模式: time / rounds
        self.max_time = 300  # 总时间限制（秒）
        self.tool_timeout = 30  # 单个工具超时（秒），由外部配置注入
        self.repo_manager = None  # 多仓库管理器（由外部注入）

        # 注意：已彻底移除"会话级粘性"机制（原 _agentic_sessions / _operations_sessions）。
        # 原设计让"群里有人用过一次工具/运维模式，该群 session 就永久继承该模式"，
        # 由于 session_id == 飞书 chat_id（房间级、永久、跨用户共享），导致：
        #   - 跨用户污染：甲用 /运维 后，乙在同群问普通问题也被拖进运维模式并被鉴权拒绝
        #   - 跨话题污染：一次运维查询后，同群所有后续话题永久停留在运维模式
        #   - 永不过期：set 只增不减，进程不重启就一直粘着
        # 现在模式判定只看"当前这条消息本身"，不看群里以前发生过什么。
        # 多轮话题延续改由用户显式「引用」上一条回复来表达（引用链 root_id），
        # 无引用 = 全新对话，从头开始，不粘连任何历史。

        # 运维工具（由外部注入）
        self.operations_tools = None
        self.operations_enabled = False

    def set_git_tools(self, git_tools, enabled: bool = True, branch_hint: str = ""):
        """
        设置 git 工具（用于代码排查）

        Args:
            git_tools: GitTools 实例
            enabled: 是否启用
            branch_hint: 版本号→分支映射提示（注入 system prompt）
        """
        self.git_tools = git_tools
        self.git_tools_enabled = enabled
        self.branch_hint = branch_hint
        logger.info(f"Git 工具已{'启用' if enabled else '禁用'}，branch_hint={'已配置' if branch_hint else '未配置'}")

    def set_repo_manager(self, repo_manager):
        """
        设置多仓库管理器（用于多仓库代码排查）

        Args:
            repo_manager: RepoManager 实例
        """
        self.repo_manager = repo_manager
        self.git_tools = repo_manager.current
        self.git_tools_enabled = True
        logger.info(f"多仓库管理器已注入: 当前仓库={repo_manager.current_repo_name}")

    def set_operations_tools(self, operations_tools, enabled: bool = True):
        """
        设置运维工具（用于运维操作）

        Args:
            operations_tools: OperationsTools 实例
            enabled: 是否启用
        """
        self.operations_tools = operations_tools
        self.operations_enabled = enabled
        logger.info(f"运维工具已{'启用' if enabled else '禁用'}")

    def _call_subcall(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        prefill: str,
        subcall: str,
    ) -> str:
        """
        执行一次"AI 子调用"（关键词提取 / 标题过滤 / 安全判定）并返回完整输出文本。

        messages 末尾是 assistant prefill（强制输出格式起手）。prefill 内容不会出现在
        响应里，这里拼回去再交给解析器。部分 OpenAI 兼容网关不接受"以 assistant 结尾"
        的消息数组，因此首次失败时去掉 prefill 重试一次，避免整个子调用静默降级。

        temperature=0：结构化抽取任务不需要随机性。

        === 本路径刻意不接 audit_api_messages / log_api_status_error ===
        将来若要给子调用补上 400 诊断，**必须**传 `expects_prefill=True`，
        并且绝不能把结果接进任何裁剪/重试决策。原因：这里的 messages 故意
        以 assistant prefill 结尾，默认（严格）自检会稳定报一条假的
        "末条消息 role='assistant'"；而工具循环里的裁剪条件是
        `(tool_format_hit or post_issues)` 非空即成立，照默认值调一次就会
        按假阳性稳定误裁。目前 expects_prefill=True 在 src/ 下零调用点，
        就是因为这条路径还没接诊断 —— 不是忘了传。

        Args:
            system_prompt: system prompt（钉死角色与输出契约）
            messages: 由 build_*_messages 构造的消息列表（末项为 prefill）
            max_tokens: 输出上限（结构化输出不需要大值，顺手掐断长篇解释）
            prefill: assistant 预填文本
            subcall: 子调用标识（仅用于日志）

        Returns:
            完整输出文本（含 prefill 前缀）

        Raises:
            Exception: 两次调用均失败时向上抛出，由各子调用自行降级
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                messages=messages,
                system=system_prompt,
            )
            return _join_prefill(prefill, response.content[0].text)
        except Exception as e:
            if not prefill or len(messages) < 2:
                raise
            logger.debug(f"子调用 prefill 方式失败，去掉 prefill 重试一次: subcall={subcall}, error={e}")
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0,
                messages=messages[:-1],
                system=system_prompt,
            )
            return response.content[0].text

    def extract_keywords(self, query_text: str) -> KeywordExtractionResult:
        """
        使用 Claude 从用户查询中提取搜索关键词 + 判断是否通用技术问题

        用户原话用 XML 定界标签包成"待处理数据"（build_keyword_extraction_messages），
        并用 assistant prefill 预填 JSON 起手，防止模型把用户的提问当成指令转去作答。

        Args:
            query_text: 用户查询文本

        Returns:
            KeywordExtractionResult（调用或解析失败时为保守降级值）
        """
        try:
            text = self._call_subcall(
                system_prompt=_KEYWORD_EXTRACTION_SYSTEM_PROMPT,
                messages=build_keyword_extraction_messages(query_text),
                max_tokens=200,
                prefill=_KEYWORD_EXTRACTION_PREFILL,
                subcall="keyword_extraction",
            )
            result = _parse_keyword_extraction_response(text, query_text[:50])
            logger.info(
                f"Claude 关键词提取: keywords={result.keywords}, "
                f"generic={result.is_generic_tech}, query='{query_text[:50]}'"
            )
            return result
        except Exception as e:
            log_subcall_degraded("keyword_extraction", f"API 调用失败，返回降级值: {e}", "", query_text[:50])
            return KeywordExtractionResult(keywords=[], is_generic_tech=False)

    def classify_prompt_safety(self, query_text: str) -> PromptSafetyResult:
        """
        使用 Claude 判断用户输入是否为提示词攻击 / 敏感信息窃取。

        待判定文本用 XML 定界标签包成"待审计素材"，配合 assistant prefill 预填 JSON
        起手，防止模型把素材当成提问转去作答（真实故障：模型回了"你还没贴线程信息呢"，
        导致安全判定 JSON 解析失败、全量 fail-open）。

        判定失败时 fail-open（返回放行值），避免误伤正常用户。

        Args:
            query_text: 用户查询文本

        Returns:
            PromptSafetyResult（调用或解析失败时 is_attack=False）
        """
        try:
            text = self._call_subcall(
                system_prompt=_PROMPT_SAFETY_SYSTEM_PROMPT,
                messages=build_prompt_safety_messages(query_text),
                # 300 而非 200：判定为攻击时 reason 偏长，撞上限会让 JSON 截断、
                # 解析失败后 fail-open 放行真实攻击。system prompt 已限 reason 30 字，
                # 这里留出余量做第二道保险（截断仍可由 _close_truncated_json 兜底）。
                max_tokens=300,
                prefill=_PROMPT_SAFETY_PREFILL,
                subcall="prompt_safety",
            )
            result = _parse_prompt_safety_response(text, query_text[:50])
            if result.is_attack:
                logger.info(
                    f"Claude 安全判定: attack={result.is_attack}, type={result.attack_type}, "
                    f"query='{query_text[:50]}'"
                )
            return result
        except Exception as e:
            # fail-open：判定链路异常不阻断正常用户
            log_subcall_degraded("prompt_safety", f"API 调用失败，放行本次请求: {e}", "", query_text[:50])
            return PromptSafetyResult(is_attack=False, attack_type="none", reason="")

    @staticmethod
    def _inherited_chain_mode(messages: List[Message]) -> str:
        """
        从引用链历史中取最近一次实际使用过的模式，用于"引用追问自动延续模式"。

        判定依据是 assistant 消息落盘的 metadata.mode（实际执行结果），而不是
        重新扫描历史文本猜测——文本里出现"查代码"未必真进过工具模式。
        取最近一轮而非任意一轮：同一条链上用户可能从排查聊到运维，最近一次
        才代表当前话题状态。

        作用域限定在引用链（root_id）内，与已废弃的"会话级粘性"有本质区别：
        粘性绑 session_id（=群 chat_id，房间级、跨用户共享），导致 A 用过运维后
        群里 B 问普通问题也被拖进运维；而引用链由用户显式引用行为构成、话题内聚，
        B 不引用就不共享 root_id，天然不会被污染。详见 operations-mode-sticky-session-bug。

        Args:
            messages: 消息列表（引用链重建后的完整历史）

        Returns:
            "agentic" / "operations" / ""（无可继承模式）
        """
        for msg in reversed(messages):
            if msg.role != "assistant":
                continue
            mode = (msg.metadata or {}).get("mode", "")
            if mode in ("agentic", "operations"):
                return mode
        return ""

    def _should_use_agentic_mode(self, messages: List[Message], session_id: Optional[str] = None) -> bool:
        """
        判断是否应该使用 Agentic 模式（工具调用）

        触发条件（命中任一即进入）：
        1. Git 工具已启用（前提）
        2. 显式斜杠指令（/排查、/查代码、/code）
        3. 工具意图关键词（"查代码""看源码""排查"等自然语言）
        4. 图片消息（日志截图）或日志文件附件
        5. 引用链继承：本条无任何触发词，但所引用的链此前用过 Agentic 模式

        条件 5 的优先级最低——"当前消息有触发词以当前为准，没有才继承历史"。
        因此从 RAG 链升级到排查/运维，用户在引用时显式带上触发词即可；
        反向不需要"退出指令"，因为排查/运维模式同样会注入 RAG 文档检索结果，
        是普通问答的超集，没有"需要退回普通模式"的场景。

        与已废弃的"会话级粘性"的区别见 _inherited_chain_mode 文档字符串。

        Args:
            messages: 消息列表
            session_id: 会话 ID（保留参数仅为签名兼容，不用于粘性判断）

        Returns:
            是否使用 Agentic 模式
        """
        if not self.git_tools_enabled or not self.git_tools:
            return False

        last_user_text = self._extract_last_user_text(messages)

        # 1. 显式斜杠指令检测
        if self._has_explicit_command(last_user_text):
            logger.info("触发 Agentic 模式：显式指令")
            return True

        # 2. 工具意图关键词检测（自然语言）
        if self._has_tool_intent_keyword(last_user_text):
            logger.info("触发 Agentic 模式：命中工具意图关键词（如查代码/看源码/排查）")
            return True

        # 3. 当前这轮带图片（日志截图）或日志文件附件。
        #    只看最后一条 user 消息：引用链历史里的图片属于往轮，不应反复触发。
        if self._current_turn_has_attachment(messages):
            logger.info("触发 Agentic 模式：当前消息带图片/日志文件")
            return True

        # 4. 引用链继承（优先级最低）：本条没有任何触发词，
        #    但用户引用的这条链此前就在用工具模式，延续之，免去重复输入触发词。
        inherited = self._inherited_chain_mode(messages)
        if inherited == "agentic":
            logger.info("触发 Agentic 模式：引用链继承（历史轮次曾使用工具模式）")
            return True

        return False

    @staticmethod
    def _content_to_api_parts(content) -> List[Dict[str, Any]]:
        """
        把单个 Content 转成 Anthropic API 的 content part 列表。

        三种模式（Agentic/Operations/RAG）共用，避免各写一遍导致某个分支
        漏掉新增类型（log_file 就属于新增类型，漏掉会静默丢附件）。

        Args:
            content: Content 对象

        Returns:
            API content part 列表（不支持的类型返回空列表）
        """
        if content.type == "text":
            return [{"type": "text", "text": content.data}]

        if content.type == "image" and isinstance(content.data, dict):
            return [{
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": content.data.get("media_type", "image/png"),
                    "data": content.data["data"],
                }
            }]

        if content.type == "log_file" and isinstance(content.data, dict):
            # 日志文件以文本形式注入：小文件带正文，大文件/压缩包只给路径，
            # 由 AI 用代码排查工具按需读取，避免整包日志塞爆上下文。
            name = content.data.get("name", "attachment")
            path = content.data.get("path", "")
            note = content.data.get("note", "")
            text = content.data.get("text", "")
            parts = [f"[用户上传的日志文件] 文件名: {name}"]
            if path:
                parts.append(f"本地路径: {path}")
            if note:
                parts.append(note)
            if text:
                parts.append(f"文件内容:\n{text}")
            elif path and not note:
                parts.append("（内容未内联，可用代码排查工具读取上述路径）")
            return [{"type": "text", "text": "\n".join(parts)}]

        return []

    @staticmethod
    def _current_turn_has_attachment(messages: List[Message]) -> bool:
        """
        判断"当前这一轮"是否带图片或日志文件附件。

        只检查末尾连续的 user 消息，不扫描整个 messages：引用链重建后历史轮里
        也有图片，若全量扫描会让一条曾发过截图的链永久停在排查模式。

        Args:
            messages: 消息列表

        Returns:
            当前轮是否含 image 或 log_file 内容块
        """
        for msg in reversed(messages):
            if msg.role != "user":
                break
            for content in msg.content:
                if content.type == "log_file":
                    return True
                if content.type == "image":
                    # 回溯拉来的历史图片带 from_history 标记，不算当前轮的附件：
                    # 否则群里别人早前发的无关截图会把普通提问拖进排查模式。
                    if isinstance(content.data, dict) and content.data.get("from_history"):
                        continue
                    return True
        return False

    def _has_explicit_operations_command(self, messages: List[Message]) -> bool:
        """
        当前消息是否显式带 /运维 或 /ops 前缀（不含引用链继承）。

        单独拆出来供 _send_with_context 做优先级判定：显式指令必须优先于
        引用链继承，否则一条运维链会吃掉后续显式写了 /查代码 的追问。

        Args:
            messages: 消息列表

        Returns:
            当前消息是否显式请求运维模式
        """
        if not self.operations_enabled or not self.operations_tools:
            return False
        text_stripped = self._extract_last_user_text(messages).strip()
        return "/运维" in text_stripped or "/ops" in text_stripped

    def _should_use_operations_mode(self, messages: List[Message], session_id: Optional[str] = None) -> bool:
        """
        判断是否应该使用 Operations 模式（运维工具调用）

        触发条件：
        1. 当前这条消息显式带 /运维 或 /ops 前缀
        2. 引用链继承：本条无前缀，但所引用的链此前用过 Operations 模式

        不存在"会话级粘性"——不看群里以前是否用过运维模式，只看当前消息 +
        用户显式引用的那条链。作用域区别见 _inherited_chain_mode 文档字符串。

        安全性：继承只决定"走哪个模式"，不放宽权限。运维白名单在进入模式后
        按当前发言人重新校验（见 _send_with_context_operations 的鉴权门），
        因此 B 引用 A 的运维链追问，仍会被白名单拦住，不构成绕过。

        Args:
            messages: 消息列表
            session_id: 会话 ID（保留参数仅为签名兼容，不再用于粘性判断）

        Returns:
            是否使用 Operations 模式
        """
        if not self.operations_enabled or not self.operations_tools:
            logger.debug(f"运维模式未启用: operations_enabled={self.operations_enabled}, operations_tools={self.operations_tools is not None}")
            return False

        last_user_text = self._extract_last_user_text(messages)

        # 1. 当前消息显式带 /运维 或 /ops（支持前面有 @ 提及等前缀）
        text_stripped = last_user_text.strip()
        if "/运维" in text_stripped or "/ops" in text_stripped:
            return True

        # 2. 引用链继承：免去每轮运维追问都要重复输入 /运维
        if self._inherited_chain_mode(messages) == "operations":
            logger.info("触发 Operations 模式：引用链继承（历史轮次曾使用运维模式）")
            return True

        return False

    def _has_explicit_command(self, text: str) -> bool:
        """
        检测显式斜杠指令

        Args:
            text: 用户消息文本

        Returns:
            是否包含显式指令
        """
        explicit_commands = ["/code", "/排查", "/查代码","查代码"]
        text_lower = text.lower()
        return any(cmd in text_lower for cmd in explicit_commands)

    def _has_tool_intent_keyword(self, text: str) -> bool:
        """
        检测自然语言中的"工具/代码意图"关键词。

        用于满足诉求：普通文档检索模式下，用户在（引用）追问中携带"查代码"等关键词时，
        自动进入工具使用模式；文档检索结果仍会注入上下文辅助判断。

        关键词选取偏保守，均为明确指向"查看/排查代码、源码、实现、调用链"的表达，
        避免误伤纯文档查询（如"报错怎么解决""某功能怎么用"）。

        Args:
            text: 用户消息文本

        Returns:
            是否命中工具意图关键词
        """
        if not text:
            return False
        text_lower = text.lower()
        tool_intent_keywords = [
            "查代码", "查下代码", "查一下代码", "查查代码", "查看代码", "看代码", "看下代码",
            "看源码", "查源码", "看看源码", "读源码", "读代码",
            "查实现", "看实现", "实现逻辑", "源码实现", "代码实现",
            "调用链", "调用关系", "谁调用", "调用了", "在哪里定义", "定义在哪",
            "排查", "排查一下", "排查下", "定位问题", "定位一下", "定位下", "trace",
            "查提交", "查commit", "改动历史", "谁改的", "哪次提交",
        ]
        return any(kw in text_lower for kw in tool_intent_keywords)

    def _send_with_context(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
    ) -> tuple[str, dict]:
        """
        发送消息到 Claude 并获取回复

        根据消息内容自动选择模式：
        - /运维 前缀 → Operations 模式（运维工具调用）
        - 有图片/日志文件或排查关键词 → Agentic 模式（代码排查工具调用）
        - 以上都无、但引用的链此前用过某模式 → 继承该模式
        - 其他 → 标准 RAG 模式

        判定顺序遵循"当前消息优先于引用链继承"：先看当前消息的显式意图，
        只有当前消息没有任何触发词时才回落到继承。否则一条用过 /运维 的链
        会让后续显式写了 /查代码 的追问仍被路由进运维模式。

        Returns:
            (reply_text, metadata) 元组
            metadata 包含：tool_rounds（Agentic/Operations）或 doc_count（RAG）等
        """
        # 第一优先级：当前消息的显式意图（不看引用链历史）
        if self._has_explicit_operations_command(messages):
            logger.info("使用 Operations 模式（当前消息显式 /运维）")
            return self._send_with_context_operations(
                messages,
                doc_context,
                session_id,
                max_rounds=self.max_rounds,
                timeout_mode=self.timeout_mode,
                max_time=self.max_time
            )

        # 判断是否使用 Agentic 模式（当前消息触发词/附件，或引用链继承）
        if self._should_use_agentic_mode(messages, session_id):
            logger.info("使用 Agentic 模式（工具调用）")
            return self._send_with_context_agentic(
                messages,
                doc_context,
                session_id,
                max_rounds=self.max_rounds,
                timeout_mode=self.timeout_mode,
                max_time=self.max_time
            )

        # 引用链继承运维模式（当前消息无任何触发词时才走到这里）
        if self._should_use_operations_mode(messages, session_id):
            logger.info("使用 Operations 模式（引用链继承）")
            return self._send_with_context_operations(
                messages,
                doc_context,
                session_id,
                max_rounds=self.max_rounds,
                timeout_mode=self.timeout_mode,
                max_time=self.max_time
            )

        # 标准 RAG 模式
        logger.info("使用标准 RAG 模式")
        return self._send_with_context_standard(messages, doc_context, session_id)

    def _send_with_context_agentic(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
        max_rounds: int = 6,
        timeout_mode: str = "time",
        max_time: int = 300,
    ) -> tuple[str, dict]:
        """
        Agentic 模式：支持工具调用的多轮对话

        Args:
            messages: 消息列表
            doc_context: 文档上下文
            session_id: 会话 ID
            max_rounds: 最大工具调用轮数
            timeout_mode: 超时模式 "time" / "rounds"
            max_time: 总时间限制（秒），timeout_mode="time" 时生效

        Returns:
            (reply_text, metadata) 元组
            metadata 包含 tool_rounds（实际工具调用轮数）
        """
        from ai_assistant.tools.git_tools import GIT_TOOLS_SCHEMA

        # 构建工具列表（多仓库时追加 switch_repo 工具）
        tools_schema = list(GIT_TOOLS_SCHEMA)
        if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
            tools_schema.append({
                "name": "switch_repo",
                "description": "切换当前代码排查的目标仓库。切换后，search_code、find_files、read_file、list_dir、run_git_command 等工具将在新仓库中执行。注意：本工具会改变全局状态，必须单独一轮调用，不要和查询类工具放在同一轮。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_name": {
                            "type": "string",
                            "description": "目标仓库名称"
                        }
                    },
                    "required": ["repo_name"]
                }
            })

        # 转换消息格式
        api_messages = []
        for msg in messages:
            content_parts = []
            for content in msg.content:
                content_parts.extend(self._content_to_api_parts(content))

            if content_parts:
                api_messages.append({"role": msg.role, "content": content_parts})

        # 构建 system prompt（Agentic 排查指引）
        system_parts = [
            "你是一个智能代码排查助手，帮助用户分析日志并定位代码问题。",
            "",
            "【重要】当用户消息里包含图片时，你能完整看到图片内容（包括截图中的日志文字、报错信息、代码片段等）。"
            "不要说'看不到图片'或'图片被省略'——你能看到，请直接分析图片里的内容。",
            "",
            "工作流程：",
            "1. 【默认先动手排查，不要先反问】用户发日志截图就是要你分析。",
            "   - 未指定版本 → 直接用仓库的默认分支（default_ref）排查，不必先问版本",
            "   - 未指定组件 → 从日志的包名/类名/路径推断仓库",
            "     （如 com.iflytek.icourt + ts-service 路径 → ts-service），",
            "     推断不出再看仓库描述里的默认仓库",
            "   - 只有在\"信息缺失到根本无法动手\"时才提问",
            "     （例如截图完全无法辨认、或同一异常在多个仓库都存在且无法区分）",
            "   - 需要澄清时也要先给出已能得到的分析结论，把问题放在最后，不要只回一堆问题",
            "2. 从日志中提取关键信息（异常类名、错误信息、堆栈类名+行号）",
            "3. 用 search_code / find_files 定位异常抛出点（可与第 2 步的多个关键词并行搜索）",
            "4. 用 read_file 读取定位到的文件，查看上下文代码（建议读取抛错位置前后 30 行）",
            "5. 沿调用链向上追（谁调用了它、超时/参数从哪来），必要时用 run_git_command 看改动历史",
            "6. 综合日志和代码，给出根因分析、代码定位（文件:行号）、修复建议",
            "",
            "【回复质量要求 —— 很重要】",
            "- 日志里已有的信息（异常类型、堆栈行号、方法名）只是起点，不是结论。",
            "  仅仅把日志内容复述一遍、不去读代码，是不合格的回复",
            "- 必须真正打开代码验证：抛错那一行在做什么、超时时间配在哪、",
            "  调用的下游地址/参数从哪来、有没有重试和兜底",
            "- 给结论时要落到具体文件:行号，并说明\"为什么会走到这里\"",
            "- 如果排查后仍无法确定根因，说明已排除了什么、还剩哪些可能、下一步怎么验证",
            "",
            "【何时停止排查 —— 很重要】",
            "- 找到根因（能回答\"为什么会这样\"+\"怎么改\"）就停，不要继续验证边缘细节",
            "- 示例：已读到\"法庭列表的 isInternetCourt 字段从 AuthorizationService.getIsInternetCourt() 注入\"",
            "  且知道这个方法的双重配置逻辑（授权方案 + 配置开关）→ 够了，停",
            "  不需要继续查：配置表的建表 SQL、git 历史为什么从 roomCode 改成 deviceCode、前端怎么调用",
            "- 如果还剩时间但问题已定位，给出结论+修复建议即可，不要\"为了用满时间\"去补充无关细节",
            "",
            "工具使用最佳实践（重要）：",
            "",
            "【并行调用 —— 这是提速的关键，请尽量用】",
            "- search_code / find_files / read_file / list_dir 都是只读工具，一次返回多个",
            "  tool_use 会并行执行，耗时约等于最慢的那一个，而不是相加",
            "- 每一轮都先想清楚\"这一步我需要哪几份信息\"，一次全部发出，不要一个个试",
            "  示例场景：",
            "  · 不确定用哪个关键词：同时搜 'servicelog'、'ServiceLog'、'internet_service_log'",
            "  · 前后端一起查：同时搜后端字段名和前端调用名",
            "  · 多个独立文件：同时 read_file 多个无依赖关系的文件",
            "- 注意：switch_repo 会改变当前仓库，必须单独一轮调用，不要和查询混在一起",
            "",
            "【search_code 策略】",
            "- 支持正则；若正则语法无效会自动退回字面量搜索，不必自我审查语法",
            "- 想看匹配点周围的代码，直接加 context 参数，不要再单独调 read_file",
            "  ✅ 高效：search_code('judgeUseSimplifyNum', context=10) # 一次拿到匹配+上下文",
            "  ❌ 低效：search_code(...) 拿到行号 → 再 read_file 读那一段 # 多花一整轮",
            "- 搜不到时，先换更短、更可能出现在代码里的关键词（函数名/字段名/API 路径），",
            "  而不是反复搜界面文案。界面文案常被拆分或写在别处，搜不到不代表功能不存在",
            "",
            "【find_files 策略（只知道文件名时用它）】",
            "- 只知道文件名、不知道路径：find_files('RecordList.vue') 直接拿到完整路径",
            "- 不要用 list_dir 一层层试探目录，那会浪费很多轮次",
            "",
            "【path_filter 用法】",
            "- 可以直接给文件名（'RecordList.vue'），系统会自动匹配任意目录下的该文件",
            "- 也可用通配符（'*.java'）或目录（'src/main/java'）",
            "- 重要：带 path_filter 搜不到时，去掉 path_filter 再搜一次全仓库确认，",
            "  不要仅凭一次带过滤的空结果就断定代码里没有",
            "",
            "【read_file 策略】",
            "- 大文件（>300行）先用 search_code 定位行号，再用 start_line/end_line 读上下文",
            "- 小文件（<200行）直接全读；只看注解或类定义时只读前 50 行",
            "",
            "【查提交历史/作者】",
            "- 用 run_git_command，例如：",
            "  · 谁改的、什么时候：['log', '--oneline', '-10', '--', '<文件路径>']",
            "  · 某几行是谁写的：['blame', '-L', '520,540', '--', '<文件路径>']",
            "  · 按提交信息找改动：['log', '--all', '--grep=log4j', '--oneline']",
            "- 不要回复\"我无法查看 git 历史\"，你有这个能力",
            "",
            "注意事项：",
            "- 必须始终使用中文回答",
            "- 代码定位要精确到文件名和行号",
            "- 如果无法定位问题，诚实告知并给出可能的排查方向",
            "- 工具调用失败时尝试其他搜索关键词，但避免重复相同的失败策略",
            "- 优先使用并行工具调用减少总轮数，提升响应速度"
        ]

        # 注入版本号→分支映射提示
        if self.branch_hint:
            system_parts.append("")
            system_parts.append("版本号与分支映射规则：")
            system_parts.append(self.branch_hint)

        # 注入飞书文档（如果有）
        if doc_context:
            system_parts.append("")
            system_parts.append(doc_context)

        # 注入负反馈提示
        feedback_prompt = self._build_feedback_prompt(session_id)
        if feedback_prompt:
            system_parts.append(feedback_prompt)

        # 注入多仓库描述（帮助 AI 判断该去哪个仓库查）
        if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
            system_parts.append("")
            system_parts.append(self.repo_manager.get_repo_descriptions())

        system_prompt = "\n".join(system_parts)

        # Agentic 循环
        import time
        start_time = time.time()
        round_num = 0

        # 智能搜索失败检测
        search_fail_count = 0
        repo_switch_count = 0

        # 400 容错预算：请求体被拒时允许"裁掉最后一轮再继续"的次数。
        # 必须有上限 —— 否则一个持续返回 400 的上游会让循环一路裁到只剩首条
        # user 消息，白跑满 600s 超时，用户等 10 分钟拿到空结论。
        body_retry_budget = 2
        # 裁剪自救被拒的原因，会带进最终告警（为空表示没走到那一步）
        trim_reject_reason = None

        while True:
            round_num += 1

            # 检查超时条件
            elapsed = time.time() - start_time
            if timeout_mode == "time":
                if elapsed > max_time:
                    logger.warning(f"达到总时间限制 {max_time}s，已执行 {round_num-1} 轮，耗时 {elapsed:.1f}s")
                    break
                logger.info(f"Agentic 轮次 {round_num}/∞, 已耗时 {elapsed:.1f}s/{max_time}s")
            elif timeout_mode == "rounds":
                if round_num > max_rounds:
                    logger.warning(f"达到最大轮数 {max_rounds}，返回当前结果")
                    break
                logger.info(f"Agentic 轮次 {round_num}/{max_rounds}")
            else:
                logger.info(f"Agentic 轮次 {round_num}")

            # 发请求前自检：请求体真坏了要立刻知道是哪个字段，而不是等上游
            # 回一句抽象的 400 再去猜。第 2 轮起才有工具回传结构，才值得打骨架。
            pre_issues = audit_api_messages(api_messages)
            if pre_issues:
                logger.warning(
                    f"请求体自检发现 {len(pre_issues)} 处问题（第 {round_num} 轮，仍将尝试发送）-> "
                    + "; ".join(pre_issues)
                )
                logger.warning(f"请求体骨架(round={round_num}): {describe_api_messages(api_messages)}")
            elif round_num > 1:
                logger.debug(f"请求体骨架(round={round_num}): {describe_api_messages(api_messages)}")

            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=api_messages,
                    tools=tools_schema,
                )

                # input_tokens 必须跟着打：兼容网关常把它回成 0 或固定小值
                # （现场 3442 字符提问 + 长 system prompt 却报 input:4），
                # 这时 token 数不可信，排查要改看请求体骨架里的字符数。
                logger.info(
                    f"Claude 响应: stop_reason={response.stop_reason}, "
                    f"tokens(input:{response.usage.input_tokens}, output:{response.usage.output_tokens})"
                )

                # 收集本轮的 assistant 消息内容（顺带剔除会让下一轮 400 的空 text block）
                assistant_content, tool_uses, collect_notes = collect_assistant_content(response.content)
                for note in collect_notes:
                    logger.warning(f"第 {round_num} 轮响应内容异常: {note}")

                # 将 assistant 消息加入对话历史。
                # content 为空数组会被上游以 400 拒绝，所以必须兜一个占位 text：
                # 模型可能只回了一个空 text block（已被上面剔除），若原样 append
                # 一个空数组，下一轮请求必挂，且错误文案完全看不出是这里的问题。
                # 这里不必再分 tool_uses 是否为空：collect_assistant_content 对
                # tool_use 是无条件 append 的（input 不是 dict 时也只记 note 不跳过），
                # 所以 tool_uses 非空 ⇒ assistant_content 必非空。能走到这里只有
                # "模型什么都没回 / 只回了空 text block"一种情况。
                if not assistant_content:
                    logger.warning(f"第 {round_num} 轮响应 content 为空，补占位文本以免下一轮请求 400")
                    assistant_content = [{"type": "text", "text": "（本轮无输出）"}]
                api_messages.append({"role": "assistant", "content": assistant_content})

                # 如果没有工具调用,返回最终文本
                if response.stop_reason == "end_turn" or not tool_uses:
                    final_text = ""
                    for block in response.content:
                        if hasattr(block, 'text'):
                            final_text += block.text
                    logger.info(f"Agentic 完成: 总轮数={round_num}, 最终回复={len(final_text)}字符")

                    # 构建 metadata（Agentic 模式）
                    metadata = {
                        "mode": "agentic",
                        "tool_rounds": round_num,
                    }

                    return (final_text or "抱歉，未能生成有效回复。"), metadata

                # 执行工具调用（search_code 可并行，其他串行）
                tool_results = []

                # 纯读工具可安全并行（无共享状态）；switch_repo 会改变当前仓库，必须串行
                PARALLEL_SAFE = {"search_code", "find_files", "read_file", "list_dir"}
                all_readonly = all(tool_use.name in PARALLEL_SAFE for tool_use in tool_uses)
                can_parallel = all_readonly and len(tool_uses) >= 2

                if can_parallel:
                    # 并行执行多个只读查询
                    names = ", ".join(t.name for t in tool_uses)
                    logger.info(f"并行执行 {len(tool_uses)} 个只读工具: {names}")
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tool_uses), 5)) as executor:
                        futures = {}
                        for tool_use in tool_uses:
                            future = executor.submit(self._execute_tool, tool_use.name, tool_use.input)
                            futures[future] = tool_use

                        for future in concurrent.futures.as_completed(futures):
                            tool_use = futures[future]
                            tool_name = tool_use.name
                            tool_input = tool_use.input
                            try:
                                result = future.result(timeout=30)
                                result_str = json.dumps(result, ensure_ascii=False)
                                if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
                                    result_str = f"[当前仓库: {self.repo_manager.current_repo_name}] {result_str}"
                                # 空 content 的 tool_result 会被上游以 400 拒绝
                                if not result_str.strip():
                                    logger.warning(f"工具 {tool_name} 结果序列化为空，补占位文本")
                                    result_str = "（工具无返回内容）"
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "content": result_str
                                })
                                logger.info(f"工具 {tool_name} 完成: {len(result_str)} 字符")
                            except Exception as e:
                                logger.error(f"并行工具 {tool_name} 失败: {e}")
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "content": json.dumps({"error": str(e)}, ensure_ascii=False),
                                    "is_error": True
                                })
                else:
                    # 串行执行（包含非搜索工具或单个工具）
                    for tool_use in tool_uses:
                        tool_name = tool_use.name
                        tool_input = tool_use.input
                        logger.info(f"执行工具: {tool_name}({tool_input})")

                        try:
                            result = self._execute_tool(tool_name, tool_input)
                            result_str = json.dumps(result, ensure_ascii=False)
                            # 多仓库模式下，工具结果前缀加当前仓库名
                            if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
                                result_str = f"[当前仓库: {self.repo_manager.current_repo_name}] {result_str}"
                            # 空 content 的 tool_result 会被上游以 400 拒绝
                            if not result_str.strip():
                                logger.warning(f"工具 {tool_name} 结果序列化为空，补占位文本")
                                result_str = "（工具无返回内容）"
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": result_str
                            })
                            # 只打规模和摘要：这里曾把 6677 字符的 Java 源码整段
                            # 打进日志，既违反「不记录完整内容」，也把流程日志冲没了。
                            # 与并行分支保持一致（原先并行只打长度、串行打全文）。
                            logger.info(f"工具 {tool_name} 完成: {len(result_str)} 字符")
                            logger.debug("工具 {} 结果: {}", tool_name, summarize_obj(result))

                            # === 智能检测逻辑（仅监控，不注入额外消息避免API错误）===
                            # 1. 检测连续搜索失败
                            if tool_name == "search_code":
                                if isinstance(result, dict) and (not result.get("results") or len(result.get("results", [])) == 0):
                                    search_fail_count += 1
                                    if search_fail_count >= 3:
                                        logger.warning(f"⚠️ 连续{search_fail_count}次搜索失败，AI可能需要调整策略")
                                        search_fail_count = 0  # 重置避免重复日志
                                else:
                                    search_fail_count = 0  # 搜索成功，重置计数

                            # 2. 检测仓库频繁切换
                            if tool_name == "switch_repo":
                                repo_switch_count += 1
                                if repo_switch_count >= 3:
                                    logger.warning(f"⚠️ 第{repo_switch_count}次切换仓库，性能损耗较大")

                        except Exception as e:
                            logger.error(f"工具 {tool_name} 执行失败: {e}")
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": json.dumps({"error": str(e)}, ensure_ascii=False),
                                "is_error": True
                            })

                # 将工具结果加入对话历史
                api_messages.append({"role": "user", "content": tool_results})

            except anthropic.APITimeoutError as e:
                logger.error("Anthropic API 请求超时")
                return "⏱️ AI 服务响应超时，请稍后重试", {
                    "mode": "agentic", "error": "timeout",
                    "error_detail": f"APITimeoutError: {e}",
                }
            except anthropic.APIConnectionError as e:
                logger.error(f"Anthropic API 连接失败: {e}")
                return "🔌 AI 服务连接失败，请检查网络或稍后重试", {
                    "mode": "agentic", "error": "connection",
                    "error_detail": f"APIConnectionError: {e}",
                }
            except anthropic.APIStatusError as e:
                post_issues = audit_api_messages(api_messages)
                log_api_status_error(
                    mode="agentic",
                    round_num=round_num,
                    error=e,
                    api_messages=api_messages,
                    base_url=self.client.base_url,
                    model=self.model,
                    audit_issues=post_issues,
                )

                # 400 = 请求体被拒。工具多轮回传是本项目唯一会动态拼请求体的地方，
                # 所以裁掉最后一轮（assistant 的 tool_use + user 的 tool_result）
                # 再继续，是唯一可能自救的动作。
                #
                # 旧判定是 `"tool_result" in message.lower()`，而现场真正收到的
                # 文案是 "Invalid tool use format. | REQUEST_BODY_INVALID"，
                # 不含 tool_result 子串 → 回退分支从未进入，一次 400 直接放弃。
                # 现在改为：工具格式类文案命中，或自检本身就发现了结构问题，
                # 二者任一成立即回退。自检命中这条很重要——上游文案可以任意含糊，
                # 但我们自己能确认请求体坏了，就不该把判断权交给对方的文案措辞。
                if e.status_code == 400 and round_num > 1 and len(api_messages) >= 3:
                    tool_format_hit = is_tool_format_400(e.message)
                    if (tool_format_hit or post_issues) and body_retry_budget > 0:
                        reason = "上游文案指向工具格式" if tool_format_hit else "本地自检发现结构问题"
                        # 裁剪结果必须先过自检：裁出畸形体比不裁更糟
                        # （详见 try_trim_last_tool_round 的 docstring）
                        trimmed, reject = try_trim_last_tool_round(api_messages)
                        if trimmed is not None:
                            body_retry_budget -= 1
                            api_messages = trimmed
                            logger.warning(
                                f"400 请求体被拒（{reason}），裁掉最后一轮工具交互后重试："
                                f"轮次={round_num}, 剩余消息数={len(api_messages)}, "
                                f"剩余重试预算={body_retry_budget}"
                            )
                            logger.warning(f"裁剪后请求体骨架: {describe_api_messages(api_messages)}")
                            # continue 会回到循环头执行 round_num += 1，所以轮次号
                            # 会递进（不是"重试当前轮次"）。这是刻意接受的：
                            # 裁剪重试确实消耗一次上游调用，让它占一个轮次号，
                            # rounds 模式下才不会因为反复裁剪而绕过 max_rounds。
                            # 已核两处受 round_num 影响的判断都不会被推到意外分支：
                            # 裁剪前置条件是 `round_num > 1`（单调递增，只会更成立），
                            # 超时检查看的是 elapsed / round_num > max_rounds（同样单调）。
                            # 唯一副作用是 metadata.tool_rounds 会把裁剪轮计入。
                            continue
                        # 自救动作本身验证不通过：不消耗预算（没真的重试），
                        # 如实放弃并把原因带进告警，否则维护者只看到一句 400。
                        logger.error(f"400 无法通过裁剪自救（{reason}），放弃本次排查：{reject}")
                        trim_reject_reason = reject
                    elif body_retry_budget <= 0:
                        logger.error("400 请求体重试预算已耗尽，放弃本次排查")

                error_type = classify_api_error(e.status_code, str(e.message))
                # error_detail 会进告警（见 ai_provider.call 的单点上报），把自检
                # 结论和 request_id 一起带上，维护者收到告警即可判断该找谁。
                #
                # 上游文案必须在【源头】过摘要，不能指望下游兜：ai_alert 那边是
                # `redact(detail)[:500]`，redact 走的是形态匹配（IP / AK-SK /
                # token），挡不住被网关回显进错误文案的用户提问正文。
                detail_parts = [
                    f"status={e.status_code}",
                    f"round={round_num}",
                    f"message={summarize_text(e.message, limit=_ALERT_MSG_CHARS, label='upstream_msg')}",
                ]
                request_ids = extract_upstream_request_ids(e.message)
                if request_ids:
                    detail_parts.append(f"upstream_request_ids={','.join(request_ids)}")
                detail_parts.append(
                    f"body_audit={'; '.join(post_issues)}" if post_issues else "body_audit=未发现结构问题"
                )
                if trim_reject_reason:
                    detail_parts.append(f"trim_rejected={trim_reject_reason}")
                return friendly_api_error_message(error_type, e.message), {
                    "mode": "agentic", "error": error_type,
                    "error_detail": ", ".join(detail_parts),
                }
            except Exception as e:
                logger.error(f"Agentic 循环异常: {e}", exc_info=True)
                return f"❌ 排查过程出错: {str(e)}", {
                    "mode": "agentic", "error": "exception",
                    "error_detail": f"{type(e).__name__}: {e}",
                }

        # 达到超时限制
        timeout_msg = "⚠️ 排查过程较复杂，已达到总时间限制。以上是目前的分析结果，如需继续请提供更多信息。" if timeout_mode == "time" else "⚠️ 排查过程较复杂，已达到最大分析轮数。以上是目前的分析结果，如需继续请提供更多信息。"
        return timeout_msg, {"mode": "agentic", "tool_rounds": round_num, "timeout": True}

    def _execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> Any:
        """
        执行工具调用，带智能批量优化

        Args:
            tool_name: 工具名称
            tool_input: 工具参数

        Returns:
            工具执行结果（可能包含批量扩展的结果）
        """
        if not self.git_tools:
            return {"error": "Git 工具未初始化"}

        # 简单直接地执行工具调用
        if tool_name == "run_git_command":
            args = tool_input.get("args", [])
            return self.git_tools.run_git_command(args)
        elif tool_name == "search_code":
            return self.git_tools.search_code(
                query=tool_input["query"],
                ref=tool_input.get("ref"),
                path_filter=tool_input.get("path_filter"),
                context=tool_input.get("context", 0)
            )
        elif tool_name == "find_files":
            return self.git_tools.find_files(
                name_pattern=tool_input["name_pattern"],
                ref=tool_input.get("ref")
            )
        elif tool_name == "list_refs":
            return self.git_tools.list_refs(tool_input.get("pattern"))
        elif tool_name == "read_file":
            return self.git_tools.read_file(
                path=tool_input["path"],
                ref=tool_input.get("ref"),
                start_line=tool_input.get("start_line"),
                end_line=tool_input.get("end_line")
            )
        elif tool_name == "list_dir":
            return self.git_tools.list_dir(
                path=tool_input.get("path", ""),
                ref=tool_input.get("ref")
            )
        elif tool_name == "switch_repo":
            if not self.repo_manager:
                return {"error": "多仓库管理器未初始化"}
            result_msg = self.repo_manager.switch_repo(tool_input["repo_name"])
            # 切换后更新当前 git_tools 引用
            self.git_tools = self.repo_manager.current
            return {"message": result_msg}
        else:
            return {"error": f"未知工具: {tool_name}"}

    def _send_with_context_operations(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
        max_rounds: int = 6,
        timeout_mode: str = "time",
        max_time: int = 300,
    ) -> tuple[str, dict]:
        """
        Operations 模式：支持运维工具调用的多轮对话

        Args:
            messages: 消息列表
            doc_context: 文档上下文
            session_id: 会话 ID
            max_rounds: 最大工具调用轮数
            timeout_mode: 超时模式 "time" / "rounds"
            max_time: 总时间限制（秒），timeout_mode="time" 时生效

        Returns:
            (reply_text, metadata) 元组
            metadata 包含 tool_rounds（实际工具调用轮数）
        """
        if not self.operations_tools:
            return "❌ 运维工具未初始化", {"mode": "operations", "error": "not_initialized"}

        # 生成运维工具的 Claude tools schema
        tools_schema = self._generate_operations_tools_schema()

        # 转换消息格式，移除 /运维 前缀
        api_messages = []
        for msg in messages:
            content_parts = []
            for content in msg.content:
                if content.type == "text":
                    text = content.data
                    # 移除 /运维 或 /ops 前缀
                    if text.strip().startswith("/运维"):
                        text = text.strip()[3:].strip()
                    elif text.strip().startswith("/ops"):
                        text = text.strip()[4:].strip()
                    content_parts.append({"type": "text", "text": text})
                else:
                    content_parts.extend(self._content_to_api_parts(content))

            if content_parts:
                api_messages.append({"role": msg.role, "content": content_parts})

        # 获取操作者身份信息
        operator_info = "未知用户"
        operator_id = ""
        operator_name = ""
        source = "feishu"
        chat_id = None  # 用于群组白名单检查

        # 只认「最后一条 user 消息」的身份，即当前发言人。
        # 原先是倒序找第一条带 metadata 的消息，一旦当前这条缺 metadata，就会往前
        # 借用历史轮里【别人】的身份来鉴权 —— B 引用 A 的运维链追问就可能拿到 A 的
        # 权限，属于越权。这里在第一条 user 消息处即停，找不到身份就按未知处理。
        last_user_msg = next(
            (msg for msg in reversed(messages) if msg.role == "user"), None
        )
        meta = (last_user_msg.metadata if last_user_msg else None) or {}
        if any(k in meta for k in ("operator_id", "operator_name", "chat_id")):
            operator_name = meta.get("operator_name", "")
            operator_id = meta.get("operator_id", "")
            source = meta.get("source", "") or "feishu"
            chat_id = meta.get("chat_id")
            if operator_name or operator_id:
                operator_info = f"{operator_name} ({operator_id}, {source})"
            logger.info(f"从消息 metadata 提取操作者信息: {operator_info}, chat_id={chat_id}")
        else:
            # 当前发言人没有身份 metadata：按未知处理并走鉴权拒绝，绝不回退借用历史身份
            logger.warning(
                f"当前发言人消息缺少运维 metadata（共 {len(messages)} 条消息），"
                f"将按未知用户鉴权"
            )

        # 只读鉴权门：仅允许白名单内的运维同事 / 授权群组使用查询能力
        if self.operations_tools and self.operations_tools.operations_manager:
            from ai_assistant.core.operations_models import OperatorIdentity
            ops_manager = self.operations_tools.operations_manager
            operator = OperatorIdentity(
                user_id=operator_id or "unknown",
                username=operator_name or operator_id or "unknown",
                metadata={"channel": source},
            )
            # 任取一台机器做白名单/群组校验（授权是渠道级别，与具体机器无关）
            any_machine = next(iter(ops_manager.machines.values()), None)
            if not ops_manager.is_authorized(
                operator, any_machine, chat_id=chat_id
            ):
                logger.warning(
                    f"运维查询被拒绝：用户 {operator.username} "
                    f"(user_id={operator_id}, chat_id={chat_id}) 不在授权白名单/群组中"
                )
                return (
                    "抱歉，您没有运维查询权限。请联系管理员将您加入运维白名单，"
                    "或在已授权的运维群组中使用本功能。",
                    {"mode": "operations", "error": "unauthorized"},
                )

        # 构建 system prompt（运维助手指引）
        system_parts = [
            "你是运维助手，当前用户是授权运维人员。",
            "",
        ]

        # 添加可用机器列表
        if self.operations_tools and self.operations_tools.operations_manager:
            ops_manager = self.operations_tools.operations_manager
            machines = ops_manager.machines
            applications = ops_manager.applications  # Dict[str, Application]
            if machines:
                system_parts.append("可用机器列表：")
                for machine in machines.values():  # machines 是 Dict[str, Machine]，需要 .values()
                    # 别名/自然语言映射来自 display_name 和 tags
                    alias_parts = []
                    if machine.display_name and machine.display_name != machine.name:
                        alias_parts.append(machine.display_name)
                    if machine.tags:
                        alias_parts.extend(machine.tags)
                    aliases = ", ".join(alias_parts) if alias_parts else machine.name
                    # host 在 ssh_config 中
                    host = machine.ssh_config.host if machine.ssh_config else "未知"
                    machine_desc = "- {} (别名/标签: {}, 主机: {})".format(
                        machine.name, aliases, host
                    )
                    system_parts.append(machine_desc)

                    # 列出该机器上的应用详情（别名、部署路径、描述）
                    machine_apps = [
                        app for app in applications.values()
                        if machine.name in app.machines
                    ]
                    for app in machine_apps:
                        app_aliases = ", ".join(app.aliases) if app.aliases else "无"
                        app_line = "    · 应用「{}」(别名: {}".format(app.name, app_aliases)
                        if app.path:
                            app_line += ", 部署路径: {}".format(app.path)
                        if app.description:
                            app_line += ", 说明: {}".format(app.description)
                        app_line += ")"
                        system_parts.append(app_line)
                system_parts.append("")
                # 引导 AI 用部署路径精确区分同名服务
                system_parts.append(
                    "重要：当多个应用同名但部署路径不同时（如不同版本/架构的 ts-service），"
                    "必须依据上面列出的『部署路径』来区分。查询进程时用 get_app_status 的 app_name "
                    "传入应用的部署路径（或路径中的唯一片段），而不是笼统的服务名，"
                    "以免把不同版本的进程混在一起。\n"
                    "重要：判断『某端口是否有服务在运行』或『哪个进程占用了某端口』时，"
                    "必须调用 get_port_info(machine_name, port)，禁止用 get_app_status(app_name=端口号)——"
                    "端口号不在进程命令行里，那样查不到。get_port_info 返回的 cwd（工作目录）和 cmdline "
                    "可与上面的部署路径比对，从而确认到底是哪个版本的服务在监听该端口。"
                )
                system_parts.append("")

        system_parts.extend([
            "可用工具（所有工具都需要 machine_name 参数指定目标机器）：",
            "- get_app_status(machine_name, app_name) - 查看应用运行状态",
            "- get_app_version(machine_name, source, jar_path/log_path/version_file/api_url) - 查看应用版本/分支",
            "  · Java 应用查版本/分支/commit 一律用 source='jar' 调这个工具，不要自己拼 MANIFEST 路径；"
            "工具会按企业规范读取 jar 内 BOOT-INF/classes/git.info（构建流水线写入，含分支和 commit），"
            "读不到才回退 MANIFEST.MF。除用户明确指定外，不要传 jar_file_path。",
            "- get_jar_info(machine_name, jar_path) - 分析 Java 应用 jar 包（获取 MANIFEST、Maven 信息、Git 版本）",
            "- get_process_info(machine_name, pid/app_name) - 查看进程详情（CPU、内存、端口、线程数）",
            "- get_port_info(machine_name, port) - 按端口号查询监听进程（PID、命令行、工作目录），判断端口是否有服务在运行时必须用它",
            "- get_system_metrics(machine_name) - 查看系统资源（CPU、内存、磁盘、负载）",
            "- get_logs(machine_name, log_path, lines, grep_pattern) - 查看应用日志",
            "  · grep_pattern 按扩展正则（grep -E）解析，可直接用 | 和 ()，"
            "如 'SQLException|Deadlock' 或 '2026-09-29.*(ERROR|WARN)'。",
            "  · 返回 matched=false 表示命令执行成功、只是日志里确实没有匹配内容，"
            "这是确定结论。不要用同一模式重试，应换关键词、放宽模式或增大 lines。",
            "",
            "本助手仅提供【只读查询】能力，不提供重启/停止/启动等任何有副作用的操作。"
            "如果用户要求重启、停止、启动、部署、修改配置等写操作，请礼貌说明本助手只支持查询，"
            "相关变更请运维同事手动执行。",
            "",
            "工具使用规则：",
            "1. **machine_name 参数**：每次调用都必须显式传，使用上面列出的机器名称或别名。"
            "不会从上一次调用继承，漏传会直接报错。",
            "2. **自然语言理解**：用户说'研发环境'时，使用别名映射到对应的机器名",
            "3. **错误处理**：如果工具返回 success=false，查看 error 字段了解原因。"
            "禁止用完全相同的参数重复调用同一工具——参数不变结果一定不变。"
            "要么改参数、换工具，要么向用户说明查不到并给出已掌握的结论。",
            "4. **只读约束**：只能查询配置中定义的机器和应用，不得尝试执行任何变更类命令",
            "",
            f"当前用户：{operator_info}",
            "",
            "工作流程：",
            "1. 理解用户的查询需求（查看状态、版本、日志、端口、资源等）",
            "2. 使用合适的只读工具收集信息",
            "3. 给出清晰的结论和建议",
            "",
            "回复格式要求：",
            "- 使用中文回答",
            "- 查询时明确说明目标（哪台机器、哪个应用）",
            "- 查询完成后给出清晰的结果摘要（状态、资源使用、版本信息等）",
            "- 如果工具调用失败，解释可能的原因（机器不存在、连接失败等）",
        ])

        # 注入飞书文档（如果有）
        if doc_context:
            system_parts.append("")
            system_parts.append(doc_context)

        system_prompt = "\n".join(system_parts)

        # Operations Agentic 循环
        import time
        start_time = time.time()
        round_num = 0
        # (工具名, 规范化参数) -> 失败次数，用于拦截完全相同参数的重复失败调用
        failed_calls: Dict[tuple, int] = {}
        # 400 容错预算，语义同 Agentic 循环（见 _send_with_context_agentic）
        body_retry_budget = 2
        trim_reject_reason = None

        while True:
            round_num += 1

            # 检查超时条件
            elapsed = time.time() - start_time
            if timeout_mode == "time":
                if elapsed > max_time:
                    logger.warning(f"达到总时间限制 {max_time}s，已执行 {round_num-1} 轮，耗时 {elapsed:.1f}s")
                    break
                logger.info(f"Operations 轮次 {round_num}/∞, 已耗时 {elapsed:.1f}s/{max_time}s")
            elif timeout_mode == "rounds":
                if round_num > max_rounds:
                    logger.warning(f"达到最大轮数 {max_rounds}，返回当前结果")
                    break
                logger.info(f"Operations 轮次 {round_num}/{max_rounds}")
            else:
                logger.info(f"Operations 轮次 {round_num}")

            # 发请求前自检（与 Agentic 循环同一套逻辑，运维侧同样会多轮回传工具结果）
            pre_issues = audit_api_messages(api_messages)
            if pre_issues:
                logger.warning(
                    f"请求体自检发现 {len(pre_issues)} 处问题（运维第 {round_num} 轮，仍将尝试发送）-> "
                    + "; ".join(pre_issues)
                )
                logger.warning(f"请求体骨架(round={round_num}): {describe_api_messages(api_messages)}")
            elif round_num > 1:
                logger.debug(f"请求体骨架(round={round_num}): {describe_api_messages(api_messages)}")

            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=api_messages,
                    tools=tools_schema,
                )

                logger.info(
                    f"Claude 响应: stop_reason={response.stop_reason}, "
                    f"tokens(input:{response.usage.input_tokens}, output:{response.usage.output_tokens})"
                )

                # 收集本轮的 assistant 消息内容（同时剔除会让下一轮 400 的空 text block）
                assistant_content, tool_uses, collect_notes = collect_assistant_content(response.content)
                for note in collect_notes:
                    logger.warning(f"运维第 {round_num} 轮响应内容异常: {note}")

                # content 为空数组会被上游 400 拒绝，补占位文本（详见 Agentic 循环同处注释）
                if not assistant_content:
                    if tool_uses:
                        logger.error(
                            f"运维第 {round_num} 轮响应有 {len(tool_uses)} 个 tool_use 但 content 收集为空，"
                            f"响应结构异常"
                        )
                    else:
                        logger.warning(f"运维第 {round_num} 轮响应 content 为空，补占位文本以免下一轮请求 400")
                        assistant_content = [{"type": "text", "text": "（本轮无输出）"}]
                api_messages.append({"role": "assistant", "content": assistant_content})

                # 如果没有工具调用，返回最终文本
                if response.stop_reason == "end_turn" or not tool_uses:
                    final_text = ""
                    for block in response.content:
                        if hasattr(block, 'text'):
                            final_text += block.text
                    logger.info(f"Operations 完成: 总轮数={round_num}, 最终回复={len(final_text)}字符")

                    # 构建 metadata（Operations 模式）
                    metadata = {
                        "mode": "operations",
                        "tool_rounds": round_num,
                    }

                    return (final_text or "抱歉，未能生成有效回复。"), metadata

                # 执行工具调用（串行执行，因为运维操作可能有状态变更）
                tool_results = []

                for tool_use in tool_uses:
                    tool_name = tool_use.name
                    tool_input = tool_use.input
                    logger.info(f"执行运维工具: {tool_name}({tool_input})")

                    try:
                        result = self._execute_operations_tool(tool_name, tool_input, chat_id=chat_id)
                        result_str = json.dumps(result, ensure_ascii=False)

                        # 完全相同的(工具, 参数)重复失败保护。
                        # 这里只拦"同工具+同参数+又失败"，不拦同名工具连续调用——
                        # 连续查多台机器/多个日志是正常操作（参见 commit 9a4addc
                        # 移除"连续5次调用同一工具"误报的教训）。
                        # 加这道闸是因为曾出现参数污染导致 AI 反复发同一个必然失败的
                        # 调用，直到耗尽 600s 超时，用户等了 10 分钟拿到空结果。
                        if isinstance(result, dict) and not result.get("success", True):
                            call_key = (
                                tool_name,
                                json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
                            )
                            failed_calls[call_key] = failed_calls.get(call_key, 0) + 1
                            if failed_calls[call_key] >= 3:
                                logger.warning(
                                    f"运维工具 {tool_name} 以完全相同的参数连续失败 "
                                    f"{failed_calls[call_key]} 次，提示 AI 改变策略"
                                )
                                result_str = json.dumps({
                                    **result,
                                    "repeated_failure": True,
                                    "hint": (
                                        f"这个调用用完全相同的参数已失败 "
                                        f"{failed_calls[call_key]} 次，再重试结果不会变。"
                                        "请改变参数（换机器、换路径、换过滤模式）、"
                                        "改用其他工具，或直接向用户说明查不到并给出已有结论。"
                                    )
                                }, ensure_ascii=False)

                        # 空 content 的 tool_result 会被上游以 400 拒绝
                        if not result_str.strip():
                            logger.warning(f"运维工具 {tool_name} 结果序列化为空，补占位文本")
                            result_str = "（工具无返回内容）"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result_str
                        })
                        # 只打规模和摘要，不打全文（运维工具会回传整段远端日志）
                        logger.info(f"运维工具 {tool_name} 完成: {len(result_str)} 字符")
                        logger.debug("运维工具 {} 结果: {}", tool_name, summarize_obj(result))

                    except Exception as e:
                        logger.error(f"运维工具 {tool_name} 执行失败: {e}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps({"error": str(e)}, ensure_ascii=False),
                            "is_error": True
                        })

                # 将工具结果加入对话历史
                api_messages.append({"role": "user", "content": tool_results})

            except anthropic.APITimeoutError as e:
                logger.error("Anthropic API 请求超时")
                return "⏱️ AI 服务响应超时，请稍后重试", {
                    "mode": "operations", "error": "timeout",
                    "error_detail": f"APITimeoutError: {e}",
                }
            except anthropic.APIConnectionError as e:
                logger.error(f"Anthropic API 连接失败: {e}")
                return "🔌 AI 服务连接失败，请检查网络或稍后重试", {
                    "mode": "operations", "error": "connection",
                    "error_detail": f"APIConnectionError: {e}",
                }
            except anthropic.APIStatusError as e:
                post_issues = audit_api_messages(api_messages)
                log_api_status_error(
                    mode="operations",
                    round_num=round_num,
                    error=e,
                    api_messages=api_messages,
                    base_url=self.client.base_url,
                    model=self.model,
                    audit_issues=post_issues,
                )

                # 运维循环此前完全没有 400 容错：请求体一旦被拒就整次放弃。
                # 它和 Agentic 一样是多轮动态拼请求体，同样的缺陷必须同样补上。
                if e.status_code == 400 and round_num > 1 and len(api_messages) >= 3:
                    tool_format_hit = is_tool_format_400(e.message)
                    if (tool_format_hit or post_issues) and body_retry_budget > 0:
                        reason = "上游文案指向工具格式" if tool_format_hit else "本地自检发现结构问题"
                        # 与 Agentic 分支同款：裁剪结果必须先过自检
                        trimmed, reject = try_trim_last_tool_round(api_messages)
                        if trimmed is not None:
                            body_retry_budget -= 1
                            api_messages = trimmed
                            logger.warning(
                                f"400 请求体被拒（{reason}），裁掉最后一轮工具交互后重试："
                                f"轮次={round_num}, 剩余消息数={len(api_messages)}, "
                                f"剩余重试预算={body_retry_budget}"
                            )
                            logger.warning(f"裁剪后请求体骨架: {describe_api_messages(api_messages)}")
                            continue
                        logger.error(f"400 无法通过裁剪自救（{reason}），放弃本次运维操作：{reject}")
                        trim_reject_reason = reject
                    elif body_retry_budget <= 0:
                        logger.error("400 请求体重试预算已耗尽，放弃本次运维操作")

                error_type = classify_api_error(e.status_code, str(e.message))
                # 源头摘要，理由同 Agentic 分支（下游 redact 挡不住用户正文）
                detail_parts = [
                    f"status={e.status_code}",
                    f"round={round_num}",
                    f"message={summarize_text(e.message, limit=_ALERT_MSG_CHARS, label='upstream_msg')}",
                ]
                request_ids = extract_upstream_request_ids(e.message)
                if request_ids:
                    detail_parts.append(f"upstream_request_ids={','.join(request_ids)}")
                detail_parts.append(
                    f"body_audit={'; '.join(post_issues)}" if post_issues else "body_audit=未发现结构问题"
                )
                if trim_reject_reason:
                    detail_parts.append(f"trim_rejected={trim_reject_reason}")
                return friendly_api_error_message(error_type, e.message), {
                    "mode": "operations", "error": error_type,
                    "error_detail": ", ".join(detail_parts),
                }
            except Exception as e:
                logger.error(f"Operations 循环异常: {e}", exc_info=True)
                return f"❌ 运维操作过程出错: {str(e)}", {
                    "mode": "operations", "error": "exception",
                    "error_detail": f"{type(e).__name__}: {e}",
                }

        # 达到超时限制
        timeout_msg = "⚠️ 运维操作较复杂，已达到总时间限制。以上是目前的结果，如需继续请提供更多信息。" if timeout_mode == "time" else "⚠️ 运维操作较复杂，已达到最大轮数。以上是目前的结果，如需继续请提供更多信息。"
        return timeout_msg, {"mode": "operations", "tool_rounds": round_num, "timeout": True}

    def _generate_operations_tools_schema(self) -> List[Dict[str, Any]]:
        """
        从 OperationsTools 实例自动生成 Claude tools schema

        Returns:
            Claude tools schema 列表
        """
        if not self.operations_tools:
            return []

        import inspect

        schema = []

        # 定义需要暴露给 AI 的方法（排除内部方法和辅助方法）
        exposed_methods = [
            'get_app_status',
            'get_app_version',
            'get_jar_info',
            'get_process_info',
            'get_port_info',
            'get_system_metrics',
            'get_logs',
        ]

        for method_name in exposed_methods:
            method = getattr(self.operations_tools, method_name, None)
            if not method or not callable(method):
                continue

            # 获取方法签名和文档
            sig = inspect.signature(method)
            doc = inspect.getdoc(method) or f"{method_name} 方法"

            # 提取文档第一行作为描述
            description_lines = doc.split('\n\n')[0].strip().split('\n')
            description = ' '.join(line.strip() for line in description_lines if line.strip())

            tool = {
                "name": method_name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }

            # 首先添加 machine_name 参数（AI 传入机器名称字符串）
            tool["input_schema"]["properties"]["machine_name"] = {
                "type": "string",
                "description": "目标机器名称（支持模糊匹配，如 'web-1' 或 '生产服务器1'）"
            }
            tool["input_schema"]["required"].append("machine_name")

            # 解析其他参数（跳过 self 和 machine）
            for param_name, param in sig.parameters.items():
                if param_name in ('self', 'machine', 'operator_id'):
                    # operator_id 由系统自动注入，不需要 AI 提供
                    continue

                # 根据类型注解确定参数类型
                param_type = "string"
                if param.annotation != inspect.Parameter.empty:
                    if param.annotation == int:
                        param_type = "integer"
                    elif param.annotation == bool:
                        param_type = "boolean"
                    elif hasattr(param.annotation, '__origin__'):
                        # 处理 Optional[str] 等类型
                        origin = getattr(param.annotation, '__origin__', None)
                        if origin is list:
                            param_type = "array"

                # 从文档中提取参数描述
                param_description = f"{param_name} 参数"
                if 'Args:' in doc:
                    args_section = doc.split('Args:')[1].split('Returns:')[0]
                    for line in args_section.split('\n'):
                        if param_name + ':' in line or param_name + ' :' in line:
                            desc_parts = line.split(':', 1)
                            if len(desc_parts) > 1:
                                param_description = desc_parts[1].strip()
                                break

                tool["input_schema"]["properties"][param_name] = {
                    "type": param_type,
                    "description": param_description
                }

                # 必需参数（没有默认值且不是 Optional）
                if param.default == inspect.Parameter.empty:
                    tool["input_schema"]["required"].append(param_name)

            schema.append(tool)

        logger.info(f"自动生成 {len(schema)} 个运维工具 schema")
        return schema

    def _execute_operations_tool(self, tool_name: str, tool_input: Dict[str, Any], chat_id: Optional[str] = None) -> Any:
        """
        执行运维工具调用

        Args:
            tool_name: 工具名称
            tool_input: 工具参数
            chat_id: 群组/会话ID（用于群组白名单检查）

        Returns:
            工具执行结果
        """
        if not self.operations_tools:
            return {"success": False, "error": "运维工具未初始化", "data": None}

        try:
            # 获取方法
            method = getattr(self.operations_tools, tool_name, None)
            if not method or not callable(method):
                return {"success": False, "error": f"未知运维工具: {tool_name}", "data": None}

            # 关键：必须拷贝一份再改。
            # 传进来的 tool_input 就是 Claude 响应里 block.input 那个 dict 对象，
            # 调用方已经把它的【引用】放进了 api_messages 的 assistant 消息里。
            # 若在此就地 pop('machine_name') / 塞 jar_file_path，等于把 Claude
            # 自己的调用记录改掉：下一轮回传时它看到"我上次没传 machine_name"，
            # 与工具返回的"缺少 machine_name"互相印证，于是它永远学不会该传，
            # 只能反复重试到超时（本次修复的死循环 bug）。
            tool_input = dict(tool_input)

            # OperationsTools 的方法第一个参数是 Machine 对象
            # AI 传入的是机器名称字符串，需要转换
            # 但并非所有工具都需要 machine 参数（如 list_machines）

            # 检查方法签名中是否需要 machine 参数和 chat_id 参数
            import inspect
            sig = inspect.signature(method)
            params = list(sig.parameters.keys())

            # 如果方法需要 machine 参数，从 operations_manager 查找
            if 'machine' in params and self.operations_tools.operations_manager:
                machine_name = tool_input.pop('machine_name', None)
                if not machine_name:
                    # 错误信息里带上可选机器名，否则 AI 只知道"缺了"但不知道该填什么
                    available = list(
                        self.operations_tools.operations_manager.machines.keys()
                    )
                    return {
                        "success": False,
                        "error": (
                            f"缺少 machine_name 参数，调用 {tool_name} 必须指定目标机器。"
                            f"可选机器：{available}"
                        ),
                        "data": None
                    }

                machine = self.operations_tools.operations_manager.find_machine(machine_name)
                if not machine:
                    return {
                        "success": False,
                        "error": f"未找到机器: {machine_name}",
                        "data": None
                    }

                # 特殊处理 get_app_version: Claude 通常从进程 cmdline 发现绝对 jar_path。
                # 这里按 path 前缀匹配到配置的应用，把配置里 version_detection 指定的 jar 内
                # 文件注入为 jar_file_path，保留 Claude 的绝对 jar_path。
                # 匹配不到配置也不要紧：工具层默认按企业规范先读 BOOT-INF/classes/git.info，
                # 再回退 MANIFEST.MF，因此这里只补 source，不再硬塞文件路径。
                if (tool_name == 'get_app_version'
                        and tool_input.get('jar_path')
                        and not tool_input.get('jar_file_path')
                        and not tool_input.get('application_metadata')):
                    jar_path = tool_input['jar_path']
                    manager = self.operations_tools.operations_manager
                    apps = list(manager.applications.values()) if manager else []

                    # 取 path 最长（最精确）的前缀匹配，避免同前缀路径误配
                    best_app = None
                    best_len = -1
                    for app in apps:
                        app_path = getattr(app, 'path', '') or ''
                        if app_path and jar_path.startswith(app_path) and len(app_path) > best_len:
                            best_app = app
                            best_len = len(app_path)

                    if best_app is not None:
                        metadata = getattr(best_app, 'metadata', {}) or {}
                        # 从 version_detection 里找到第一个带 file_path 的 jar_manifest 配置
                        jar_file_path = None
                        for cfg in metadata.get('version_detection', []):
                            if cfg.get('type') == 'jar_manifest' and cfg.get('file_path'):
                                jar_file_path = cfg['file_path']
                                break
                        if jar_file_path:
                            tool_input['jar_file_path'] = jar_file_path
                            logger.info(
                                f"✅ get_app_version 自动注入 jar_file_path={jar_file_path} "
                                f"(应用: {best_app.name})"
                            )
                        else:
                            logger.debug(
                                f"应用 {best_app.name} 无 jar_manifest.file_path 配置，"
                                f"按企业规范默认尝试 BOOT-INF/classes/git.info"
                            )
                    else:
                        logger.debug(f"get_app_version 未匹配到应用配置: jar_path={jar_path}, 候选应用={len(apps)} 个")

                    # Claude 偶尔漏传 source 导致工具直接报参数缺失，这里按 jar_path 补齐
                    tool_input['source'] = 'jar'


                # 如果方法需要 chat_id 参数，传入
                if 'chat_id' in params:
                    result = method(machine, chat_id=chat_id, **tool_input)
                else:
                    result = method(machine, **tool_input)
            else:
                # 不需要 machine 参数，直接调用
                if 'chat_id' in params:
                    result = method(chat_id=chat_id, **tool_input)
                else:
                    result = method(**tool_input)

            # 确保返回格式统一
            if not isinstance(result, dict):
                result = {"success": True, "data": result, "error": None}

            return result

        except Exception as e:
            logger.error(f"运维工具 {tool_name} 执行异常: {e}", exc_info=True)
            return {"success": False, "error": str(e), "data": None}

    def _send_with_context_standard(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
    ) -> tuple[str, dict]:
        """
        标准 RAG 模式（原有实现）

        Returns:
            (reply_text, metadata) 元组
            metadata 包含 doc_count（检索到的文档数）等
        """
        # 在 try 之外初始化：except 分支要拿它打请求体骨架，
        # 若定义在 try 内部，转换阶段就抛异常时这个名字还不存在。
        api_messages: List[Dict[str, Any]] = []
        try:
            # 转换消息格式
            for msg in messages:
                content_parts = []
                for content in msg.content:
                    content_parts.extend(self._content_to_api_parts(content))

                if content_parts:
                    api_messages.append({"role": msg.role, "content": content_parts})

            # 构建 system prompt
            system_parts = ["你是一个智能助手，帮助用户回答问题。你必须始终使用中文回答，包括技术术语的解释也要用中文。即使用户用英文提问，你也要用中文回答。"]

            # 注入文档内容（由基类传入）
            if doc_context:
                system_parts.append(doc_context)
                system_parts.append(
                    "请基于以上文档内容回答用户的问题。如果文档中没有相关信息，请如实告知。\n"
                    "回答完成后，在末尾附加参考文档链接（除非用户明确要求不附加），格式如下：\n"
                    "---\n"
                    "📎 参考文档：\n"
                    "- [文档标题](原文链接)"
                )

            # 注入负反馈提示
            feedback_prompt = self._build_feedback_prompt(session_id)
            if feedback_prompt:
                system_parts.append(feedback_prompt)

            system_prompt = "\n\n".join(system_parts)

            # 调用 Anthropic API
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=api_messages,
            )

            reply = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    reply += block.text
            if not reply:
                reply = "抱歉，未能生成有效回复。"
            logger.info(
                f"Claude 回复已接收: {len(reply)} 字符, "
                f"tokens(input:{response.usage.input_tokens}, output:{response.usage.output_tokens})"
            )

            # 构建 metadata（标准 RAG 模式）
            # 统计文档数：doc_context 中每个 "## " 开头表示一篇文档
            doc_count = 0
            if doc_context:
                doc_count = doc_context.count("\n## ")
                # 如果第一行就是 ##，需要额外加1
                if doc_context.startswith("## "):
                    doc_count += 1

            metadata = {
                "mode": "rag",
                "doc_count": doc_count,
            }

            return reply, metadata

        except anthropic.APITimeoutError as e:
            logger.error("Anthropic API 请求超时")
            return "⏱️ AI 服务响应超时，请稍后重试", {
                "mode": "rag", "error": "timeout",
                "error_detail": f"APITimeoutError: {e}",
            }
        except anthropic.APIConnectionError as e:
            logger.error(f"Anthropic API 连接失败: {e}")
            return "🔌 AI 服务连接失败，请检查网络或稍后重试", {
                "mode": "rag", "error": "connection",
                "error_detail": f"APIConnectionError: {e}",
            }
        except anthropic.APIStatusError as e:
            status_code = e.status_code
            # 原来这里把响应体原样打出来（可能回显用户提问），改用统一诊断函数：
            # 响应体截断 + 骨架代替正文，三种模式的错误日志形态也就一致了。
            log_api_status_error(
                mode="rag",
                round_num=0,
                error=e,
                api_messages=api_messages,
                base_url=self.client.base_url,
                model=self.model,
            )

            # 错误分类同时看状态码和错误文案：网关常把额度耗尽返回 403 而非 402
            error_type = classify_api_error(status_code, str(e.message))
            # 源头摘要，理由同 Agentic 分支（下游 redact 挡不住用户正文）
            detail_parts = [
                f"status={status_code}",
                f"message={summarize_text(e.message, limit=_ALERT_MSG_CHARS, label='upstream_msg')}",
            ]
            request_ids = extract_upstream_request_ids(e.message)
            if request_ids:
                detail_parts.append(f"upstream_request_ids={','.join(request_ids)}")
            return friendly_api_error_message(error_type, e.message), {
                "mode": "rag", "error": error_type,
                "error_detail": ", ".join(detail_parts),
            }

    def check_health(self) -> bool:
        """检查 Anthropic API 服务健康状态"""
        try:
            # 发送一个简单请求验证 API Key 有效
            response = self.client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception as e:
            logger.warning(f"Anthropic 健康检查失败: {e}")
            return False

    def _build_feedback_prompt(self, session_id: Optional[str]) -> str:
        """
        构建负反馈提示文本

        Args:
            session_id: 会话 ID

        Returns:
            反馈提示文本，如果没有负反馈则返回空字符串
        """
        if not self.feedback_manager or not session_id:
            return ""

        feedbacks = self.feedback_manager.get_session_negative_feedbacks(session_id, limit=3)
        if not feedbacks:
            return ""

        # 提取 record_ids，并用 chat_history 反查原始 query/answer
        record_ids = [fb.get("record_id") for fb in feedbacks if fb.get("record_id")]
        if not record_ids or not self._chat_history:
            return ""

        records = self._chat_history.get_records_by_ids(record_ids)

        lines = ["\n\n【用户反馈提示】", "在本次对话中，用户对你的以下回答表示不满意：", ""]

        injected = 0
        for fb in feedbacks:
            record = records.get(fb.get("record_id"))
            if not record:
                continue  # 找不到历史记录就跳过

            query = record.get("query", "")
            answer = record.get("answer", "")
            lines.append(f"问题：「{query[:100]}」")
            lines.append(f"你的回答：「{answer[:150]}...」")
            if fb.get("feedback_text"):
                lines.append(f"用户反馈：「{fb['feedback_text']}」")
            lines.append("")
            injected += 1

        if injected == 0:
            return ""

        lines.append("请在本次回答中注意改进，提供更准确、更具体的信息。")

        logger.info(f"注入 {injected} 条负反馈到 Prompt")
        return "\n".join(lines)

    def filter_docs_by_relevance(self, query: str, candidates: List[Dict[str, Any]], max_docs: int = 3) -> List[int]:
        """
        用 Claude 判断候选文档标题与 query 的相关性，返回 0-based 下标列表。

        prompt 结构见 build_doc_filter_messages：候选列表在前、用户查询用 XML 定界标签
        包成"待匹配素材"、任务指令复述在后，再用 assistant prefill 强制从编号起手。
        否则模型会把用户查询当成提问，转而索要日志内容并拒绝给编号（真实故障，
        导致 RAG 退化成纯融合排序、注入一堆无关文档）。

        Args:
            query: 用户查询文本
            candidates: 候选文档列表，每项需有 "path" 或 "title" 字段
            max_docs: 最多返回的文档数

        Returns:
            选中文档的 0-based 下标列表（按相关性排序，失败时为空列表）
        """
        if not candidates:
            return []
        try:
            text = self._call_subcall(
                system_prompt=_DOC_FILTER_SYSTEM_PROMPT,
                messages=build_doc_filter_messages(query, candidates),
                max_tokens=120,
                prefill=_DOC_FILTER_PREFILL,
                subcall="doc_title_filter",
            )
            logger.info(f"Claude 标题过滤输出: {text.strip()[:200]}")

            selected_indices = parse_doc_index_list(text, len(candidates), max_docs)
            if not selected_indices:
                log_subcall_degraded(
                    "doc_title_filter", "未能从输出中解析出有效文档编号", text, query[:50]
                )
            return selected_indices
        except Exception as e:
            log_subcall_degraded("doc_title_filter", f"API 调用失败: {e}", "", query[:50])
            return []

