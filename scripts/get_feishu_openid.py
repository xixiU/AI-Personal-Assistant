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

    3. 方法三：通过飞书API查询（需要 tenant_access_token）
       python scripts/get_feishu_openid.py --api --token <tenant_access_token>
"""

import sys
import os
import argparse
import json
import re
from datetime import datetime

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


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
        print(f"   消息数: {info['count']}")
        print(f"   最后活跃: {info['last_time']}")
        print()

    print("-" * 60)
    print("📋 配置文件格式（复制到 config.yaml）:\n")
    print("operations:")
    print("  authorization:")
    print("    operators:")
    print("      feishu:")
    for open_id, info in sorted_users:
        print(f'        - open_id: "{open_id}"')
        print(f'          name: "{info["name"]}"')


def query_via_api(tenant_access_token):
    """通过飞书API查询当前用户 open_id"""
    import requests

    print("🌐 正在通过飞书API查询...")
    print("-" * 60)

    url = "https://open.feishu.cn/open-apis/contact/v3/users/me"
    headers = {"Authorization": f"Bearer {tenant_access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        result = response.json()

        if result.get('code') == 0:
            user_data = result.get('data', {}).get('user', {})
            open_id = user_data.get('open_id', 'N/A')
            name = user_data.get('name', 'N/A')

            print("✅ 查询成功!\n")
            print(f"Open ID: {open_id}")
            print(f"姓名: {name}")
            print()
            print("-" * 60)
            print("📋 配置文件格式:\n")
            print("operations:")
            print("  authorization:")
            print("    operators:")
            print("      feishu:")
            print(f'        - open_id: "{open_id}"')
            print(f'          name: "{name}"')
        else:
            print(f"❌ API 调用失败: {result.get('msg', 'Unknown error')}")
            print(f"错误码: {result.get('code')}")

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

  # 通过API查询（需要 tenant_access_token）
  python scripts/get_feishu_openid.py --api --token <your_token>
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
        '--api',
        action='store_true',
        help='通过飞书API查询'
    )

    parser.add_argument(
        '--token',
        help='tenant_access_token（使用 --api 时必需）'
    )

    args = parser.parse_args()

    # 优先级: --listen > --api > --from-log（默认）
    if args.listen:
        listen_realtime()
    elif args.api:
        if not args.token:
            parser.error("--api 需要提供 --token <tenant_access_token>")
        query_via_api(args.token)
    else:
        # 默认从日志查询
        parse_log_file(args.log_file, args.lines)


if __name__ == '__main__':
    main()
