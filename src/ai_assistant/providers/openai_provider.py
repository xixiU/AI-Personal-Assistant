"""
OpenAI 兼容 API Provider

支持所有 OpenAI 兼容的 API，包括：
- OpenAI 官方 API
- Azure OpenAI
- Ollama
- LM Studio
- 其他兼容 OpenAI API 的服务
"""

import requests
from typing import Any, Dict, List, Optional
from loguru import logger
from ai_assistant.core.ai_provider import (
    AIProvider,
    KeywordExtractionResult,
    _KEYWORD_EXTRACTION_SYSTEM_PROMPT,
    _parse_keyword_extraction_response,
)
from ai_assistant.core.models import Message


class OpenAIProvider(AIProvider):
    """OpenAI 兼容 API Provider"""

    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4", timeout: int = 30):
        """
        初始化 OpenAI Provider

        Args:
            base_url: API 基础 URL
            api_key: API 密钥（可选，某些本地服务不需要）
            model: 模型名称
            timeout: 请求超时时间（秒）
        """
        super().__init__()
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

        logger.info(f"OpenAI Provider 初始化: {base_url}, 模型: {model}")

    def _send_with_context(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
    ) -> str:
        """发送消息到 OpenAI 兼容 API"""
        try:
            # 转换消息格式为 OpenAI 格式
            api_messages = []

            # 如果有文档上下文，插入 system 消息
            if doc_context:
                system_content = (
                    f"{doc_context}\n\n"
                    "请基于以上文档内容回答用户的问题。如果文档中没有相关信息，请如实告知。\n"
                    "回答完成后，在末尾附加参考文档链接（除非用户明确要求不附加），格式如下：\n"
                    "---\n"
                    "📎 参考文档：\n"
                    "- [文档标题](原文链接)"
                )
                api_messages.append({"role": "system", "content": system_content})

            for msg in messages:
                content_text = ""
                for content in msg.content:
                    if content.type == "text":
                        content_text += content.data

                api_messages.append({
                    "role": msg.role,
                    "content": content_text
                })

            # 构建请求头
            headers = {
                "Content-Type": "application/json"
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            # 构建请求体
            payload = {
                "model": self.model,
                "messages": api_messages
            }

            # 发送请求
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout
            )
            response.raise_for_status()

            # 解析响应
            result = response.json()
            reply = result["choices"][0]["message"]["content"]

            # 提取 token 使用信息
            usage = result.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            logger.info(
                f"AI 回复已接收: {len(reply)} 字符, "
                f"tokens(input:{input_tokens}, output:{output_tokens})"
            )
            return reply

        except requests.exceptions.Timeout:
            logger.error("AI API 请求超时")
            raise Exception("AI 服务响应超时")
        except requests.exceptions.RequestException as e:
            logger.error(f"AI API 请求失败: {e}")
            raise Exception(f"AI 服务调用失败: {str(e)}")
        except (KeyError, IndexError) as e:
            logger.error(f"解析 AI 响应失败: {e}")
            raise Exception("AI 响应格式错误")

    def check_health(self) -> bool:
        """检查 API 服务健康状态"""
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = requests.get(
                f"{self.base_url}/v1/models",
                headers=headers,
                timeout=5
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"健康检查失败: {e}")
            return False

    def extract_keywords(self, query_text: str) -> KeywordExtractionResult:
        """
        使用 OpenAI 兼容接口从用户查询中提取搜索关键词 + 判断是否通用技术问题。

        兼容 Ollama 等不支持 response_format=json 的服务：先尝试 JSON mode，
        失败时降级为普通 prompt 调用，依赖 _parse_keyword_extraction_response 的
        正则/JSON 提取逻辑容错处理。
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        messages = [
            {"role": "system", "content": _KEYWORD_EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": query_text},
        ]

        # 先尝试 JSON mode（OpenAI / 支持 JSON mode 的兼容接口）
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": 150,
                "response_format": {"type": "json_object"},
            }
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            result = _parse_keyword_extraction_response(text, query_text[:50])
            logger.info(
                f"OpenAI 关键词提取(JSON mode): keywords={result.keywords}, "
                f"generic={result.is_generic_tech}, query='{query_text[:50]}'"
            )
            return result
        except Exception as e:
            logger.debug(f"JSON mode 关键词提取失败，降级为普通模式: {e}")

        # 降级：普通 prompt，依赖解析器容错
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": 150,
            }
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            result = _parse_keyword_extraction_response(text, query_text[:50])
            logger.info(
                f"OpenAI 关键词提取(普通模式): keywords={result.keywords}, "
                f"generic={result.is_generic_tech}, query='{query_text[:50]}'"
            )
            return result
        except Exception as e:
            logger.warning(f"OpenAI 关键词提取失败，返回降级值: {e}")
            return KeywordExtractionResult(keywords=[], is_generic_tech=False)

    def filter_docs_by_relevance(self, query: str, candidates: List[Dict[str, Any]], max_docs: int = 3) -> List[int]:
        """用 OpenAI 兼容接口判断候选文档标题与 query 的相关性，返回 0-based 下标列表"""
        if not candidates:
            return []
        try:
            candidates_lines = []
            for i, doc in enumerate(candidates, 1):
                path = doc.get("path", doc.get("title", ""))
                candidates_lines.append(f"{i}. {path}")
            candidates_text = "\n".join(candidates_lines)

            prompt = f"""用户查询: {query}

以下是候选文档列表（含完整父目录路径）：
{candidates_text}

请判断哪些文档标题与用户查询相关，返回相关文档的编号，用逗号分隔。只输出编号，不要其他内容。"""

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100,
            }
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            logger.info(f"OpenAI 标题过滤输出: {text}")

            selected_indices = []
            for part in text.replace('，', ',').split(','):
                try:
                    idx = int(part.strip())
                    if 1 <= idx <= len(candidates):
                        selected_indices.append(idx - 1)
                except ValueError:
                    continue
            return selected_indices[:max_docs]
        except Exception as e:
            logger.warning(f"OpenAI 标题过滤失败: {e}")
            return []
