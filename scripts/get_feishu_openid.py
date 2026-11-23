#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书 Open ID 查询工具

用途：查询自己或团队成员的飞书 open_id，用于配置运维权限白名单

使用方法：
    1. 方法一：通过飞书机器人查询（推荐）
       给机器人发送任意消息，脚本会实时监听并打印 open_id

    2. 方法二：通过日志文件查询
       python scripts/get_feishu_openid.py --from-log

    3. 方法三：通过 open_id 查询用户详细信息
       python scripts/get_feishu_openid.py --open-id ou_xxx
"""

import sys
import os
import argparse
import json
import re
import yaml
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def load_feishu_config():
    """从 config.yaml 加载飞书配置"""
    import yaml

    config_file = 'config.yaml'

    if not os.path.exists(config_file):
        print(f"❌ 配置文件不存在: {config_file}")
        return None

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        # 尝试从 adapters 列表中找 feishu 配置
        adapters = config.get('adapters', [])
        feishu_adapter = None
        for adapter in adapters:
            if adapter.get('name') == 'feishu':
                feishu_adapter = adapter
                break

        if not feishu_adapter:
            print("❌ config.yaml 的 adapters 中未找到 name='feishu' 的配置")
            return None

        bot_api = feishu_adapter.get('bot_api', {})
        app_id = bot_api.get('app_id')
        app_secret = bot_api.get('app_secret')
        base_url = bot_api.get('base_url', 'https://open.feishu.cn')

        if not app_id or not app_secret:
            print("❌ config.yaml 中缺少飞书配置（adapters[name=feishu].bot_api.app_id 或 app_secret）")
            return None

        return {
            'app_id': app_id,
            'app_secret': app_secret,
            'base_url': base_url
        }

    except Exception as e:
        print(f"❌ 读取配置文件失败: {e}")
        return None


def get_tenant_access_token(app_id: str, app_secret: str, base_url: str = 'https://open.feishu.cn'):
    """获取飞书 tenant_access_token"""
    import requests

    url = f"{base_url}/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": app_id,
        "app_secret": app_secret
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()

        if result.get('code') == 0:
            return result.get('tenant_access_token')
        else:
            print(f"❌ 获取 token 失败: {result.get('msg', 'Unknown error')}")
            return None

    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return None


def parse_log_file(log_file='logs/app.log', lines=100):
    """从日志文件中解析 open_id"""
    print(f"📄 正在分析日志文件: {log_file} (最近 {lines} 行)")
    print("-" * 60)

    if not os.path.exists(log_file):
        print(f"❌ 日志文件不存在: {log_file}")
        print("提示: 请先运行应用生成日志，或检查日志文件路径")
        return

    # 正则匹配 open_id
    pattern = re.compile(r'(open_id|sender_id|user_id)["\s:=]+([a-z0-9_]+)', re.IGNORECASE)

    users = {}  # {open_id: {name, last_time, count}}

    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
        all_lines = f.readlines()
        recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

        for line in recent_lines:
            matches = pattern.findall(line)
            if not matches:
                continue

            for _, open_id in matches:
                if not open_id.startswith('ou_'):
                    continue

                # 提取时间戳（如果有）
                time_match = re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', line)
                timestamp = time_match.group(0) if time_match else "N/A"

                # 提取用户名（如果有）
                name_match = re.search(r'(name|sender_name|display_name)["\s:=]+([^",\s]+)', line, re.IGNORECASE)
                name = name_match.group(2) if name_match else "未知"

                if open_id not in users:
                    users[open_id] = {
                        'name': name,
                        'last_time': timestamp,
                        'count': 0
                    }

                users[open_id]['count'] += 1
                users[open_id]['last_time'] = timestamp
                if name != "未知":
                    users[open_id]['name'] = name

    if not users:
        print("❌ 未找到任何 open_id")
        print("提示: 请先给飞书机器人发送消息，然后重新运行此脚本")
        return

    print(f"✅ 找到 {len(users)} 个用户的 open_id:\n")

    # 按消息数量排序
    sorted_users = sorted(users.items(), key=lambda x: x[1]['count'], reverse=True)

    for idx, (open_id, info) in enumerate(sorted_users, 1):
        print(f"{idx}. Open ID: {open_id}")
        print(f"   姓名: {info['name']}")

def query_via_api(open_id=None):
    """通过飞书API查询用户信息（自动从 config.yaml 读取配置）

    Args:
        open_id: 用户的 open_id（可选，不传则查询当前用户）
    """
    import requests

    print("🌐 正在通过飞书API查询...")
    print("-" * 60)

    # 加载配置
    config = load_feishu_config()
    if not config:
        return

    # 获取 token
    print("正在获取 tenant_access_token...")
    token = get_tenant_access_token(config['app_id'], config['app_secret'], config['base_url'])
    if not token:
        return

    print("✅ Token 获取成功\n")

    # 如果指定了 open_id，查询指定用户；否则查询当前用户
    if open_id:
        url = f"{config['base_url']}/open-apis/contact/v3/users/{open_id}"
        print(f"查询用户: {open_id}\n")
    else:
        url = f"{config['base_url']}/open-apis/contact/v3/users/me"
        print("查询当前用户\n")

    headers = {"Authorization": f"Bearer {token}"}
    params = {"user_id_type": "open_id"} if open_id else {}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        result = response.json()

        if result.get('code') == 0:
            user_data = result.get('data', {}).get('user', {})

            print("✅ 查询成功!\n")
            print("=" * 60)
            print("用户完整信息：")
            print("=" * 60)

            # 打印所有字段
            for key, value in sorted(user_data.items()):
                print(f"{key:20s}: {value}")

            print("=" * 60)
            print("\n重要字段说明：")
            print("-" * 60)
            print(f"open_id        : {user_data.get('open_id', 'N/A')} （用户唯一标识）")
            print(f"name           : {user_data.get('name', 'N/A')} （中文姓名）")
            print(f"en_name        : {user_data.get('en_name', 'N/A')} （英文名）")
            print(f"email          : {user_data.get('email', 'N/A')} （邮箱）")
            print(f"enterprise_email: {user_data.get('enterprise_email', 'N/A')} （企业邮箱）")
            print(f"employee_no    : {user_data.get('employee_no', 'N/A')} （工号）")
            print(f"mobile         : {user_data.get('mobile', 'N/A')} （手机号）")

            # 提取域账号建议
            user_id = user_data.get('user_id', '')
            email = user_data.get('email') or user_data.get('enterprise_email', '')
            email_prefix = email.split('@')[0] if '@' in email else ''

            print("\n" + "=" * 60)
            print("建议的 username（域账号）:")
            print("=" * 60)
            if user_id:
                print(f"从 user_id 提取（推荐）: {user_id}")
            if email_prefix:
                print(f"从邮箱提取（备选）: {email_prefix}")
            if not user_id and not email_prefix:
                print("N/A - 建议使用 feishu_{open_id[:8]}")

            print()
            print("-" * 60)
            print("📋 配置文件格式:\n")
            print("operations:")
            print("  authorization:")
            print("    operators:")
            print("      feishu:")
            print(f'        - open_id: "{user_data.get("open_id", "")}"')
            print(f'          name: "{user_data.get("name", "")}"')
        else:
            print(f"❌ API 调用失败: {result.get('msg', 'Unknown error')}")
            print(f"错误码: {result.get('code')}")
            print(f"完整响应: {json.dumps(result, indent=2, ensure_ascii=False)}")

    except Exception as e:
        print(f"❌ 请求失败: {e}")


def listen_realtime():
    """实时监听飞书消息（需要应用正在运行）"""
    print("👂 实时监听模式")
    print("-" * 60)
    print("请给飞书机器人发送任意消息，脚本会自动打印 open_id")
    print("按 Ctrl+C 退出\n")

    log_file = 'logs/app.log'

    if not os.path.exists(log_file):
        print(f"❌ 日志文件不存在: {log_file}")
        print("提示: 请先启动应用")
        return

    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            # 跳到文件末尾
            f.seek(0, 2)

            pattern = re.compile(r'(open_id|sender_id)["\s:=]+([a-z0-9_]+)', re.IGNORECASE)
            seen_ids = set()

            while True:
                line = f.readline()
                if not line:
                    import time
                    time.sleep(0.5)
                    continue

                matches = pattern.findall(line)
                for _, open_id in matches:
                    if open_id.startswith('ou_') and open_id not in seen_ids:
                        seen_ids.add(open_id)

                        # 提取姓名
                        name_match = re.search(r'(name|sender_name)["\s:=]+([^",\s]+)', line, re.IGNORECASE)
                        name = name_match.group(2) if name_match else "未知"

                        timestamp = datetime.now().strftime("%H:%M:%S")
                        print(f"[{timestamp}] 检测到新用户:")
                        print(f"  Open ID: {open_id}")
                        print(f"  姓名: {name}")
                        print()

    except KeyboardInterrupt:
        print("\n\n监听已停止")


def main():
    parser = argparse.ArgumentParser(
        description='飞书 Open ID 查询工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 从日志文件查询（默认）
  python scripts/get_feishu_openid.py

  # 从日志文件查询（指定行数）
  python scripts/get_feishu_openid.py --from-log --lines 200

  # 实时监听新消息（推荐）
  python scripts/get_feishu_openid.py --listen

  # 通过 open_id 查询用户详细信息（自动从 config.yaml 读取配置）
  python scripts/get_feishu_openid.py --open-id ou_xxx
        """
    )

    parser.add_argument(
        '--from-log',
        action='store_true',
        help='从日志文件中解析 open_id（默认方式）'
    )

    parser.add_argument(
        '--lines',
        type=int,
        default=100,
        help='分析日志文件的最近 N 行（默认100）'
    )

    parser.add_argument(
        '--log-file',
        default='logs/app.log',
        help='指定日志文件路径（默认 logs/app.log）'
    )

    parser.add_argument(
        '--listen',
        action='store_true',
        help='实时监听模式（需要应用正在运行）'
    )

    parser.add_argument(
        '--open-id',
        help='查询指定用户的详细信息（自动从 config.yaml 读取飞书配置）'
    )

    args = parser.parse_args()

    # 优先级: --listen > --open-id > --from-log（默认）
    if args.listen:
        listen_realtime()
    elif args.open_id:
        query_via_api(args.open_id)
    else:
        # 默认从日志查询
        parse_log_file(args.log_file, args.lines)
        # 默认从日志查询
        parse_log_file(args.log_file, args.lines)


if __name__ == '__main__':
    main()
