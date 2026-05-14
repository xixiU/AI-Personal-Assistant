#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
飞书文档手动同步脚本

用法:
    uv run sync_docs.py                    # 同步文档 + 建立向量索引
    uv run sync_docs.py --no-index         # 仅同步文档到本地，不处理向量数据库
    uv run sync_docs.py --list-only        # 仅列出目录结构，不下载内容、不缓存
    uv run sync_docs.py --force            # 强制重新获取（清空缓存后重建）
    uv run sync_docs.py --debug            # 输出详细调试信息
    uv run sync_docs.py --config /path/to/config.yaml
"""

import sys
import argparse
from pathlib import Path

# 添加 src 目录到 Python 路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / "src"))


def main():
    parser = argparse.ArgumentParser(description="飞书文档手动同步")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--force", action="store_true", help="强制重新获取（清空缓存后重建）")
    parser.add_argument("--no-index", action="store_true", help="仅同步文档到本地，不建立向量索引")
    parser.add_argument("--list-only", action="store_true", help="仅列出目录结构，不下载内容")
    parser.add_argument("--debug", action="store_true", help="输出详细调试信息")
    args = parser.parse_args()

    # 加载配置
    from ai_assistant.core.config import Config
    config_path = args.config
    if not Path(config_path).exists():
        print(f"❌ 配置文件不存在: {config_path}")
        sys.exit(1)

    config = Config.load(config_path)

    if not config.feishu_docs_enabled:
        print("⚠️  飞书文档功能未启用（feishu_docs.enabled=false），退出")
        sys.exit(0)

    if not config.feishu_docs_sources:
        print("⚠️  未配置 feishu_docs.sources，退出")
        sys.exit(0)

    # 设置日志
    from loguru import logger
    logger.remove()
    log_level = "DEBUG" if args.debug else "INFO"
    logger.add(sys.stdout, level=log_level, format="{time:HH:mm:ss} | {level: <8} | {message}")

    logger.info(f"MCP 服务器: {config.feishu_docs_mcp_url}")
    logger.info(f"文档域名: {getattr(config, 'feishu_docs_doc_base_url', '') or '(未配置)'}")
    logger.info(f"Sources: {config.feishu_docs_sources}")
    logger.info("")

    # 初始化文档管理器
    from ai_assistant.core.feishu_doc_manager import FeishuDocManager

    doc_manager = FeishuDocManager(
        mcp_url=config.feishu_docs_mcp_url,
        cache_dir=config.feishu_docs_cache_dir,
        cache_ttl=config.feishu_docs_cache_ttl,
        sources=config.feishu_docs_sources,
        local_docs=config.local_docs,
        use_gpu=getattr(config, 'vector_db_use_gpu', False),
        gpu_id=getattr(config, 'vector_db_gpu_id', 0),
        batch_size=getattr(config, 'vector_db_batch_size', 32),
        doc_base_url=getattr(config, 'feishu_docs_doc_base_url', ''),
    )

    # 仅列出目录
    if args.list_only:
        items = doc_manager.list_docs()
        if not items:
            logger.warning("未列出任何节点（MCP 可能未正常返回）")
            sys.exit(1)

        # 按类型分组统计
        folder_count = sum(1 for i in items if i["type"] == "folder")
        doc_count = len(items) - folder_count

        logger.info(f"\n共 {len(items)} 个节点（文件夹 {folder_count}, 文档 {doc_count}）\n")

        for item in items:
            icon = "📁" if item["type"] == "folder" else "📄"
            url_info = f" → {item['url']}" if item["url"] else ""
            logger.info(f"  {icon} [{item['type']}] {item['title']} (token={item['token']}){url_info}")
        sys.exit(0)

    # 同步文档到本地
    docs = doc_manager.sync_docs(force=args.force)

    if not docs:
        logger.warning("未同步到任何文档")
        sys.exit(0)

    # 输出文档列表
    for doc in docs:
        url = doc.get("url", "")
        url_info = f" → {url}" if url else ""
        logger.info(f"  📄 {doc['title']}{url_info}")

    # 建立向量索引（可选）
    if not args.no_index:
        logger.info("\n正在建立向量索引...")
        doc_manager._ensure_indexed()
        logger.info("✅ 文档同步 + 向量索引建立完成")
    else:
        logger.info("\n✅ 文档同步完成（跳过向量索引）")


if __name__ == "__main__":
    main()
