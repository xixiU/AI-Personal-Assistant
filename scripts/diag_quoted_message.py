#!/usr/bin/env python3
"""
诊断脚本：验证机器人能否回查「未收到推送」的被引用消息及其附件。

背景
----
群聊里用户拖入文件时飞书不会推送事件（权限 im:message.group_at_msg:readonly
只推 @机器人的消息），机器人对该文件毫无感知。随后用户引用该文件消息提问时，
事件里只带 parent_id/root_id 两个 ID，不内联被引用消息的内容。

因此「引用文件消息追问」要能工作，必须用 parent_id 主动回查。本脚本验证这条
路径在当前应用权限下是否可行，决定修复方案走向：
  - 能查到且能下载 -> 改代码补回查逻辑（方案 B）
  - 无权限         -> 只能去开启「获取群组中所有消息」事件（方案 A）

用法
----
    PYTHONPATH=src python scripts/diag_quoted_message.py <message_id>
    PYTHONPATH=src python scripts/diag_quoted_message.py om_x100b62c4b8bb50a038685c0e3d10a1d

凭据从 config.yaml 的 adapters[].bot_api 段读取，不接受命令行传入，
也不打印 app_secret 与 token 明文。
"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
TIMEOUT = 15

# 权限类错误码 -> 人话解释。飞书各部署版本错误码不完全一致，故同时看 HTTP 状态。
PERMISSION_CODES = {
    99991672: "应用无该接口权限（未申请或未发布）",
    99991671: "access_token 无效或已过期",
    230002: "机器人无该消息的可见权限",
    230001: "消息不存在或对该应用不可见",
    232001: "消息已撤回或被删除",
}

# 参数本身不合法/不存在，与权限无关。单独列出来是因为把它误判成「无权限」会
# 直接把结论导向「只能申请权限」，而真正的原因可能只是 message_id 抄错一位。
INVALID_ID_CODES = {
    99992354: "message_id 格式不合法或该 ID 不存在（注意逐字核对，不是权限问题）",
}


def load_bot_api_config() -> Dict[str, Any]:
    """从 config.yaml 取出飞书机器人的 bot_api 配置段。"""
    if not CONFIG_PATH.exists():
        sys.exit(f"配置文件不存在: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # 兼容两种层级：顶层 adapters[]（当前实际结构）与 im.adapters[]（旧结构）
    candidates = (data.get("adapters") or []) + ((data.get("im", {}) or {}).get("adapters") or [])

    for adapter in candidates:
        bot_api = (adapter or {}).get("bot_api") or {}
        if bot_api.get("app_id") and bot_api.get("app_secret"):
            return bot_api

    sys.exit("未在 config.yaml 的 adapters[].bot_api 下找到 app_id/app_secret")


def get_token(base_url: str, app_id: str, app_secret: str) -> str:
    """取 tenant_access_token。与 feishu_bot.get_tenant_access_token 同一接口。"""
    url = f"{base_url}/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(
        url,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=TIMEOUT,
    )
    try:
        body = resp.json()
    except Exception:
        sys.exit(f"取 token 返回非 JSON: HTTP {resp.status_code}, 前 200 字符: {resp.text[:200]}")
    token = body.get("tenant_access_token", "")
    if not token:
        sys.exit(f"取 token 失败: HTTP {resp.status_code}, code={body.get('code')}, msg={body.get('msg')}")
    print(f"[1/3] token 获取成功（{token[:8]}... 已脱敏）")
    return token


def fetch_message(
    base_url: str, token: str, message_id: str
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[int]]:
    """
    回查被引用消息。这是本脚本的核心验证点。

    Returns:
        (是否成功, 消息体 item, 飞书返回的 code)。code 透出给调用方区分
        「ID 不合法」与「无权限」两类失败，二者的修复方向完全不同。
    """
    url = f"{base_url}/open-apis/im/v1/messages/{message_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)

    # requests 抛的是自家的 RequestsJSONDecodeError，它在不同版本下的基类不统一
    # （曾是 ValueError，新版挂在 requests.exceptions 下），只接 ValueError 会漏掉，
    # 导致网关返回 HTML 错误页时脚本直接崩栈而不是给出可读结论。
    try:
        body = resp.json()
    except Exception:
        print(f"[2/3] 返回非 JSON: HTTP {resp.status_code}, 前 200 字符: {resp.text[:200]}")
        return False, None, None

    code = body.get("code")
    print(f"[2/3] GET im/v1/messages/{message_id}  ->  HTTP {resp.status_code}, code={code}")

    if code != 0:
        if code in INVALID_ID_CODES:
            hint = INVALID_ID_CODES[code]
        else:
            hint = PERMISSION_CODES.get(code, "未知错误码，见下方原始返回")
        print(f"      失败原因: {body.get('msg')}  ({hint})")
        print(f"      原始返回: {json.dumps(body, ensure_ascii=False)[:500]}")
        return False, None, code

    items = body.get("data", {}).get("items") or []
    if not items:
        print("      code=0 但 items 为空，拿不到消息体")
        return False, None, code

    item = items[0]
    print(f"      消息类型: msg_type={item.get('msg_type')}, chat_id={item.get('chat_id')}")
    print(f"      发送者:   {(item.get('sender') or {}).get('id', '?')}")
    return True, item, code


def extract_file_keys(item: Dict[str, Any]) -> list:
    """
    从消息体里抽出附件标识。

    file/image 消息的 file_key/image_key 在 body.content（字符串化 JSON）里；
    post 富文本则嵌在多层 content 数组中，这里一并递归搜出来。
    """
    raw = (item.get("body") or {}).get("content") or ""
    try:
        content = json.loads(raw)
    except (ValueError, TypeError):
        print(f"      body.content 不是合法 JSON，原文前 200 字符: {str(raw)[:200]}")
        return []

    found = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("file_key", "image_key"):
                if node.get(key):
                    found.append({
                        "key": node[key],
                        "kind": "file" if key == "file_key" else "image",
                        "name": node.get("file_name") or node.get("name") or "",
                    })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(content)
    return found


def try_download(base_url: str, token: str, message_id: str, attach: Dict[str, str]) -> None:
    """验证附件能否真的下载下来。走的是 feishu_bot.py:219 同一条下载路径。"""
    url = f"{base_url}/open-apis/im/v1/messages/{message_id}/resources/{attach['key']}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"type": attach["kind"]},
        timeout=TIMEOUT,
        stream=True,
    )

    if resp.status_code != 200:
        preview = resp.text[:300] if resp.text else "(空)"
        print(f"      下载失败: HTTP {resp.status_code}, 返回: {preview}")
        return

    size = 0
    head = b""
    for chunk in resp.iter_content(8192):
        if not head:
            head = chunk[:64]
        size += len(chunk)

    print(f"      下载成功: {size} 字节")
    try:
        print(f"      正文开头: {head.decode('utf-8', errors='replace')[:60]!r}")
    except Exception:
        print(f"      正文开头(hex): {head[:16].hex()}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"用法: python {Path(__file__).name} <message_id>")

    message_id = sys.argv[1].strip()
    cfg = load_bot_api_config()
    base_url = (cfg.get("base_url") or "").rstrip("/")

    print(f"飞书地址: {base_url}")
    print(f"目标消息: {message_id}\n")

    token = get_token(base_url, cfg["app_id"], cfg["app_secret"])
    ok, item, code = fetch_message(base_url, token, message_id)

    if not ok:
        # ID 不合法与「无权限」必须分开报，否则会把「抄错一位」误判成「权限不够」，
        # 进而把结论错误地导向「只能申请权限」。
        if code in INVALID_ID_CODES:
            print("\n结论: 本次验证无效 —— message_id 不存在或格式不对，与权限无关。")
            print("      请逐字核对 ID 后重跑（飞书 message_id 形如 om_ + 32 位十六进制字符）。")
            sys.exit(3)
        print("\n结论: 回查不可行 —— 机器人读不到这条未收到推送的消息。")
        print("      方案 B（parent_id 主动回查）走不通，只能开启飞书「获取群组中所有消息」事件（方案 A）。")
        sys.exit(1)

    print("\n[3/3] 从消息体里提取附件标识")
    attaches = extract_file_keys(item)
    if not attaches:
        print("      没找到 file_key / image_key —— 能查到消息但拿不到附件标识。")
        print("\n结论: 半可行 —— 文本能回取，附件不能。若被引用消息确实是文件消息，说明返回体结构与预期不同，需人工看原始 content。")
        sys.exit(2)

    for attach in attaches:
        print(f"      找到 {attach['kind']}: name={attach['name'] or '(无名)'}, key={attach['key'][:20]}...")
        try_download(base_url, token, message_id, attach)

    print("\n结论: 回查可行 —— 消息能查到、附件能下载。方案 B 成立，可据此改代码。")


if __name__ == "__main__":
    main()
