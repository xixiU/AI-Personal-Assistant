#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
飞书文档手动同步脚本

用法:
    uv run sync_docs.py              # 使用默认配置文件 config.yaml
    uv run sync_docs.py --config /path/to/config.yaml
    uv run sync_docs.py --force      # 强制重新获取（忽略缓存 TTL）
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
    logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")

    # 强制模式：清空缓存
    if args.force:
        import shutil
        cache_dir = Path(config.feishu_docs_cache_dir)
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
            logger.info(f"已清空缓存目录: {cache_dir}")

    # 初始化文档管理器
    logger.info(f"开始同步飞书文档: sources={config.feishu_docs_sources}")

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

    # 触发索引（内部会自动获取文档、建立向量索引）
    doc_manager._ensure_indexed()

    logger.info("✅ 飞书文档同步完成")


if __name__ == "__main__":
    main()
