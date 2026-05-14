#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
飞书文档手动同步脚本

用法:
    uv run sync_docs.py                    # 同步文档 + 建立向量索引
    uv run sync_docs.py --no-index         # 仅同步文档到本地，不处理向量数据库
    uv run sync_docs.py --force            # 强制重新获取（清空缓存后重建）
    uv run sync_docs.py --debug            # 输出详细调试信息（含 MCP 原始返回）
    uv run sync_docs.py --list-only        # 仅列出目录结构，不下载文档内容
    uv run sync_docs.py --config /path/to/config.yaml
"""

import sys
import json
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
    parser.add_argument("--debug", action="store_true", help="输出详细调试信息")
    parser.add_argument("--list-only", action="store_true", help="仅列出目录结构，不下载文档内容")
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

    # 强制模式：清空缓存
    if args.force:
        import shutil
        cache_dir = Path(config.feishu_docs_cache_dir)
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
            logger.info(f"已清空缓存目录: {cache_dir}")

    # 初始化 MCP 客户端
    from ai_assistant.core.simple_mcp_client import SimpleMCPClient
    mcp_client = SimpleMCPClient(config.feishu_docs_mcp_url)

    doc_base_url = getattr(config, 'feishu_docs_doc_base_url', '')

    logger.info(f"MCP 服务器: {config.feishu_docs_mcp_url}")
    logger.info(f"文档域名: {doc_base_url or '(未配置)'}")
    logger.info(f"Sources: {config.feishu_docs_sources}")
    logger.info("")

    total_docs = 0

    for source_token in config.feishu_docs_sources:
        logger.info(f"{'='*60}")
        logger.info(f"处理 source: {source_token}")
        logger.info(f"{'='*60}")

        # 第一步：获取目录树
        logger.info("正在获取目录树...")
        try:
            raw_result = mcp_client.list_children(source_token, type_hint="auto", recursive=True)
        except Exception as e:
            logger.error(f"MCP list_children 调用失败: {e}")
            continue

        # 调试：输出原始返回
        if args.debug:
            logger.debug(f"MCP 原始返回类型: {type(raw_result)}")
            if isinstance(raw_result, (list, dict)):
                logger.debug(f"MCP 原始返回（前 2000 字符）:\n{json.dumps(raw_result, ensure_ascii=False, indent=2)[:2000]}")
            else:
                logger.debug(f"MCP 原始返回: {str(raw_result)[:2000]}")

        # 解析节点
        items = _parse_children(raw_result)
        logger.info(f"解析到 {len(items)} 个节点")

        if not items:
            logger.warning("未获取到任何节点，可能原因：")
            logger.warning("  1. source token 不正确")
            logger.warning("  2. MCP 服务器返回格式不匹配")
            logger.warning("  3. 权限不足")
            logger.warning(f"  原始返回: {str(raw_result)[:500]}")
            continue

        # 列出目录结构
        doc_items = []
        folder_items = []
        for item in items:
            node_type = item.get("type", "")
            if node_type == "folder":
                folder_items.append(item)
            else:
                doc_items.append(item)

        logger.info(f"  文件夹: {len(folder_items)} 个")
        logger.info(f"  文档: {len(doc_items)} 个")
        logger.info("")

        # 打印目录结构
        if args.list_only or args.debug:
            logger.info("目录结构：")
            for item in items:
                name = item.get("name") or item.get("title") or "未知"
                node_type = item.get("type", "unknown")
                token = item.get("obj_token") or item.get("token", "")
                depth = item.get("depth", 0)
                indent = "  " * (depth + 1)
                icon = "📁" if node_type == "folder" else "📄"
                logger.info(f"{indent}{icon} {name} (type={node_type}, token={token})")
            logger.info("")

        if args.list_only:
            continue

        # 第二步：下载文档内容
        logger.info("正在下载文档内容...")
        cache_dir = Path(config.feishu_docs_cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

        docs = []
        for i, item in enumerate(doc_items):
            title = item.get("name") or item.get("title") or "未知"
            obj_token = item.get("obj_token") or item.get("token", "")
            node_token = item.get("token", "")

            if not obj_token:
                continue

            logger.info(f"  [{i+1}/{len(doc_items)}] 下载: {title}")

            try:
                content = mcp_client.read_document(obj_token)
                if isinstance(content, dict):
                    content = content.get("content") or content.get("text") or json.dumps(content, ensure_ascii=False)
                elif not isinstance(content, str):
                    content = str(content) if content else ""
            except Exception as e:
                logger.warning(f"    ❌ 下载失败: {e}")
                continue

            if not content or not content.strip():
                logger.debug(f"    ⚠️  内容为空，跳过")
                continue

            # 构建 URL
            url = ""
            if doc_base_url:
                base = doc_base_url.rstrip('/')
                if node_token and node_token != obj_token:
                    url = f"{base}/wiki/{node_token}"
                else:
                    url = f"{base}/docx/{obj_token}"

            docs.append({
                "title": title,
                "token": obj_token,
                "node_token": node_token,
                "path": title,
                "content": content,
                "url": url,
            })
            logger.info(f"    ✅ {len(content)} 字符{f', URL: {url}' if url else ''}")

        logger.info(f"\n同步完成: {len(docs)}/{len(doc_items)} 篇文档")
        total_docs += len(docs)

        # 第三步：保存到本地缓存
        if docs:
            _save_docs_to_cache(cache_dir, source_token, docs, logger)

    logger.info(f"\n{'='*60}")
    logger.info(f"全部完成: 共 {total_docs} 篇文档已同步到本地")

    # 第四步：建立向量索引（可选）
    if not args.no_index and total_docs > 0:
        logger.info("\n正在建立向量索引...")
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
            doc_base_url=doc_base_url,
        )
        doc_manager._ensure_indexed()
        logger.info("✅ 向量索引建立完成")
    elif args.no_index:
        logger.info("\n⏭️  跳过向量索引（--no-index）")


def _parse_children(result) -> list:
    """解析 list_children 返回结果"""
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in ("items", "nodes", "children", "files"):
            if key in result and isinstance(result[key], list):
                return [item for item in result[key] if isinstance(item, dict)]
    return []


def _save_docs_to_cache(cache_dir: Path, source_token: str, docs: list, logger):
    """保存文档到本地缓存"""
    import re
    import time as time_mod

    safe_name = re.sub(r'[<>:"/\\|?*]', '_', source_token)
    source_dir = cache_dir / safe_name
    source_dir.mkdir(parents=True, exist_ok=True)

    doc_infos = []
    for doc in docs:
        doc_path = doc.get("path", doc["title"])
        safe_path = re.sub(r'[<>:"|?*]', '_', doc_path)
        filename = f"{safe_path}.txt"

        content_file = source_dir / filename
        content_file.parent.mkdir(parents=True, exist_ok=True)
        content_file.write_text(doc["content"], encoding="utf-8")

        doc_infos.append({
            "title": doc["title"],
            "token": doc.get("token", ""),
            "path": doc.get("path", ""),
            "edit_time": doc.get("edit_time"),
            "url": doc.get("url", ""),
            "filename": filename,
        })

    metadata = {
        "source_token": source_token,
        "cached_at": time_mod.time(),
        "total_docs": len(docs),
        "documents": doc_infos,
    }
    metadata_file = source_dir / "metadata.json"
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    logger.info(f"缓存已保存: {source_dir} ({len(docs)} 篇)")


if __name__ == "__main__":
    main()
