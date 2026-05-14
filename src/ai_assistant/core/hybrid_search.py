"""n混合检索引擎

结合向量检索（语义匹配）和 BM25（关键词精确匹配），
使用 RRF（Reciprocal Rank Fusion）算法合并排序结果。
"""

import jieba
from typing import Any, Dict, List, Optional
from pathlib import Path
from loguru import logger


class Text2VecEmbeddingFunction:
    """基于 ONNX Runtime 的中文 Embedding 函数，支持 GPU 加速"""

    def __init__(self, model_dir: str = None, use_gpu: bool = False, gpu_id: int = 0, batch_size: int = 32):
        """
        Args:
            model_dir: 模型目录路径
            use_gpu: 是否使用 GPU 加速
            gpu_id: 使用哪张 GPU（0 或 1）
            batch_size: 批处理大小（GPU: 128-256, CPU: 16-32）
        """
        import os
        import numpy as np
        from transformers import AutoTokenizer
        import onnxruntime as ort

        self._np = np
        self._batch_size = batch_size

        if model_dir is None:
            model_dir = "./models/text2vec-base-chinese"

        if not os.path.isdir(model_dir):
            raise FileNotFoundError(f"模型目录不存在: {model_dir}")

        # 加载 tokenizer（从本地目录）
        self._tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)

        # 加载 ONNX 模型
        onnx_path = os.path.join(model_dir, "model.onnx")
        if not os.path.exists(onnx_path):
            raise FileNotFoundError(f"ONNX 模型文件不存在: {onnx_path}")

        # 配置 ONNX Runtime 会话选项
        sess_options = ort.SessionOptions()

        # 选择执行提供者（GPU 或 CPU）
        providers = []
        if use_gpu:
            try:
                # 检查 CUDA 是否可用
                available_providers = ort.get_available_providers()
                if 'CUDAExecutionProvider' in available_providers:
                    # 配置 CUDA 提供者
                    cuda_provider_options = {
                        'device_id': gpu_id,  # 指定使用哪张 GPU
                        'arena_extend_strategy': 'kNextPowerOfTwo',
                        'gpu_mem_limit': 2 * 1024 * 1024 * 1024,  # 限制 GPU 内存使用 2GB
                        'cudnn_conv_algo_search': 'EXHAUSTIVE',
                        'do_copy_in_default_stream': True,
                    }
                    providers = [
                        ('CUDAExecutionProvider', cuda_provider_options),
                        'CPUExecutionProvider'  # fallback
                    ]
                    logger.info(f"使用 GPU {gpu_id} 加速 Embedding 生成")
                else:
                    logger.warning("CUDA 不可用，fallback 到 CPU")
                    providers = ['CPUExecutionProvider']
                    # CPU 模式下限制线程数
                    sess_options.intra_op_num_threads = 2
                    sess_options.inter_op_num_threads = 2
                    sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            except Exception as e:
                logger.warning(f"GPU 初始化失败: {e}，fallback 到 CPU")
                providers = ['CPUExecutionProvider']
                sess_options.intra_op_num_threads = 2
                sess_options.inter_op_num_threads = 2
                sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        else:
            # CPU 模式，限制线程数避免 CPU 爆炸
            providers = ['CPUExecutionProvider']
            sess_options.intra_op_num_threads = 2
            sess_options.inter_op_num_threads = 2
            sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        self._session = ort.InferenceSession(
            onnx_path,
            sess_options=sess_options,
            providers=providers
        )
        self._model_inputs = {inp.name for inp in self._session.get_inputs()}

        # 输出实际使用的提供者
        actual_providers = self._session.get_providers()
        logger.info(f"ONNX Embedding 模型已加载: {model_dir}, providers={actual_providers}")

    def name(self) -> str:
        """ChromaDB 要求的方法，返回 Embedding 函数名称"""
        return "text2vec-base-chinese"

    def __call__(self, input: List[str]) -> List[List[float]]:
        return self._encode(input)

    def embed_query(self, input: str) -> List[float]:
        """ChromaDB 查询时调用（参数名必须是 input）"""
        # ChromaDB 可能传入字符串或列表
        if isinstance(input, list):
            # 如果是列表，返回 List[List[float]]
            return self._encode(input)

        # 如果是字符串，确保是真正的字符串
        if not isinstance(input, str):
            input = str(input)

        result = self._encode([input])
        # result 是 List[List[float]]，取第一个元素得到 List[float]
        if isinstance(result, list) and len(result) > 0:
            return result[0]
        else:
            logger.error(f"embed_query 返回类型错误: {type(result)}, {result}")
            return [0.0] * 768  # 返回零向量作为降级

    def embed_documents(self, input: List[str]) -> List[List[float]]:
        """ChromaDB 索引时调用（参数名必须是 input）"""
        # 确保所有元素都是字符串
        if not isinstance(input, list):
            input = [input]
        input = [str(item) if not isinstance(item, str) else item for item in input]
        return self._encode(input)

    def _encode(self, texts: List[str]) -> List[List[float]]:
        # 确保输入是字符串列表
        if not texts or not isinstance(texts, list):
            logger.warning(f"_encode 收到非法输入: {type(texts)}, {texts}")
            texts = [str(texts)] if texts else [""]

        texts = [str(t) if not isinstance(t, str) else t for t in texts]

        # 分批处理，避免单次请求过大导致 GPU OOM
        total = len(texts)
        if total > self._batch_size:
            logger.info(f"分批处理 Embedding: total={total}, batch_size={self._batch_size}")
            all_embeddings = []
            for i in range(0, total, self._batch_size):
                batch = texts[i:i + self._batch_size]
                batch_embeddings = self._encode_batch(batch)
                all_embeddings.extend(batch_embeddings)
            return all_embeddings
        else:
            return self._encode_batch(texts)

    def _encode_batch(self, texts: List[str]) -> List[List[float]]:
        """单批次 Embedding 计算"""
        try:
            # 不使用 return_tensors，直接返回 Python 对象
            encoded = self._tokenizer(
                texts, padding=True, truncation=True, max_length=512
            )

            # 手动转换为 numpy 数组
            import numpy as np
            input_ids = np.array(encoded["input_ids"], dtype=np.int64)
            attention_mask = np.array(encoded["attention_mask"], dtype=np.int64)
            token_type_ids = np.array(encoded.get("token_type_ids", [[0] * len(input_ids[0])] * len(input_ids)), dtype=np.int64)

        except Exception as e:
            logger.error(f"Tokenizer 调用失败: {e}, 输入类型={type(texts)}, 输入前2项={texts[:2] if len(texts) > 0 else texts}")
            raise

        ort_inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }
        ort_inputs = {k: v for k, v in ort_inputs.items() if k in self._model_inputs}

        outputs = self._session.run(None, ort_inputs)

        # Mean pooling
        token_embeddings = outputs[0]
        attention_mask = encoded["attention_mask"]
        mask_expanded = self._np.expand_dims(attention_mask, axis=-1)
        sum_embeddings = (token_embeddings * mask_expanded).sum(axis=1)
        sum_mask = mask_expanded.sum(axis=1).clip(min=1e-9)
        embeddings = sum_embeddings / sum_mask

        # L2 归一化
        norms = self._np.linalg.norm(embeddings, axis=1, keepdims=True).clip(min=1e-9)
        embeddings = embeddings / norms

        return embeddings.tolist()


class HybridSearchEngine:
    """混合检索引擎：向量检索 + BM25"""

    def __init__(self, persist_dir: str = "./data/vector_db", use_gpu: bool = False, gpu_id: int = 0, batch_size: int = 32):
        """
        Args:
            persist_dir: ChromaDB 持久化目录
            use_gpu: 是否使用 GPU 加速 Embedding 生成
            gpu_id: 使用哪张 GPU（0 或 1）
            batch_size: Embedding 批处理大小
                       显存占用估算（text2vec-base-chinese）：
                       - batch_size=32:  约 1-1.5GB
                       - batch_size=64:  约 2-3GB
                       - batch_size=128: 约 4-6GB
                       - batch_size=256: 约 8-12GB
                       建议：GPU 显存 ≥ 16GB 时用 128-256，否则用 32-64
        """
        import chromadb
        import os

        # 限制 ChromaDB 和 ONNX 的线程数，避免 CPU 爆炸（仅 CPU 模式需要）
        if not use_gpu:
            os.environ['OMP_NUM_THREADS'] = '4'
            os.environ['MKL_NUM_THREADS'] = '4'
            os.environ['OPENBLAS_NUM_THREADS'] = '4'
            os.environ['VECLIB_MAXIMUM_THREADS'] = '4'
            os.environ['NUMEXPR_NUM_THREADS'] = '4'

        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        # 配置 ChromaDB 使用更少的线程
        settings = chromadb.Settings(
            anonymized_telemetry=False,
            allow_reset=True,
        )
        self._chroma_client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=settings
        )

        # 使用 text2vec 中文 Embedding（支持 GPU 加速）
        self._embedding_fn = Text2VecEmbeddingFunction(use_gpu=use_gpu, gpu_id=gpu_id, batch_size=batch_size)

        self._collection = self._chroma_client.get_or_create_collection(
            name="feishu_docs",
            metadata={"hnsw:space": "cosine"},
            embedding_function=self._embedding_fn,
        )

        # BM25 索引（内存中）
        self._bm25 = None
        self._bm25_docs: List[Dict[str, str]] = []
        self._bm25_corpus: List[List[str]] = []

        logger.info(f"混合检索引擎初始化: persist_dir={persist_dir}, docs={self._collection.count()}")

    def index_documents(self, docs: List[Dict[str, str]]) -> None:
        """
        索引文档（向量 + BM25）

        大文档会被分块索引到向量数据库（提升 Embedding 质量），
        BM25 保持全文索引（精确匹配不受影响）。

        Args:
            docs: [{"token": ..., "title": ..., "path": ..., "content": ...}]
        """
        if not docs:
            return

        from rank_bm25 import BM25Okapi

        CHUNK_SIZE = 1000  # 向量索引的分块大小（字符）
        CHUNK_OVERLAP = 100  # 分块重叠

        # 向量索引：分块 upsert 到 ChromaDB
        ids = []
        documents = []
        metadatas = []

        for doc in docs:
            token = doc.get("token", "")
            if not token:
                continue
            title = doc.get("title", "")
            path = doc.get("path", "")
            content = doc.get("content", "")

            if len(content) <= CHUNK_SIZE * 2:
                # 短文档：整篇索引
                ids.append(token)
                documents.append(f"{title}\n{title}\n{path}\n{content}")
                metadatas.append({"title": title, "path": path, "token": token})
            else:
                # 长文档：分块索引，每块带标题前缀
                chunks = self._split_text(content, CHUNK_SIZE, CHUNK_OVERLAP)
                for i, chunk in enumerate(chunks):
                    chunk_id = f"{token}_chunk{i}"
                    ids.append(chunk_id)
                    documents.append(f"{title}\n{path}\n{chunk}")
                    metadatas.append({"title": title, "path": path, "token": token, "chunk": i})

        if ids:
            batch_size = 5000
            for i in range(0, len(ids), batch_size):
                self._collection.upsert(
                    ids=ids[i:i+batch_size],
                    documents=documents[i:i+batch_size],
                    metadatas=metadatas[i:i+batch_size],
                )

        # BM25 索引：全文索引（不分块，精确匹配需要完整内容）
        self._bm25_docs = docs
        self._bm25_corpus = []
        for doc in docs:
            text = f"{doc.get('title', '')} {doc.get('path', '')} {doc.get('content', '')}"
            tokens = list(jieba.cut(text))
            self._bm25_corpus.append(tokens)

        self._bm25 = BM25Okapi(self._bm25_corpus)
        logger.info(f"文档索引完成: 向量={self._collection.count()}, BM25={len(self._bm25_corpus)}, 原始文档={len(docs)}")

    @staticmethod
    def _split_text(text: str, chunk_size: int, overlap: int) -> List[str]:
        """将长文本分块"""
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            start = end - overlap
        return chunks

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        混合检索：向量 + BM25，RRF 融合排序

        Args:
            query: 用户查询文本
            top_k: 返回结果数量

        Returns:
            排序后的文档列表 [{"token", "title", "path", "content", "score"}]
        """
        if not self._bm25_docs:
            return []

        # 1. 向量检索
        vector_results = self._vector_search(query, top_k=top_k * 2)

        # 2. BM25 检索
        bm25_results = self._bm25_search(query, top_k=top_k * 2)

        # 3. RRF 融合（BM25 权重更高，因为中文场景下关键词匹配更可靠）
        fused = self._rrf_fusion(vector_results, bm25_results, k=60, bm25_weight=1.5)
        fused_titles = [d['title'] for d in fused]

        # 4. 标题相关性过滤：用 Embedding 计算 query 与标题的相似度，过滤明显不相关的
        fused = self._filter_by_title_relevance(query, fused, threshold=0.35)

        # 取 top_k
        results = fused[:top_k]

        # 详细日志
        vector_titles = [f"{d['title']}({d['score']:.3f})" for d in vector_results[:5]]
        bm25_titles = [f"{d['title']}({d['score']:.1f})" for d in bm25_results[:5]]
        result_titles = [d['title'] for d in results]
        logger.info(
            f"混合检索: query='{query[:50]}'\n"
            f"  向量Top5: {vector_titles}\n"
            f"  BM25Top5: {bm25_titles}\n"
            f"  融合({len(fused_titles)}篇): {fused_titles}\n"
            f"  过滤后({len(results)}篇): {result_titles}"
        )

        return results

    def _vector_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """向量检索（分块结果自动去重回溯到原始文档）"""
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k * 3, self._collection.count()),  # 多取一些，去重后保证数量
            )

            # 去重：同一个原始文档的多个 chunk 只保留最高分
            seen_tokens = {}
            if results and results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i] if results.get("distances") else 0
                    metadata = results["metadatas"][0][i] if results.get("metadatas") else {}
                    # 获取原始文档 token（chunk 的 metadata 中存了原始 token）
                    original_token = metadata.get("token", doc_id.split("_chunk")[0])
                    score = 1 - distance

                    if original_token not in seen_tokens or score > seen_tokens[original_token]["score"]:
                        seen_tokens[original_token] = {
                            "token": original_token,
                            "title": metadata.get("title", ""),
                            "path": metadata.get("path", ""),
                            "score": score,
                        }

            # 按分数排序
            docs = sorted(seen_tokens.values(), key=lambda x: x["score"], reverse=True)
            return docs[:top_k]
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            return []

    def _bm25_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """BM25 关键词检索"""
        if not self._bm25:
            return []

        try:
            query_tokens = list(jieba.cut(query))
            scores = self._bm25.get_scores(query_tokens)

            # 获取 top_k 结果
            indexed_scores = [(i, s) for i, s in enumerate(scores) if s > 0]
            indexed_scores.sort(key=lambda x: x[1], reverse=True)

            docs = []
            for idx, score in indexed_scores[:top_k]:
                doc = self._bm25_docs[idx]
                docs.append({
                    "token": doc.get("token", ""),
                    "title": doc.get("title", ""),
                    "path": doc.get("path", ""),
                    "score": score,
                })
            return docs
        except Exception as e:
            logger.warning(f"BM25 检索失败: {e}")
            return []

    def _rrf_fusion(
        self,
        vector_results: List[Dict[str, Any]],
        bm25_results: List[Dict[str, Any]],
        k: int = 60,
        bm25_weight: float = 1.5,
    ) -> List[Dict[str, Any]]:
        """
        RRF（Reciprocal Rank Fusion）融合两个检索结果

        RRF(d) = Σ weight_i/(k + rank_i(d))，k 是常数（通常 60）
        bm25_weight > 1.0 时 BM25 结果权重更高（中文场景推荐）
        """
        scores: Dict[str, float] = {}
        doc_map: Dict[str, Dict[str, Any]] = {}

        # 向量检索排名（权重 1.0）
        for rank, doc in enumerate(vector_results):
            token = doc["token"]
            scores[token] = scores.get(token, 0) + 1.0 / (k + rank + 1)
            doc_map[token] = doc

        # BM25 检索排名（权重更高）
        for rank, doc in enumerate(bm25_results):
            token = doc["token"]
            scores[token] = scores.get(token, 0) + bm25_weight / (k + rank + 1)
            if token not in doc_map:
                doc_map[token] = doc

        # 按 RRF 分数排序
        sorted_tokens = sorted(scores.keys(), key=lambda t: scores[t], reverse=True)

        results = []
        for token in sorted_tokens:
            doc = doc_map[token]
            # 从 bm25_docs 中查找完整内容
            full_doc = next((d for d in self._bm25_docs if d.get("token") == token), None)
            if full_doc:
                results.append({
                    "token": token,
                    "title": full_doc.get("title", ""),
                    "path": full_doc.get("path", ""),
                    "content": full_doc.get("content", ""),
                    "score": scores[token],
                })

        return results

    def _filter_by_title_relevance(
        self, query: str, docs: List[Dict[str, Any]], threshold: float = 0.35
    ) -> List[Dict[str, Any]]:
        """
        用 Embedding 相似度过滤标题与 query 明显不相关的文档

        threshold: 相似度低于此值的文档被过滤（0.35 比较宽松，只过滤明显不相关的）
        """
        import numpy as np

        if not docs:
            return docs

        titles = [doc["title"] for doc in docs]
        # 计算 query 和所有标题的 Embedding
        query_emb = np.array(self._embedding_fn._encode([query]))  # (1, dim)
        title_embs = np.array(self._embedding_fn._encode(titles))  # (n, dim)

        # 余弦相似度（已 L2 归一化，直接点积）
        similarities = (query_emb @ title_embs.T).flatten()

        filtered = []
        removed = []
        for i, doc in enumerate(docs):
            if similarities[i] >= threshold:
                filtered.append(doc)
            else:
                removed.append(f"{doc['title']}({similarities[i]:.3f})")

        # 至少保留 3 个结果，避免过度过滤
        if len(filtered) < 3 and len(docs) >= 3:
            filtered = docs[:3]

        if removed:
            logger.info(f"标题相关性过滤: 移除{len(removed)}篇(阈值{threshold}): {removed}")

        return filtered

    def remove_documents(self, tokens: List[str]) -> None:
        """删除文档"""
        if tokens:
            self._collection.delete(ids=tokens)

    @property
    def doc_count(self) -> int:
        """已索引文档数量"""
        return self._collection.count()
