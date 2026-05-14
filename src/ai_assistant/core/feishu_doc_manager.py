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
        self.sources = sources or []
        self._keyword_extractor = keyword_extractor
        self._local_docs_config = local_docs or []
        self._lock = threading.Lock()
        self._indexed = False
        self._doc_base_url = doc_base_url.rstrip('/') if doc_base_url else ""

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

        # 确保文档已加载并建立索引
        self._ensure_indexed()

        # 使用混合检索
        results = self._search_engine.search(query_text, top_k=self.MAX_DOCS_IN_PROMPT)

        if not results:
            logger.info(f"检索无结果: '{query_text[:50]}'")
            return ""

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
        """确保文档已加载到缓存并建立检索索引（含在线文档 + 本地文档）"""
        if self._indexed:
            return

        all_docs = self.sync_docs()

        if all_docs:
            self._search_engine.index_documents(all_docs)
            self._indexed = True
            online_count = sum(1 for d in all_docs if not d.get("token", "").startswith("local_"))
            local_count = len(all_docs) - online_count
            logger.info(f"检索索引已建立: {len(all_docs)} 篇文档（在线 {online_count}, 本地 {local_count}）")

    def sync_docs(self, force: bool = False) -> List[Dict[str, str]]:
        """
        同步所有文档到本地缓存（不建立向量索引）

        Args:
            force: 是否强制重新获取（忽略缓存 TTL）

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
        for source_token in self.sources:
            docs = self._get_docs_for_source(source_token)
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

        for source_token in self.sources:
            logger.info(f"列出目录: source={source_token}")
            try:
                children = self.mcp_client.list_children(source_token, type_hint="auto", recursive=True)
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
                        url = self._build_doc_url(node_token, obj_token, node_type)

                    all_items.append({
                        "title": title,
                        "type": node_type,
                        "token": node_token,
                        "obj_token": obj_token,
                        "url": url,
                        "source": source_token,
                    })
            except Exception as e:
                logger.error(f"  list_children 失败: {e}")

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

    def _get_docs_for_source(self, source_token: str) -> List[Dict[str, str]]:
        """获取某个 source token 下的所有文档"""
        # 检查缓存
        cached = self._load_from_cache(source_token)
        if cached is not None:
            # 缓存存在，检查是否需要更新
            updated_docs = self._check_and_update(source_token, cached)
            if updated_docs is not None:
                self._indexed = False  # 文档有更新，需要重建索引
                return updated_docs
            # 缓存有效，直接返回
            return cached

        # 无缓存，从 MCP 全量获取
        logger.info(f"首次从 MCP 获取文档: source={source_token}")
        try:
            docs = self._fetch_tree(source_token)
            if docs:
                self._save_to_cache(source_token, docs)
                self._indexed = False  # 新文档，需要重建索引
            return docs
        except Exception as e:
            logger.error(f"MCP 获取文档失败: source={source_token}, error={e}")
            return []

    def _check_and_update(self, source_token: str, cached_docs: List[Dict[str, str]]) -> Optional[List[Dict[str, str]]]:
        """
        检查缓存并增量更新

        Returns:
            None: 缓存有效，无需更新（调用方直接使用 cached_docs）
            List: 更新后的文档列表
        """
        cache_path = self._get_cache_path(source_token)

        # TTL 内直接返回
        if self._is_cache_valid(cache_path):
            logger.info(f"缓存 TTL 有效: source={source_token}, docs={len(cached_docs)}")
            return None

        # TTL 过期，获取最新元数据比对
        logger.info(f"缓存 TTL 过期，检查文档更新: source={source_token}")
        try:
            latest_meta = self._fetch_metadata_tree(source_token, depth=0)
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

        if not tokens_to_update:
            logger.info(f"文档未更新，继续使用缓存: source={source_token}")
            # 刷新 TTL
            self._save_to_cache(source_token, cached_docs)
            return None

        # 增量更新：只拉取有变化的文档
        logger.info(f"增量更新 {len(tokens_to_update)} 篇文档: source={source_token}")
        updated_docs = list(cached_docs)  # 复制一份

        # 获取完整目录树用于路径信息
        all_items = self._get_all_items(source_token)
        item_map = {}
        for item in all_items:
            t = item.get("obj_token") or item.get("token", "")
            if t:
                item_map[t] = item

        for token in tokens_to_update:
            content = self._read_document(token)
            if not content:
                continue

            item = item_map.get(token, {})
            title = item.get("name") or item.get("title") or "未知"
            edit_time = latest_meta.get(token)
            parent_path = item.get("parent_path", "")
            current_path = f"{parent_path}/{title}" if parent_path else title
            node_token = item.get("node_token") or item.get("token", "")
            url = self._build_doc_url(node_token, token)

            new_doc = {
                "title": title,
                "token": token,
                "path": current_path,
                "content": content,
                "edit_time": edit_time,
                "url": url,
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
        logger.info(f"增量更新完成: source={source_token}, 更新 {len(tokens_to_update)} 篇, 总计 {len(updated_docs)} 篇")
        return updated_docs

    def _get_all_items(self, source_token: str) -> List[Dict[str, Any]]:
        """获取完整目录树的节点列表（用于路径信息）"""
        try:
            children = self.mcp_client.list_children(source_token, recursive=True)
            return self._parse_children(children)
        except Exception:
            return []

    def _fetch_tree(self, token: str, depth: int = 0, path: str = "") -> List[Dict[str, str]]:
        """
        获取 token 下的所有文档，保留层级路径

        使用 MCP 的 recursive=True 参数一次性获取完整目录树，
        然后根据 parent_token 关系构建完整路径。
        """
        docs = []
        try:
            # 一次性获取完整目录树
            children = self.mcp_client.list_children(token, type_hint="auto", recursive=True)
            items = self._parse_children(children)

            logger.info(f"MCP 递归获取到 {len(items)} 个节点")

            # 构建 token -> item 映射和 token -> name 映射，用于路径构建
            token_name_map = {token: ""}  # 根节点
            token_parent_map = {}
            for item in items:
                node_token = item.get("token", "")
                name = item.get("name") or item.get("title") or "未知"
                parent = item.get("parent_token", token)
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

            for item in items:
                child_token = item.get("token", "")
                title = item.get("name") or item.get("title") or "未知"
                node_type = item.get("type", "")
                obj_token = item.get("obj_token") or child_token
                edit_time = item.get("modified_time") or item.get("edit_time") or item.get("update_time")

                # 构建完整路径
                current_path = build_path(child_token)

                # 读取文档内容（folder 类型跳过）
                if obj_token and node_type not in ("folder",):
                    content = self._read_document(obj_token)
                    if content:
                        # 构建文档 URL
                        url = self._build_doc_url(child_token, obj_token, node_type)
                        docs.append({
                            "title": title,
                            "token": obj_token,
                            "path": current_path,
                            "content": content,
                            "edit_time": edit_time,
                            "url": url,
                        })

        except Exception as e:
            logger.error(f"获取文档树失败: token={token}, error={e}")

        return docs

    def _parse_children(self, result: Any) -> List[Dict[str, Any]]:
        """解析 list_children 返回结果"""
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            # 可能是 {"items": [...]} 或 {"nodes": [...]} 或 {"children": [...]}
            for key in ("items", "nodes", "children", "files"):
                if key in result and isinstance(result[key], list):
                    return [item for item in result[key] if isinstance(item, dict)]
        return []

    def _read_document(self, token: str) -> Optional[str]:
        """读取单个文档内容"""
        try:
            result = self.mcp_client.read_document(token)
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                return (
                    result.get("content")
                    or result.get("text")
                    or json.dumps(result, ensure_ascii=False)
                )
            return str(result) if result else None
        except Exception as e:
            logger.warning(f"读取文档失败: token={token}, error={e}")
            return None

    def _build_doc_url(self, node_token: str, obj_token: str, node_type: str = "") -> str:
        """
        根据 token 构建飞书文档 URL

        飞书文档 URL 格式：
        - 知识库文档：{base_url}/wiki/{node_token}
        - 云空间文档：{base_url}/docx/{obj_token}
        """
        if not self._doc_base_url:
            return ""

        # 知识库文档优先使用 node_token（wiki 路径）
        if node_token and node_token != obj_token:
            return f"{self._doc_base_url}/wiki/{node_token}"

        # 云空间文档使用 obj_token
        if obj_token:
            return f"{self._doc_base_url}/docx/{obj_token}"

        return ""

    def _extract_keywords(self, query_text: str) -> List[str]:
        """从查询文本中提取关键词，优先使用 AI 辅助提取"""
        # 优先使用 AI 提取关键词
        if self._keyword_extractor:
            try:
                keywords = self._keyword_extractor(query_text)
                if keywords:
                    logger.info(f"AI 关键词提取: '{query_text}' → {keywords}")
                    return keywords
            except Exception as e:
                logger.warning(f"AI 关键词提取失败，降级到规则提取: {e}")

        # 降级：规则提取
        keywords = self._extract_keywords_by_rules(query_text)
        logger.info(f"规则关键词提取: '{query_text}' → {keywords}")
        return keywords

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
        return keywords[:3]

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

    def _fetch_metadata_tree(self, token: str, depth: int) -> Dict[str, Optional[str]]:
        """获取文档元数据（只获取 token 和 edit_time，不读取内容）"""
        metadata = {}
        try:
            children = self.mcp_client.list_children(token, recursive=True)
            items = self._parse_children(children)

            for item in items:
                child_token = item.get("obj_token") or item.get("token", "")
                has_child = item.get("has_child", False)
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
