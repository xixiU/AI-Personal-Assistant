#!/usr/bin/env python3
"""
测试 Embedding 函数，定位 tokenizer 和 ChromaDB 调用问题

运行方式：
    uv run tests/unit/test_embedding.py
"""

import sys
import os

# 获取项目根目录（从 tests/unit/ 向上两级）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

def test_tokenizer():
    """测试 tokenizer 基本功能"""
    print("=" * 60)
    print("测试 1: Tokenizer 基本功能")
    print("=" * 60)

    from transformers import AutoTokenizer

    model_dir = os.path.join(PROJECT_ROOT, "models/text2vec-base-chinese")
    print(f"加载 tokenizer: {model_dir}")

    try:
        tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
        print("✓ Tokenizer 加载成功")
    except Exception as e:
        print(f"✗ Tokenizer 加载失败: {e}")
        return False

    # 测试不同的调用方式
    test_cases = [
        (["你好"], "单个字符串列表"),
        (["你好", "世界"], "多个字符串列表"),
    ]

    for texts, desc in test_cases:
        print(f"\n测试: {desc}, 输入={texts}")
        try:
            # 方式1: 不带 return_tensors
            result = tokenizer(texts, padding=True, truncation=True, max_length=512)
            print(f"  ✓ 不带 return_tensors: {type(result)}, keys={list(result.keys())}")

            # 方式2: return_tensors="np"
            try:
                result_np = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="np")
                print(f"  ✓ return_tensors='np': {type(result_np)}")
            except Exception as e:
                print(f"  ✗ return_tensors='np' 失败: {e}")

        except Exception as e:
            print(f"  ✗ 调用失败: {e}")

    return True


def test_embedding_function():
    """测试 Embedding 函数"""
    print("\n" + "=" * 60)
    print("测试 2: Embedding 函数")
    print("=" * 60)

    from ai_assistant.core.hybrid_search import Text2VecEmbeddingFunction

    try:
        emb_fn = Text2VecEmbeddingFunction()
        print("✓ Embedding 函数初始化成功")
    except Exception as e:
        print(f"✗ Embedding 函数初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    # 测试 __call__
    print("\n测试 __call__ 方法:")
    try:
        result = emb_fn(["你好", "世界"])
        print(f"  ✓ __call__(['你好', '世界']): shape={len(result)}x{len(result[0])}")
    except Exception as e:
        print(f"  ✗ __call__ 失败: {e}")
        import traceback
        traceback.print_exc()

    # 测试 embed_query
    print("\n测试 embed_query 方法:")
    test_queries = [
        "你好",
        "部署环境有什么要求？",
    ]

    for query in test_queries:
        try:
            result = emb_fn.embed_query(query)
            print(f"  测试 embed_query('{query}'):")
            print(f"    返回类型: {type(result)}")
            print(f"    是否是列表: {isinstance(result, list)}")
            if isinstance(result, list):
                print(f"    列表长度: {len(result)}")
                if len(result) > 0:
                    print(f"    第一个元素类型: {type(result[0])}")
                    print(f"    前5个元素: {result[:5]}")
                print(f"  ✓ embed_query('{query}'): shape={len(result)}")
            else:
                print(f"    ✗ 返回值不是列表: {result}")
        except Exception as e:
            print(f"  ✗ embed_query('{query}') 失败: {e}")
            import traceback
            traceback.print_exc()

    # 测试 embed_documents
    print("\n测试 embed_documents 方法:")
    try:
        docs = ["文档1", "文档2", "文档3"]
        result = emb_fn.embed_documents(docs)
        print(f"  ✓ embed_documents({docs}): shape={len(result)}x{len(result[0])}")
    except Exception as e:
        print(f"  ✗ embed_documents 失败: {e}")
        import traceback
        traceback.print_exc()

    return True


def test_chromadb_integration():
    """测试 ChromaDB 集成"""
    print("\n" + "=" * 60)
    print("测试 3: ChromaDB 集成")
    print("=" * 60)

    import chromadb
    from ai_assistant.core.hybrid_search import Text2VecEmbeddingFunction
    import tempfile
    import shutil

    # 创建临时目录
    temp_dir = tempfile.mkdtemp(prefix="test_chroma_")
    print(f"临时目录: {temp_dir}")

    # 创建一个包装类来监控方法调用
    class DebugEmbeddingFunction(Text2VecEmbeddingFunction):
        def __call__(self, input):
            print(f"  [调用] __call__(input={input[:50] if isinstance(input, list) and input else input}...)")
            result = super().__call__(input)
            print(f"  [返回] __call__ -> {type(result)}, len={len(result)}")
            return result

        def embed_query(self, input):
            print(f"  [调用] embed_query(input='{input[:50]}...')")
            result = super().embed_query(input)
            print(f"  [返回] embed_query -> {type(result)}, len={len(result) if isinstance(result, list) else 'N/A'}")
            return result

        def embed_documents(self, input):
            print(f"  [调用] embed_documents(input={input[:2] if isinstance(input, list) else input}...)")
            result = super().embed_documents(input)
            print(f"  [返回] embed_documents -> {type(result)}, len={len(result)}")
            return result

    try:
        emb_fn = DebugEmbeddingFunction()
        client = chromadb.PersistentClient(path=temp_dir)

        print("创建 collection...")
        collection = client.get_or_create_collection(
            name="test_collection",
            metadata={"hnsw:space": "cosine"},
            embedding_function=emb_fn,
        )
        print("✓ Collection 创建成功")

        # 测试索引
        print("\n测试索引文档...")
        try:
            collection.upsert(
                ids=["doc1", "doc2"],
                documents=["这是第一篇文档", "这是第二篇文档"],
                metadatas=[{"title": "文档1"}, {"title": "文档2"}],
            )
            print("✓ 文档索引成功")
        except Exception as e:
            print(f"✗ 文档索引失败: {e}")
            import traceback
            traceback.print_exc()

        # 测试查询
        print("\n测试查询（观察 ChromaDB 调用了哪个方法）...")
        try:
            results = collection.query(
                query_texts=["第一篇"],
                n_results=2,
            )
            print(f"✓ 查询成功: {results['ids']}")
        except Exception as e:
            print(f"✗ 查询失败: {e}")
            import traceback
            traceback.print_exc()

    finally:
        # 清理临时目录
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"\n清理临时目录: {temp_dir}")

    return True


if __name__ == "__main__":
    print("开始测试 Embedding 功能\n")

    success = True
    success = test_tokenizer() and success
    success = test_embedding_function() and success
    success = test_chromadb_integration() and success

    print("\n" + "=" * 60)
    if success:
        print("✓ 所有测试通过")
    else:
        print("✗ 部分测试失败")
    print("=" * 60)

    sys.exit(0 if success else 1)
