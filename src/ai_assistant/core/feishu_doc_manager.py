"""
飞书文档管理器

基于配置的知识库/云空间 token 列表，通过 MCP 工具递归获取文档内容并缓存。
使用统一的 list_children 和 read_document 工具，自动识别知识库和云空间。
"""

import json
import re
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

from ai_assistant.core.simple_mcp_client import SimpleMCPClient
from ai_assistant.core.hybrid_search import HybridSearchEngine
from ai_assistant.core.ai_provider import KeywordExtractionResult


class FeishuDocManager:
    """飞书文档管理器，支持本地缓存和递归目录遍历"""

    MAX_DEPTH = 10  # 递归深度限制
    MAX_DOCS_IN_PROMPT = 3  # 注入 prompt 时的最大文档数

    def __init__(
        self,
        mcp_url: str,
        cache_dir: str,
        cache_ttl: int = 86400,
        sources: List[str] = None,
        keyword_extractor=None,
        local_docs: List[Dict[str, str]] = None,
        use_gpu: bool = False,
        gpu_id: int = 0,
        batch_size: int = 32,
        doc_base_url: str = "",
    ):
        """
        Args:
            mcp_url: MCP 服务器 SSE 端点
            cache_dir: 本地缓存目录
            cache_ttl: 缓存有效期（秒），默认 1 天
            sources: 知识库/云空间 token 列表，限定搜索范围
            keyword_extractor: 关键词提取函数（可选）
            local_docs: 本地离线文档配置列表 [{path, description, keywords}]
            use_gpu: 是否使用 GPU 加速 Embedding 生成
            gpu_id: 使用哪张 GPU（0 或 1）
            batch_size: Embedding 批处理大小（GPU: 128-256, CPU: 16-32）
            doc_base_url: 飞书文档域名（如 https://xxx.feishu.cn），用于生成文档链接
        """
        self.mcp_client = SimpleMCPClient(mcp_url)
        self.cache_dir = Path(cache_dir)
        self.cache_ttl = cache_ttl
        # 规范化 sources 为 [{"token": ..., "type": "wiki"|"drive"}, ...]
        self.sources = self._normalize_sources(sources or [])
        self._keyword_extractor = keyword_extractor
        self._local_docs_config = local_docs or []
        self._lock = threading.Lock()
        self._indexed = False
        self._indexing_in_progress = False  # 是否有线程正在索引
        self._doc_base_url = doc_base_url.rstrip('/') if doc_base_url else ""
        self._sync_interval = 1800  # 定时同步间隔（秒），默认 30 分钟
        self._sync_thread = None
        self._stop_event = threading.Event()
        self._ai_provider = None  # AI provider 用于标题过滤，在初始化后设置

        # 混合检索引擎（支持 GPU 加速）
        vector_db_dir = str(Path(cache_dir) / "_vector_db")
        self._search_engine = HybridSearchEngine(
            persist_dir=vector_db_dir,
            use_gpu=use_gpu,
            gpu_id=gpu_id,
            batch_size=batch_size
        )

        # 确保缓存目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"飞书文档管理器初始化: cache_dir={cache_dir}, ttl={cache_ttl}s, "
            f"sources={self.sources}, use_gpu={use_gpu}, gpu_id={gpu_id}"
        )

        # 启动后台定时同步线程
        self._start_background_sync()

    def _start_background_sync(self):
        """启动后台定时同步线程，首次启动时立即执行一次同步"""
        if not self.sources:
            return

        self._sync_thread = threading.Thread(
            target=self._background_sync_loop,
            name="feishu-doc-sync",
            daemon=True
        )
        self._sync_thread.start()
        logger.info(f"后台文档同步线程已启动，间隔 {self._sync_interval}s")

    def _background_sync_loop(self):
        """后台同步循环：启动时立即同步一次，之后每隔 N 秒增量检查"""
        # 启动时立即同步一次
        self._do_incremental_sync()

        while not self._stop_event.is_set():
            # 等待下一次同步（可被 stop 中断）
            if self._stop_event.wait(timeout=self._sync_interval):
                break
            self._do_incremental_sync()

    def _do_incremental_sync(self):
        """执行一次增量同步：比对 edit_time，只更新有变化的文档，增量更新索引"""
        try:
            logger.info("开始定时增量同步...")

            all_docs = self.sync_docs(force=False, ignore_ttl=True)

            if all_docs:
                # sync_docs 内部发现文档有更新时会设 self._indexed=False
                # 因此在 sync_docs 之后检查 _indexed 来决定是否需要重建
                with self._lock:
                    need_rebuild = not self._indexed

                if need_rebuild:
                    # 抢占索引权：只有一个线程能执行索引
                    with self._lock:
                        if self._indexing_in_progress:
                            logger.info("其他线程正在索引中，跳过本次索引重建")
                            return
                        self._indexing_in_progress = True

                    try:
                        logger.info(f"[后台同步] 开始重建索引...({len(all_docs)} 篇文档)")
                        self._search_engine.index_documents(all_docs)

                        with self._lock:
                            self._indexed = True
                            self._indexing_in_progress = False

                        online_count = sum(1 for d in all_docs if not d.get("token", "").startswith("local_"))
                        local_count = len(all_docs) - online_count
                        logger.info(f"[后台同步] 索引重建完成: {len(all_docs)} 篇文档（在线 {online_count}, 本地 {local_count}）")
                    except Exception as e:
                        with self._lock:
                            self._indexing_in_progress = False
                        raise e
                else:
                    logger.info(f"定时同步完成，文档无变化，跳过索引重建")
            else:
                logger.info("定时同步完成，无文档")
        except Exception as e:
            logger.error(f"定时同步异常: {e}")

    def stop_background_sync(self):
        """停止后台同步线程（用于优雅退出）"""
        self._stop_event.set()
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5)
            logger.info("后台文档同步线程已停止")

    @staticmethod
    def _normalize_sources(sources: List[Any]) -> List[Dict[str, str]]:
        """
        规范化 sources 配置，支持两种格式：
        1. 字符串：默认按 wiki 处理
        2. 字典：{"token": "xxx", "type": "wiki"|"drive"}
        """
        normalized = []
        for s in sources:
            if isinstance(s, str):
                # 兼容旧格式：纯字符串默认按 wiki 处理
                normalized.append({"token": s, "type": "wiki"})
            elif isinstance(s, dict) and s.get("token"):
                source_type = s.get("type", "wiki")
                if source_type not in ("wiki", "drive"):
                    logger.warning(f"未知 source type: {source_type}，按 wiki 处理")
                    source_type = "wiki"
                normalized.append({"token": s["token"], "type": source_type})
            else:
                logger.warning(f"忽略无效的 source 配置: {s}")
        return normalized

    def get_documents_by_query(self, query_text: str) -> str:
        """
        根据查询文本获取相关文档内容

        工作流程：
        1. 确保文档已缓存且已建索引
        2. 使用混合检索（向量 + BM25）找到相关文档
        3. 拼接后返回

        Args:
            query_text: 用户查询文本

        Returns:
            拼接后的文档内容字符串，可直接注入 system prompt
        """
        if not self.sources:
            logger.warning("未配置飞书文档 sources，跳过文档检索")
            return ""

        # 清理 query：去掉飞书 @ 提及
        cleaned_query = re.sub(r'@_user_\d+\s*', '', query_text)
        cleaned_query = cleaned_query.strip()

        # 用大模型提取关键词 + 判断是否通用技术问题
        kw_result = self._extract_keywords(cleaned_query)

        # 通用技术问题（Redis/Nginx/Docker 等标准组件问题）直接跳过文档检索
        if kw_result.is_generic_tech:
            logger.info(
                f"问题分类为通用技术问题，跳过文档检索（直接用大模型知识回答）: '{cleaned_query[:60]}'"
            )
            return ""

        keywords = kw_result.keywords
        if keywords:
            # 关键词拼接到 query 前面，提高权重
            enhanced_query = " ".join(keywords) + " " + cleaned_query
            logger.info(f"关键词增强检索: '{cleaned_query}' → keywords={keywords}")
        else:
            enhanced_query = cleaned_query

        # 确保文档已加载并建立索引
        self._ensure_indexed()

        # 检查索引是否就绪（可能正在后台重建）
        if not self._indexed:
            logger.warning(f"文档索引正在后台重建中，暂时无法检索，请稍后再试")
            return ""

        # 使用混合检索（返回更多候选）
        candidates = self._search_engine.search(enhanced_query, top_k=self.MAX_DOCS_IN_PROMPT)

        if not candidates:
            logger.info(f"检索无结果: '{query_text[:50]}'")
            return ""

        # 用 AI 过滤标题：判断哪些文档标题与 query 相关
        results = self._filter_docs_by_ai(cleaned_query, candidates)

        if not results:
            logger.info(f"AI 过滤后无结果，使用原始检索结果")
            results = candidates[:self.MAX_DOCS_IN_PROMPT]

        # 拼接文档内容，限制总字符数避免 prompt 过大
        max_total_chars = 50000
        result_parts = ["以下是相关的飞书知识库文档内容：\n"]
        total_chars = 0
        included = []
        for doc in results:
            content = doc["content"]
            # 单篇文档截断（避免超大文件占满配额）
            if len(content) > 15000:
                content = content[:15000] + "\n...（文档内容过长，已截断）"
            if total_chars + len(content) > max_total_chars:
                logger.debug(f"总字符数达到上限 {max_total_chars}，跳过: {doc['title']}")
                continue

            # 文档标题 + 原始链接
            doc_url = doc.get("url", "")
            if doc_url:
                result_parts.append(f"## {doc['title']}\n📎 原文链接: {doc_url}")
            else:
                result_parts.append(f"## {doc['title']}")
            result_parts.append(content)
            result_parts.append("")
            total_chars += len(content)
            included.append(doc['title'])

        if not included:
            return ""

        result = "\n".join(result_parts)
        logger.info(f"文档检索完成: {len(included)} 篇, {len(result)} 字符, 匹配文档: {included}")
        return result

    def _ensure_indexed(self):
        """确保文档已建立检索索引（后台同步线程负责加载和更新）"""
        # 快速路径：已经建立索引，直接返回
        if self._indexed:
            return

        # 检查是否有其他线程正在索引
        with self._lock:
            if self._indexed:
                return
            if self._indexing_in_progress:
                logger.info("其他线程正在索引中，跳过重复加载")
                return
            self._indexing_in_progress = True

        try:
            all_docs = []
            for source in self.sources:
                cached = self._load_from_cache(source["token"])
                if cached:
                    all_docs.extend(cached)
            local_docs = self._load_all_local_docs()
            all_docs.extend(local_docs)

            if all_docs:
                logger.info(f"[前台请求] 从本地缓存加载索引: {len(all_docs)} 篇文档")
                self._search_engine.index_documents(all_docs)

                with self._lock:
                    self._indexed = True
                    self._indexing_in_progress = False
                logger.info(f"[前台请求] 索引加载完成: {len(all_docs)} 篇文档")
            else:
                with self._lock:
                    self._indexing_in_progress = False
        except Exception:
            with self._lock:
                self._indexing_in_progress = False
            raise

    def sync_docs(self, force: bool = False, ignore_ttl: bool = False) -> List[Dict[str, str]]:
        """
        同步所有文档到本地缓存（不建立向量索引）

        Args:
            force: 是否强制重新获取（清空缓存全量拉取）
            ignore_ttl: 是否忽略 TTL 强制检查更新（增量比对 edit_time）

        Returns:
            所有文档列表
        """
        if force:
            import shutil
            if self.cache_dir.exists():
                shutil.rmtree(self.cache_dir)
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"已清空缓存目录: {self.cache_dir}")
            self._indexed = False

        all_docs = []

        # 在线飞书文档
        for source in self.sources:
            docs = self._get_docs_for_source(source["token"], source["type"], ignore_ttl=ignore_ttl)
            all_docs.extend(docs)

        # 本地离线文档
        local_docs = self._load_all_local_docs()
        all_docs.extend(local_docs)

        logger.info(f"文档同步完成: 共 {len(all_docs)} 篇（在线 {len(all_docs) - len(local_docs)}, 本地 {len(local_docs)}）")
        return all_docs

    def list_docs(self) -> List[Dict[str, Any]]:
        """
        仅列出所有 source 下的目录结构（不下载内容、不缓存、不建索引）

        用于排查 MCP 是否能正常获取目录。

        Returns:
            节点列表 [{"title", "type", "token", "obj_token", "url", "source"}]
        """
        all_items = []

        for source in self.sources:
            source_token = source["token"]
            source_type = source["type"]
            logger.info(f"列出目录: source={source_token}, type={source_type}")
            try:
                children = self._mcp_list_children(source_token, source_type, recursive=True)
                items = self._parse_children(children)
                logger.info(f"  解析到 {len(items)} 个节点")

                for item in items:
                    title = item.get("name") or item.get("title") or "未知"
                    node_type = item.get("type", "unknown")
                    node_token = item.get("token", "")
                    obj_token = item.get("obj_token") or node_token

                    # 仅文档构建 URL，folder 不需要
                    url = ""
                    if node_type not in ("folder",) and obj_token:
                        url = self._build_doc_url(node_token, obj_token, node_type, source_type)

                    all_items.append({
                        "title": title,
                        "type": node_type,
                        "token": node_token,
                        "obj_token": obj_token,
                        "url": url,
                        "source": source_token,
                        "source_type": source_type,
                    })
            except Exception as e:
                logger.error(f"  list 失败: {e}")

        return all_items

    def _load_all_local_docs(self) -> List[Dict[str, str]]:
        """加载所有本地离线文档"""
        import os
        docs = []
        for doc_config in self._local_docs_config:
            path = doc_config.get("path", "")
            description = doc_config.get("description", "")

            if not path or not os.path.isdir(path):
                continue

            for root, dirs, files in os.walk(path):
                for fname in sorted(files):
                    if not fname.endswith(('.txt', '.md', '.sql', '.json', '.yaml', '.yml', '.csv')):
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            content = f.read()
                        if content.strip():
                            import hashlib
                            rel_path = os.path.relpath(fpath, path)
                            # 使用文件完整路径的 md5 作为唯一 ID
                            doc_id = "local_" + hashlib.md5(fpath.encode('utf-8')).hexdigest()[:16]
                            docs.append({
                                "token": doc_id,
                                "title": f"[{description}] {rel_path}",
                                "path": f"{description}/{rel_path}",
                                "content": content,
                            })
                    except Exception as e:
                        logger.warning(f"读取本地文档失败: {fpath}, error={e}")

        if docs:
            logger.info(f"加载本地文档: {len(docs)} 篇")
        return docs

    def _get_docs_for_source(self, source_token: str, source_type: str = "wiki", ignore_ttl: bool = False) -> List[Dict[str, str]]:
        """获取某个 source 下的所有文档

        Args:
            source_token: 源 token
            source_type: "wiki" 或 "drive"
            ignore_ttl: 是否忽略 TTL 强制检查更新（定时同步时使用）
        """
        logger.info(f"获取文档源: source={source_token}, type={source_type}")

        # 检查缓存
        cached = self._load_from_cache(source_token)
        if cached is not None:
            # 缓存存在，检查是否需要更新
            logger.debug(f"发现缓存，检查更新: source={source_token}, type={source_type}")
            updated_docs = self._check_and_update(source_token, cached, source_type, ignore_ttl=ignore_ttl)
            if updated_docs is not None:
                self._indexed = False  # 文档有更新，需要重建索引
                return updated_docs
            # 缓存有效，直接返回
            return cached

        # 无缓存，从 MCP 全量获取
        logger.info(f"首次从 MCP 获取文档: source={source_token}, type={source_type}")
        try:
            docs = self._fetch_tree(source_token, source_type=source_type)
            if docs:
                self._save_to_cache(source_token, docs)
                self._indexed = False  # 新文档，需要重建索引
            return docs
        except Exception as e:
            logger.error(f"MCP 获取文档失败: source={source_token}, error={e}")
            return []

    def _mcp_list_children(self, token: str, source_type: str, recursive: bool = True) -> Any:
        """根据 source_type 调用对应的 MCP 接口列出子节点"""
        if source_type == "drive":
            return self.mcp_client.drive_list_folder(token, recursive=recursive)
        else:  # wiki
            return self.mcp_client.wiki_list_nodes(token, recursive=recursive)

    def _mcp_read_document(self, token: str, source_type: str) -> Any:
        """根据 source_type 调用对应的 MCP 接口读取文档"""
        logger.debug(f"MCP 读取文档: token={token}, source_type={source_type}")
        if source_type == "drive":
            logger.debug(f"调用 drive_read_document: file_id={token}")
            return self.mcp_client.drive_read_document(token)
        else:  # wiki
            logger.debug(f"调用 wiki_read_document: wiki_token={token}")
            return self.mcp_client.wiki_read_document(token)

    def _check_and_update(self, source_token: str, cached_docs: List[Dict[str, str]], source_type: str = "wiki", ignore_ttl: bool = False) -> Optional[List[Dict[str, str]]]:
        """
        检查缓存并增量更新

        Returns:
            None: 缓存有效，无需更新（调用方直接使用 cached_docs）
            List: 更新后的文档列表
        """
        logger.debug(f"进入 _check_and_update: source={source_token}, type={source_type}, cached_docs={len(cached_docs)}")
        cache_path = self._get_cache_path(source_token)

        # TTL 内直接返回（定时同步时跳过 TTL 检查）
        if not ignore_ttl and self._is_cache_valid(cache_path):
            logger.info(f"缓存 TTL 有效: source={source_token}, docs={len(cached_docs)}")
            return None

        # 获取最新元数据比对
        logger.info(f"检查文档更新: source={source_token}, type={source_type}")
        try:
            latest_meta = self._fetch_metadata_tree(source_token, source_type=source_type)
        except Exception as e:
            logger.warning(f"获取元数据失败，继续使用旧缓存: {e}")
            return None

        # 构建缓存 token -> doc 映射
        cached_map = {}
        for doc in cached_docs:
            token = doc.get("token")
            if token:
                cached_map[token] = doc

        # 找出需要更新和新增的文档
        tokens_to_update = []
        for token, latest_time in latest_meta.items():
            cached_doc = cached_map.get(token)
            if cached_doc is None:
                # 新文档
                tokens_to_update.append(token)
            elif latest_time and cached_doc.get("edit_time") != latest_time:
                # 已更新的文档
                tokens_to_update.append(token)

        # 找出远端已删除的文档（缓存中有但远端目录树中不存在）
        tokens_to_delete = []
        for token in cached_map:
            if token not in latest_meta:
                tokens_to_delete.append(token)

        if not tokens_to_update and not tokens_to_delete:
            logger.info(f"文档未更新，继续使用缓存: source={source_token}")
            # 刷新 TTL
            self._save_to_cache(source_token, cached_docs)
            return None

        # 增量更新：只拉取有变化的文档
        logger.info(f"增量同步: 新增/更新 {len(tokens_to_update)} 篇, 删除 {len(tokens_to_delete)} 篇, source={source_token}")
        updated_docs = list(cached_docs)  # 复制一份

        # 删除远端已移除的文档
        if tokens_to_delete:
            delete_set = set(tokens_to_delete)
            deleted_titles = [d.get("title", "") for d in updated_docs if d.get("token") in delete_set]
            updated_docs = [d for d in updated_docs if d.get("token") not in delete_set]
            logger.info(f"删除本地缓存中已失效的文档({len(deleted_titles)}篇): {deleted_titles}")

        # 如果没有需要新增/更新的文档，直接保存并返回
        if not tokens_to_update:
            self._save_to_cache(source_token, updated_docs)
            logger.info(f"增量同步完成(仅删除): source={source_token}, 删除 {len(tokens_to_delete)} 篇, 总计 {len(updated_docs)} 篇")
            return updated_docs

        # 获取完整目录树用于路径信息
        all_items = self._get_all_items(source_token, source_type)
        item_map = {}
        token_name_map = {source_token: ""}
        token_parent_map = {}
        for item in all_items:
            t = item.get("obj_token") or item.get("token", "")
            nt = item.get("node_token") or item.get("token", "")
            name = item.get("name") or item.get("title") or "未知"
            parent = item.get("parent_node_token") or item.get("parent_token", source_token)
            if t:
                item_map[t] = item
            if nt:
                token_name_map[nt] = name
                token_parent_map[nt] = parent

        def build_path(node_tk: str) -> str:
            parts = []
            current = node_tk
            visited = set()
            while current and current != source_token and current not in visited:
                visited.add(current)
                name = token_name_map.get(current, "")
                if name:
                    parts.append(name)
                current = token_parent_map.get(current)
            parts.reverse()
            return "/".join(parts)

        success_count = 0
        fail_count = 0
        for token in tokens_to_update:
            item = item_map.get(token, {})
            node_token = item.get("node_token") or item.get("token", "")
            title = item.get("name") or item.get("title") or "未知"
            # wiki 模式用 node_token 读取，drive 模式用 obj_token
            read_token = node_token if source_type == "wiki" else token
            content = self._read_document(read_token, source_type, title=title)
            if not content:
                fail_count += 1
                continue
            success_count += 1
            edit_time = latest_meta.get(token)
            current_path = build_path(node_token) if node_token else title
            url = self._build_doc_url(node_token, token, source_type=source_type)

            new_doc = {
                "title": title,
                "token": token,
                "node_token": node_token,
                "path": current_path,
                "content": content,
                "edit_time": edit_time,
                "url": url,
                "source_type": source_type,
            }

            # 替换或新增
            existing_idx = None
            for i, doc in enumerate(updated_docs):
                if doc.get("token") == token:
                    existing_idx = i
                    break

            if existing_idx is not None:
                updated_docs[existing_idx] = new_doc
                logger.debug(f"更新文档: {title}")
            else:
                updated_docs.append(new_doc)
                logger.debug(f"新增文档: {title}")

        # 保存更新后的缓存
        self._save_to_cache(source_token, updated_docs)
        logger.info(
            f"增量同步完成: source={source_token}, "
            f"新增/更新成功 {success_count} 篇, 失败 {fail_count} 篇, "
            f"删除 {len(tokens_to_delete)} 篇, 总计 {len(updated_docs)} 篇"
        )
        return updated_docs

    def _get_all_items(self, source_token: str, source_type: str = "wiki") -> List[Dict[str, Any]]:
        """获取完整目录树的节点列表（用于路径信息）"""
        try:
            children = self._mcp_list_children(source_token, source_type, recursive=True)
            return self._parse_children(children)
        except Exception:
            return []

    def _fetch_tree(self, token: str, depth: int = 0, path: str = "", source_type: str = "wiki") -> List[Dict[str, str]]:
        """
        获取 token 下的所有文档，保留层级路径

        使用 MCP 的 recursive=True 参数一次性获取完整目录树，
        然后根据 parent_token 关系构建完整路径。

        Args:
            token: source token
            source_type: "wiki"（知识库）或 "drive"（云空间）
        """
        docs = []
        try:
            # 一次性获取完整目录树（根据类型调用不同接口）
            children = self._mcp_list_children(token, source_type, recursive=True)
            items = self._parse_children(children)

            logger.info(f"MCP 递归获取到 {len(items)} 个节点 (type={source_type})")

            # 构建 token -> item 映射和 token -> name 映射，用于路径构建
            token_name_map = {token: ""}  # 根节点
            token_parent_map = {}
            for item in items:
                node_token = item.get("node_token") or item.get("token", "")
                name = item.get("name") or item.get("title") or "未知"
                parent = item.get("parent_node_token") or item.get("parent_token", token)
                if node_token:
                    token_name_map[node_token] = name
                    token_parent_map[node_token] = parent

            def build_path(node_token: str) -> str:
                """根据 parent_token 链构建完整路径"""
                parts = []
                current = node_token
                visited = set()
                while current and current != token and current not in visited:
                    visited.add(current)
                    name = token_name_map.get(current, "")
                    if name:
                        parts.append(name)
                    current = token_parent_map.get(current)
                parts.reverse()
                return "/".join(parts)

            success_count = 0
            fail_count = 0
            skip_count = 0
            for item in items:
                child_token = item.get("node_token") or item.get("token", "")
                title = item.get("name") or item.get("title") or "未知"
                node_type = item.get("obj_type") or item.get("type", "")
                obj_token = item.get("obj_token") or child_token
                edit_time = item.get("modified_time") or item.get("edit_time") or item.get("update_time")

                # 构建完整路径
                current_path = build_path(child_token)

                # 跳过不可读的类型
                skip_types = ("folder", "file")
                if source_type == "drive":
                    skip_types = ("folder", "file", "shortcut")

                if node_type in skip_types:
                    skip_count += 1
                    continue

                # 读取文档内容
                if obj_token:
                    # 根据 source_type 选择读取接口
                    # wiki 模式用 child_token (wiki_token)，drive 模式用 obj_token
                    read_token = child_token if source_type == "wiki" else obj_token
                    content = self._read_document(read_token, source_type, title=title)
                    if content:
                        # 构建文档 URL
                        url = self._build_doc_url(child_token, obj_token, node_type, source_type)
                        docs.append({
                            "title": title,
                            "token": obj_token,
                            "node_token": child_token,
                            "path": current_path,
                            "content": content,
                            "edit_time": edit_time,
                            "url": url,
                            "source_type": source_type,
                        })
                        success_count += 1
                    else:
                        fail_count += 1

            logger.info(f"全量同步完成: token={token}, 成功 {success_count} 篇, 失败 {fail_count} 篇, 跳过 {skip_count} 篇")

        except Exception as e:
            logger.error(f"获取文档树失败: token={token}, error={e}")

        return docs

    def _parse_children(self, result: Any) -> List[Dict[str, Any]]:
        logger.info(f"MCP 递归获取到 {result}")
        """解析 list_children 返回结果"""
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            # 可能是 {"items": [...]} 或 {"nodes": [...]} 或 {"children": [...]}
            for key in ("items", "nodes", "children", "files"):
                if key in result and isinstance(result[key], list):
                    return [item for item in result[key] if isinstance(item, dict)]
        return []

    def _read_document(self, token: str, source_type: str = "wiki", title: str = "") -> Optional[str]:
        """读取单个文档内容（根据 source_type 调用对应接口）"""
        try:
            result = self._mcp_read_document(token, source_type)
            if isinstance(result, str):
                # 检测 MCP 返回的错误信息
                if result.startswith("Error executing tool") or result.startswith("Error:"):
                    logger.warning(f"文档读取返回错误，跳过: title='{title}', token={token}, error={result[:200]}")
                    return None
                # 检测不支持的文档类型（MCP 返回的 JSON 格式错误）
                if '"error"' in result and '不支持的文档类型' in result:
                    logger.info(f"文档类型不支持（旧版飞书文档），跳过: title='{title}', token={token}")
                    return None
                return result
            if isinstance(result, dict):
                return (
                    result.get("content")
                    or result.get("text")
                    or json.dumps(result, ensure_ascii=False)
                )
            return str(result) if result else None
        except Exception as e:
            logger.warning(f"读取文档失败: title='{title}', token={token}, type={source_type}, error={e}")
            return None

    def _build_doc_url(self, node_token: str, obj_token: str, node_type: str = "", source_type: str = "") -> str:
        """
        根据 token 构建飞书文档 URL

        飞书文档 URL 格式：
        - 知识库文档：{base_url}/wiki/{node_token}
        - 云空间文档：{base_url}/docx/{obj_token}
        """
        if not self._doc_base_url:
            return ""

        # 优先根据 source_type 判断
        if source_type == "wiki" and node_token:
            return f"{self._doc_base_url}/wiki/{node_token}"
        if source_type == "drive" and obj_token:
            return f"{self._doc_base_url}/docx/{obj_token}"

        # 兼容：未指定 source_type 时按 token 是否相同判断
        if node_token and node_token != obj_token:
            return f"{self._doc_base_url}/wiki/{node_token}"
        if obj_token:
            return f"{self._doc_base_url}/docx/{obj_token}"

        return ""

    def _extract_keywords(self, query_text: str) -> KeywordExtractionResult:
        """
        从查询文本中提取关键词，并判断是否为通用技术问题。

        优先通过已注入的 AI provider 调用，降级到规则提取（此时 is_generic_tech=False）。
        """
        # 优先通过 AI provider 提取（统一接口，所有 Provider 均支持）
        if self._ai_provider:
            try:
                result = self._ai_provider.extract_keywords(query_text)
                # provider 返回了有效关键词或进行了分类，直接使用
                if result.keywords or result.is_generic_tech:
                    return result
                # keywords 为空时降级，但保留 is_generic_tech=False
            except Exception as e:
                logger.warning(f"AI 关键词提取失败，降级到规则提取: {e}")

        # 兼容旧的 keyword_extractor callable（deprecated）
        if self._keyword_extractor:
            try:
                keywords = self._keyword_extractor(query_text)
                if keywords:
                    logger.info(f"callable 关键词提取: '{query_text[:50]}' → {keywords}")
                    return KeywordExtractionResult(keywords=keywords, is_generic_tech=False)
            except Exception as e:
                logger.warning(f"callable 关键词提取失败，降级到规则提取: {e}")

        # 最终降级：规则提取
        keywords = self._extract_keywords_by_rules(query_text)
        logger.info(f"规则关键词提取: '{query_text[:50]}' → {keywords}")
        return KeywordExtractionResult(keywords=keywords, is_generic_tech=False)

    def _extract_keywords_by_rules(self, query_text: str) -> List[str]:
        """基于规则的关键词提取（降级方案）"""
        # 提取引号中的内容作为精确关键词
        quoted = re.findall(r'[「」""\'"](.+?)[「」""\'"]', query_text)
        if quoted:
            return quoted[:3]

        # 去除常见停用词
        stop_words = {
            "帮我", "找一下", "相关的", "文档", "资料", "内容", "什么是", "如何",
            "怎么", "请问", "告诉我", "关于", "有没有", "查一下", "看看",
        }
        text = query_text
        for sw in stop_words:
            text = text.replace(sw, " ")

        # 提取连续中文或英文片段
        segments = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_.]{2,}', text)
        keywords = [s for s in segments if len(s) >= 2]
        return keywords[:4]

    def _filter_relevant_docs(
        self, docs: List[Dict[str, str]], keywords: List[str]
    ) -> List[Dict[str, str]]:
        """根据关键词过滤相关文档"""
        if not keywords:
            return docs

        # 第一轮：标题和路径匹配（精准）
        title_matched = []
        for doc in docs:
            title = doc.get("title", "")
            path = doc.get("path", "")
            text = f"{title} {path}".lower()
            if any(kw.lower() in text for kw in keywords):
                title_matched.append(doc)

        if title_matched:
            logger.debug(f"标题匹配到 {len(title_matched)} 篇文档")
            return title_matched

        # 第二轮：内容匹配（要求所有关键词都命中）
        content_matched = []
        for doc in docs:
            content = doc.get("content", "").lower()
            if all(kw.lower() in content for kw in keywords):
                content_matched.append(doc)

        if content_matched:
            logger.debug(f"内容匹配到 {len(content_matched)} 篇文档")
            return content_matched

        # 第三轮：内容宽松匹配（任一关键词命中）
        loose_matched = []
        for doc in docs:
            content = doc.get("content", "").lower()
            if any(kw.lower() in content for kw in keywords):
                loose_matched.append(doc)

        if loose_matched:
            logger.debug(f"宽松匹配到 {len(loose_matched)} 篇文档")
            return loose_matched

        logger.debug("关键词过滤无结果")
        return []

    # ---- 缓存管理 ----

    def _get_cache_path(self, source_token: str) -> Path:
        """获取 source token 对应的缓存目录"""
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', source_token)
        return self.cache_dir / safe_name

    def _is_cache_valid(self, cache_path: Path) -> bool:
        """检查缓存是否有效（基于 TTL）"""
        metadata_file = cache_path / "metadata.json"
        if not metadata_file.exists():
            return False
        try:
            with open(metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            cached_at = metadata.get("cached_at", 0)
            return (time.time() - cached_at) < self.cache_ttl
        except (json.JSONDecodeError, OSError):
            return False

    def _fetch_metadata_tree(self, token: str, source_type: str = "wiki") -> Dict[str, Optional[str]]:
        """获取文档元数据（只获取 token 和 edit_time，不读取内容）"""
        metadata = {}
        try:
            children = self._mcp_list_children(token, source_type, recursive=True)
            items = self._parse_children(children)

            for item in items:
                child_token = item.get("obj_token") or item.get("token", "")
                node_type = item.get("type", "")
                edit_time = item.get("modified_time") or item.get("edit_time") or item.get("update_time")

                if child_token and node_type not in ("folder",):
                    metadata[child_token] = edit_time

        except Exception as e:
            logger.error(f"获取元数据失败: token={token}, error={e}")

        return metadata

    def _load_from_cache(self, source_token: str) -> Optional[List[Dict[str, str]]]:
        """从缓存加载文档"""
        cache_path = self._get_cache_path(source_token)
        with self._lock:
            metadata_file = cache_path / "metadata.json"
            if not metadata_file.exists():
                return None

            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)

                docs = []
                for doc_info in metadata.get("documents", []):
                    content_file = cache_path / doc_info["filename"]
                    if content_file.exists():
                        content = content_file.read_text(encoding="utf-8")
                        docs.append({
                            "title": doc_info["title"],
                            "token": doc_info.get("token", ""),
                            "path": doc_info.get("path", ""),
                            "content": content,
                            "edit_time": doc_info.get("edit_time"),
                            "url": doc_info.get("url", ""),
                        })
                return docs
            except Exception as e:
                logger.warning(f"读取缓存失败: source={source_token}, error={e}")
                return None

    def _save_to_cache(self, source_token: str, docs: List[Dict[str, str]]) -> None:
        """保存文档到缓存，保持飞书原始目录结构"""
        cache_path = self._get_cache_path(source_token)
        with self._lock:
            try:
                cache_path.mkdir(parents=True, exist_ok=True)

                doc_infos = []
                for doc in docs:
                    # 使用文档的层级路径创建目录结构
                    doc_path = doc.get("path", doc["title"])
                    safe_path = re.sub(r'[<>:"|?*]', '_', doc_path)

                    # 分离目录和文件名
                    parts = safe_path.split("/")
                    if len(parts) > 1:
                        dir_path = cache_path / "/".join(parts[:-1])
                        dir_path.mkdir(parents=True, exist_ok=True)

                    filename = f"{safe_path}.txt"

                    # 写入文档内容
                    content_file = cache_path / filename
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
                    "cached_at": time.time(),
                    "total_docs": len(docs),
                    "documents": doc_infos,
                }
                metadata_file = cache_path / "metadata.json"
                with open(metadata_file, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)

                logger.info(f"缓存已保存: source={source_token}, docs={len(docs)}")
            except Exception as e:
                logger.error(f"保存缓存失败: source={source_token}, error={e}")

    def _filter_docs_by_ai(self, query: str, docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """用 AI 过滤文档：判断标题（含父目录）与 query 的相关性"""
        if not self._ai_provider or not docs:
            logger.debug(f"无 AI provider 或无文档，返回前 {self.MAX_DOCS_IN_PROMPT} 篇")
            return docs[:self.MAX_DOCS_IN_PROMPT]

        try:
            candidates_lines = []
            for i, doc in enumerate(docs, 1):
                path = doc.get("path", doc.get("title", ""))
                candidates_lines.append(f"{i}. {path}")
            candidates_text = "\n".join(candidates_lines)

            logger.info(f"AI 标题过滤输入:\n查询: {query}\n候选文档({len(docs)}篇):\n{candidates_text}")

            selected_indices = self._ai_provider.filter_docs_by_relevance(
                query, docs, max_docs=self.MAX_DOCS_IN_PROMPT
            )

            if selected_indices:
                filtered = [docs[i] for i in selected_indices]
                logger.info(f"AI 过滤后保留 {len(filtered)} 篇: {[d['title'] for d in filtered]}")
                return filtered
            else:
                logger.warning("AI 未返回有效文档下标，使用原始排序")
                return docs[:self.MAX_DOCS_IN_PROMPT]

        except Exception as e:
            logger.warning(f"AI 过滤失败: {e}，使用原始排序")
            return docs[:self.MAX_DOCS_IN_PROMPT]

    def set_ai_provider(self, ai_provider):
        """设置 AI provider（用于标题过滤）"""
        self._ai_provider = ai_provider
