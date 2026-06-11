import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from loguru import logger
from ai_assistant.core.models import Message


@dataclass
class KeywordExtractionResult:
    """
    关键词提取结果

    Attributes:
        keywords: 提取的搜索关键词列表（1-5 个）
        is_generic_tech: 是否为纯通用技术/基础组件问题。
            True 时可跳过本地文档检索，让大模型直接用自身知识回答。
            False（默认保守值）时继续走向量检索 + BM25。
    """
    keywords: List[str] = field(default_factory=list)
    is_generic_tech: bool = False


# 关键词提取的统一提示词（Anthropic / OpenAI 兼容接口通用）
_KEYWORD_EXTRACTION_SYSTEM_PROMPT = (
    "你是关键词提取器和问题分类器。从用户的问题中提取搜索关键词，并判断是否为纯通用技术问题。\n"
    "\n"
    "关键词提取规则：\n"
    "1. 提取 1-5 个最核心的搜索关键词\n"
    "2. 版本号必须保留（如 4.3.6、3.5、V4.0），这是业务中最重要的标识\n"
    "3. 保留专有名词、技术术语、产品名称的完整性（如【智慧法庭V4.0】不要拆分）\n"
    "4. 英文变量名、配置项保持原样（如 note_simplify_websocket_url）\n"
    "5. 去除无意义的词（如【帮我找】【是什么】【怎么】）\n"
    "\n"
    "问题类型判断（generic 字段）：\n"
    "- generic=true：纯通用技术/基础组件问题，如 Redis、Nginx、Docker、MySQL、K8s 等标准组件的\n"
    "  安装、配置、排障，且与任何业务系统完全无关\n"
    "- generic=false：凡涉及【我们的系统】、特定业务功能、产品名称、特定版本号、公司内部系统，\n"
    "  一律为 false；同时含通用技术和业务信息时也为 false；拿不准时默认 false\n"
    "\n"
    "只输出 JSON，格式如下，不要输出任何其他内容：\n"
    '{"keywords": ["关键词1", "关键词2"], "generic": false}'
)


def _parse_keyword_extraction_response(text: str, query_preview: str) -> KeywordExtractionResult:
    """
    解析 AI 返回的关键词提取 JSON，失败时返回保守降级值。

    Args:
        text: AI 原始输出文本
        query_preview: 用于日志的查询摘要（不超过 50 字符）

    Returns:
        KeywordExtractionResult（解析失败时 keywords=[], is_generic_tech=False）
    """
    text = text.strip()
    # 提取 JSON 块（部分模型会在 JSON 前后加说明文字）
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        text = text[start:end + 1]

    try:
        data = json.loads(text)
        keywords = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
        is_generic = bool(data.get("generic", False))
        return KeywordExtractionResult(keywords=keywords[:5], is_generic_tech=is_generic)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(f"关键词提取 JSON 解析失败，降级到无关键词: query='{query_preview}', error={e}, raw={text!r}")
        return KeywordExtractionResult(keywords=[], is_generic_tech=False)


class AIProvider(ABC):
    """AI Provider 抽象基类"""

    _chat_history = None  # 对话历史管理器（类级共享）

    @classmethod
    def set_chat_history(cls, chat_history):
        """设置对话历史管理器（所有 Provider 共享）"""
        cls._chat_history = chat_history

    @abstractmethod
    def send_message(self, messages: List[Message], session_id: Optional[str] = None) -> str:
        """
        发送消息到 AI 模型并获取回复

        Args:
            messages: 消息列表（包含上下文）
            session_id: 会话 ID，用于维持多用户对话上下文

        Returns:
            AI 生成的回复文本
        """
        pass

    def call(self, messages: List[Message], session_id: Optional[str] = None) -> str:
        """
        统一入口：记录日志 + 计时 + 调用 send_message + 保存历史

        所有外部调用应使用此方法，而非直接调用 send_message。
        """
        provider_name = self.__class__.__name__
        model_name = getattr(self, 'model', 'unknown')

        logger.info(f"调用 AI: provider={provider_name}, model={model_name}, messages={messages}")

        start = time.time()
        reply = self.send_message(messages, session_id=session_id)
        duration = time.time() - start

        logger.info(f"AI 回复完成: {len(reply)} 字符, 耗时={duration:.2f}s")

        # 保存对话历史
        if self._chat_history:
            try:
                # 提取最后一条用户消息作为 query
                query = ""
                for msg in reversed(messages):
                    if msg.role == "user":
                        query = " ".join(c.data for c in msg.content if c.type == "text")
                        break
                if query:
                    self._chat_history.save(
                        session_id=session_id or "unknown",
                        query=query,
                        answer=reply,
                        latency_ms=int(duration * 1000),
                    )
            except Exception as e:
                logger.warning(f"保存对话历史失败: {e}")

        return reply

    @abstractmethod
    def check_health(self) -> bool:
        """
        检查 AI 服务健康状态

        Returns:
            True 如果服务可用，否则 False
        """
        pass

    def extract_keywords(self, query_text: str) -> KeywordExtractionResult:
        """
        从用户查询中提取搜索关键词，并判断是否为通用技术问题。

        默认实现返回保守降级值（无关键词，不跳过检索）。
        各 Provider 子类应覆盖此方法，调用自身 AI 接口实现。

        Args:
            query_text: 用户查询文本

        Returns:
            KeywordExtractionResult
        """
        return KeywordExtractionResult(keywords=[], is_generic_tech=False)

    def filter_docs_by_relevance(self, query: str, candidates: List[Dict[str, Any]], max_docs: int = 3) -> List[int]:
        """
        用 AI 从候选文档列表中筛选与 query 相关的文档，返回 0-based 下标列表。

        默认实现返回空列表（由调用方降级处理）。
        各 Provider 子类应覆盖此方法。

        Args:
            query: 用户查询文本
            candidates: 候选文档列表，每项需有 "path" 或 "title" 字段
            max_docs: 最多返回的文档数

        Returns:
            选中文档的 0-based 下标列表（按相关性排序）
        """
        return []
