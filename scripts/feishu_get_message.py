"""
测试脚本：验证通过飞书消息 ID 获取消息原文

## 背景
容器重启后 message_id → record_id 的内存缓存会丢失，导致旧消息无法反馈。
本脚本验证方案可行性：点赞/点踩时，直接用飞书 API 通过 message_id 获取消息原文，
从原文里提取 query 再到 chat_history 里查 record_id。

## 飞书 API 信息
- **API Endpoint**: GET /open-apis/im/v1/messages/{message_id}
- **官方文档**: https://open.feishu.cn/document/server-docs/im-v1/message/get
- **认证方式**: tenant_access_token (Bearer)
- **返回数据**: data.items[0] 包含 msg_type、body.content、chat_id、sender 等

## 所需权限 (scope)
需在飞书开放平台后台申请以下权限并发布应用版本：
- im:message:readonly (获取单聊、群组消息，仅机器人可见的消息)
- 或 im:message.group_at_msg:readonly (仅获取群聊中@机器人的消息)

## 限制和注意事项
1. **可见性限制**：只能获取"机器人可见"的消息
   - 私聊：机器人收到的所有消息
   - 群聊（权限为 im:message.group_at_msg:readonly）：仅 @机器人 的消息
   - 群聊（权限为 im:message:readonly）：机器人可见的所有消息

2. **已实测确认**（见 feishu_bot.py 第 227-231 行注释）：
   对一条从未推送给机器人的消息（如群里用户拖入文件，未@机器人），
   本接口返回 HTTP 200 / code=0，能拿到完整的消息内容。

3. **错误码说明**：
   - 99992354: message_id 格式不合法或该 ID 不存在（参数问题）
   - 99991672: 应用无该接口权限（未申请或未发布）
   - 99991671: access_token 无效或已过期
   - 230001: 消息不存在或对该应用不可见
   - 230002: 机器人无该消息的可见权限
   - 232001: 消息已撤回或被删除

## 使用方法
```bash
# 方式1：交互式输入消息 ID
cd D:\\source\\AI-Personal-Assistant
python tests/manual/test_feishu_get_message.py

# 方式2：命令行参数指定消息 ID
python tests/manual/test_feishu_get_message.py om_x100b60f5612448a0386b2b8da8d8a6c
```

## 测试结果记录
### 测试时间：待运行
### 消息 ID：待输入
### 结果：
- [ ] 成功获取消息内容
- [ ] 权限不足（需申请权限）
- [ ] 消息不存在/不可见
- [ ] 其他错误

### 返回的消息字段：
```
待测试后填写
```
"""

import sys
import os
import json
import requests
import yaml
from typing import Optional, Dict, Any

# 修复 Windows 控制台输出编码问题
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """
    从 config.yaml 加载配置

    Args:
        config_path: 配置文件路径

    Returns:
        配置字典
    """
    if not os.path.exists(config_path):
        print(f"❌ 配置文件不存在: {config_path}")
        print("   请确保 config.yaml 存在，或使用 config.example.yaml 作为模板创建")
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    return config


def get_tenant_access_token(base_url: str, app_id: str, app_secret: str) -> Optional[str]:
    """
    获取飞书 tenant_access_token

    Args:
        base_url: 飞书 API 基础 URL
        app_id: 应用 ID
        app_secret: 应用 Secret

    Returns:
        访问令牌，失败返回 None
    """
    url = f"{base_url}/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": app_id,
        "app_secret": app_secret
    }

    print(f"🔑 正在获取 tenant_access_token...")
    print(f"   API: {url}")
    print(f"   app_id: {app_id[:10]}...")

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        result = response.json()

        if result.get("code") == 0:
            token = result["tenant_access_token"]
            print(f"✅ Token 获取成功: {token[:20]}...")
            return token
        else:
            print(f"❌ Token 获取失败: code={result.get('code')}, msg={result.get('msg')}")
            return None

    except Exception as e:
        print(f"❌ Token 获取异常: {e}")
        return None


def get_message(base_url: str, token: str, message_id: str) -> Optional[Dict[str, Any]]:
    """
    通过消息 ID 获取消息详情

    Args:
        base_url: 飞书 API 基础 URL
        token: tenant_access_token
        message_id: 消息 ID

    Returns:
        消息详情字典，失败返回 None
    """
    url = f"{base_url}/open-apis/im/v1/messages/{message_id}"
    headers = {"Authorization": f"Bearer {token}"}

    print(f"\n📨 正在获取消息详情...")
    print(f"   API: {url}")
    print(f"   message_id: {message_id}")

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        result = response.json()

        code = result.get("code")
        if code != 0:
            print(f"\n❌ 获取消息失败:")
            print(f"   code: {code}")
            print(f"   msg: {result.get('msg')}")

            # 给出错误提示
            error_hints = {
                99992354: "❗ message_id 格式不合法或该 ID 不存在（参数问题，不是权限问题）",
                99991672: "❗ 应用无该接口权限（未申请或未发布），需在飞书开放平台申请 im:message:readonly 权限",
                99991671: "❗ access_token 无效或已过期",
                230001: "❗ 消息不存在或对该应用不可见",
                230002: "❗ 机器人无该消息的可见权限",
                232001: "❗ 消息已撤回或被删除",
            }

            if code in error_hints:
                print(f"   提示: {error_hints[code]}")

            return None

        items = (result.get("data") or {}).get("items") or []
        if not items:
            print(f"❌ 返回的 items 为空")
            return None

        message_data = items[0]
        print(f"\n✅ 消息获取成功!")
        return message_data

    except Exception as e:
        print(f"❌ 获取消息异常: {e}")
        return None


def print_message_info(message_data: Dict[str, Any]):
    """
    打印消息详情

    Args:
        message_data: 消息数据字典
    """
    print("\n" + "="*70)
    print("📋 消息详情")
    print("="*70)

    # 基本信息
    print(f"\n【基本信息】")
    print(f"  message_id    : {message_data.get('message_id')}")
    print(f"  chat_id       : {message_data.get('chat_id')}")
    print(f"  chat_type     : {message_data.get('chat_type')}")
    print(f"  message_type  : {message_data.get('msg_type')}")
    print(f"  create_time   : {message_data.get('create_time')}")
    print(f"  update_time   : {message_data.get('update_time')}")

    # 发送人信息
    sender = message_data.get('sender', {})
    print(f"\n【发送人】")
    print(f"  sender_id     : {sender.get('id')}")
    print(f"  sender_type   : {sender.get('sender_type')}")
    print(f"  tenant_key    : {sender.get('tenant_key')}")

    # 消息内容
    msg_type = message_data.get('msg_type')
    body = message_data.get('body', {})
    content_str = body.get('content', '{}')

    print(f"\n【消息内容】")
    print(f"  msg_type: {msg_type}")

    try:
        content_data = json.loads(content_str)
        print(f"  content (已解析):")
        print(json.dumps(content_data, indent=4, ensure_ascii=False))

        # 根据消息类型提取关键信息
        if msg_type == "text":
            text = content_data.get("text", "")
            print(f"\n  📝 文本内容: {text}")
        elif msg_type == "image":
            image_key = content_data.get("image_key", "")
            print(f"\n  🖼️  图片 key: {image_key}")
        elif msg_type == "file":
            file_key = content_data.get("file_key", "")
            file_name = content_data.get("file_name", "")
            print(f"\n  📎 文件 key: {file_key}")
            print(f"  📎 文件名: {file_name}")
        elif msg_type == "post":
            print(f"\n  📄 富文本消息 (post 格式)")

    except json.JSONDecodeError:
        print(f"  content (原始): {content_str}")

    # 其他字段
    print(f"\n【其他字段】")
    mentions = message_data.get('mentions')
    if mentions:
        print(f"  mentions: {mentions}")

    parent_id = message_data.get('parent_id')
    if parent_id:
        print(f"  parent_id: {parent_id} (这是一条回复消息)")

    root_id = message_data.get('root_id')
    if root_id:
        print(f"  root_id: {root_id}")

    print("\n" + "="*70)


def main():
    """主函数"""
    print("="*70)
    print("🧪 飞书消息 ID 获取消息原文测试")
    print("="*70)

    # 1. 加载配置
    print("\n📖 步骤 1: 加载配置文件")
    config = load_config()

    # 提取飞书配置
    feishu_adapter = None
    for adapter in config.get('adapters', []):
        if adapter.get('name') == 'feishu' and adapter.get('enabled'):
            feishu_adapter = adapter
            break

    if not feishu_adapter:
        print("❌ 飞书适配器未启用或未配置")
        sys.exit(1)

    bot_api = feishu_adapter.get('bot_api', {})
    base_url = bot_api.get('base_url', 'https://open.feishu.cn')
    app_id = bot_api.get('app_id', '')
    app_secret = bot_api.get('app_secret', '')

    if not app_id or not app_secret:
        print("❌ 飞书 app_id 或 app_secret 未配置")
        print("   请在 config.yaml 中配置 adapters.feishu.bot_api.app_id 和 app_secret")
        sys.exit(1)

    print(f"✅ 配置加载成功")
    print(f"   base_url: {base_url}")
    print(f"   app_id: {app_id[:10]}...")

    # 2. 获取 message_id
    print("\n📖 步骤 2: 获取消息 ID")
    if len(sys.argv) > 1:
        message_id = sys.argv[1]
        print(f"✅ 从命令行参数获取: {message_id}")
    else:
        print("💡 请输入要查询的消息 ID（可从飞书日志中获取，如 om_xxx 格式）:")
        print("   示例: om_x100b60f5612448a0386b2b8da8d8a6c")
        message_id = input("   message_id: ").strip()

        if not message_id:
            print("❌ 未输入消息 ID")
            sys.exit(1)

    # 3. 获取 token
    print("\n📖 步骤 3: 获取访问令牌")
    token = get_tenant_access_token(base_url, app_id, app_secret)
    if not token:
        sys.exit(1)

    # 4. 获取消息
    print("\n📖 步骤 4: 获取消息详情")
    message_data = get_message(base_url, token, message_id)

    if not message_data:
        print("\n❌ 测试失败")
        sys.exit(1)

    # 5. 打印消息详情
    print_message_info(message_data)

    # 6. 总结
    print("\n" + "="*70)
    print("✅ 测试成功！")
    print("="*70)
    print("\n📊 方案可行性评估:")
    print("  ✅ 能够通过消息 ID 获取消息原文")
    print("  ✅ 返回的内容包含 message_type、content、sender 等完整信息")
    print("  ✅ 可从 content 中提取用户问题（query）")
    print("\n💡 后续实施步骤:")
    print("  1. 在 process_card_action 中，当 message_cache 未命中时")
    print("  2. 调用 self.get_message(message_id) 获取消息原文")
    print("  3. 解析 content 提取 query（文本消息直接取 text，其他类型按规则提取）")
    print("  4. 用 ChatHistoryManager.find_record_by_query(session_id, query) 查询 record_id")
    print("  5. 找到后保存反馈，未找到则返回友好提示")
    print("\n⚠️  注意事项:")
    print("  - 需确保应用已申请 im:message:readonly 权限并发布")
    print("  - 消息撤回/删除后无法获取")
    print("  - 群聊中未@机器人的消息可能不可见（取决于权限范围）")
    print("  - query 提取需考虑多模态消息（纯图片、附件等）的默认提问文本")
    print("="*70)


if __name__ == "__main__":
    main()
