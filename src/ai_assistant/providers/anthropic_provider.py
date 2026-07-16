"""
Anthropic Claude Provider

使用 Anthropic API 调用 Claude 模型，支持集成飞书文档管理器。
文档内容在调用前注入 system prompt，Claude 基于文档生成回复。

支持 Agentic 模式：Claude 可以主动调用工具（如代码搜索、文件读取）进行多轮排查。
"""

from typing import Any, Dict, List, Optional
from loguru import logger
import anthropic
import json
import re

from ai_assistant.core.ai_provider import (
    AIProvider,
    KeywordExtractionResult,
    _KEYWORD_EXTRACTION_SYSTEM_PROMPT,
    _parse_keyword_extraction_response,
)
from ai_assistant.core.models import Message


class AnthropicProvider(AIProvider):
    """Anthropic Claude Provider"""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        base_url: str = None,
        timeout: int = 90,
    ):
        """
        Args:
            api_key: Anthropic API Key
            model: 模型名称
            base_url: API 基础 URL（可选，用于代理或兼容服务）
            timeout: 请求超时时间（秒）
        """
        super().__init__()
        self.model = model
        self.timeout = timeout

        # 初始化 Anthropic 客户端
        client_kwargs = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
            logger.info(f"使用自定义 base_url: {base_url}")

        self.client = anthropic.Anthropic(**client_kwargs)
        logger.info(f"Anthropic Provider 初始化: model={model}, base_url={base_url or '默认'}")

        # Git 工具（由外部注入）
        self.git_tools = None
        self.git_tools_enabled = False
        self.branch_hint = ""  # 版本号→分支映射提示
        self.max_rounds = 6  # Agentic 最大轮数，由外部注入覆盖

    def set_git_tools(self, git_tools, enabled: bool = True, branch_hint: str = ""):
        """
        设置 git 工具（用于代码排查）

        Args:
            git_tools: GitTools 实例
            enabled: 是否启用
            branch_hint: 版本号→分支映射提示（注入 system prompt）
        """
        self.git_tools = git_tools
        self.git_tools_enabled = enabled
        self.branch_hint = branch_hint
        logger.info(f"Git 工具已{'启用' if enabled else '禁用'}，branch_hint={'已配置' if branch_hint else '未配置'}")

    def extract_keywords(self, query_text: str) -> KeywordExtractionResult:
        """
        使用 Claude 从用户查询中提取搜索关键词 + 判断是否通用技术问题
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=150,
                messages=[{"role": "user", "content": query_text}],
                system=_KEYWORD_EXTRACTION_SYSTEM_PROMPT,
            )
            text = response.content[0].text.strip()
            result = _parse_keyword_extraction_response(text, query_text[:50])
            logger.info(
                f"Claude 关键词提取: keywords={result.keywords}, "
                f"generic={result.is_generic_tech}, query='{query_text[:50]}'"
            )
            return result
        except Exception as e:
            logger.warning(f"Claude 关键词提取失败，返回降级值: {e}")
            return KeywordExtractionResult(keywords=[], is_generic_tech=False)

    def _should_use_agentic_mode(self, messages: List[Message]) -> bool:
        """
        判断是否应该使用 Agentic 模式（工具调用）

        触发条件（按优先级）：
        1. Git 工具已启用
        2. 显式指令（/code、/排查、/查代码、/search）
        3. 明确意图词（结合代码、查代码、排查等）
        4. 技术特征词（模块名、接口路径、版本号）
        5. 上下文延续（前一轮提到代码意图）
        6. 图片消息（可能是日志截图）
        7. 排查关键词（原有逻辑）

        Args:
            messages: 消息列表

        Returns:
            是否使用 Agentic 模式
        """
        if not self.git_tools_enabled or not self.git_tools:
            return False

        # 提取最后一条用户消息文本
        last_user_text = self._extract_last_user_text(messages)
        last_user_text_lower = last_user_text.lower()

        # 1. 显式指令检测（最高优先级）
        if self._has_explicit_command(last_user_text):
            logger.info("触发 Agentic 模式：显式指令")
            return True

        # 2. 明确意图词检测
        intent_keywords = self._check_code_intent_keywords(last_user_text_lower)
        if intent_keywords:
            logger.info(f"触发 Agentic 模式：意图关键词 [{', '.join(intent_keywords)}]")
            return True

        # 3. 技术特征词检测
        tech_features = self._check_tech_features(last_user_text)
        if tech_features:
            logger.info(f"触发 Agentic 模式：技术特征 [{', '.join(tech_features)}]")
            return True

        # 4. 上下文延续检测
        if self._should_continue_agentic(messages):
            logger.info("触发 Agentic 模式：上下文延续（前一轮提到代码相关内容）")
            return True

        # 5. 检查是否有图片（原有逻辑）
        for msg in messages:
            for content in msg.content:
                if content.type == "image":
                    logger.info("触发 Agentic 模式：检测到图片消息")
                    return True

        # 6. 检查文本中是否有排查关键词（原有逻辑）
        troubleshoot_keywords = ["排查", "报错", "异常", "错误", "bug", "问题", "崩溃", "failed", "error", "exception"]
        if any(kw in last_user_text_lower for kw in troubleshoot_keywords):
            logger.info("触发 Agentic 模式：排查关键词")
            return True

        return False

    def _has_explicit_command(self, text: str) -> bool:
        """
        检测显式指令

        Args:
            text: 用户消息文本

        Returns:
            是否包含显式指令
        """
        explicit_commands = ["/code", "/排查", "/查代码", "/search"]
        text_lower = text.lower()
        return any(cmd in text_lower for cmd in explicit_commands)

    def _check_code_intent_keywords(self, text_lower: str) -> List[str]:
        """
        检测明确意图词

        Args:
            text_lower: 用户消息文本（小写）

        Returns:
            匹配到的关键词列表
        """
        intent_keywords = [
            "结合代码", "查代码", "看代码", "读代码", "分析代码",
            "代码在哪", "哪里实现", "定位代码", "找代码",
            "代码排查", "查看代码", "检查代码"
        ]
        matched = [kw for kw in intent_keywords if kw in text_lower]
        return matched

    def _check_tech_features(self, text: str) -> List[str]:
        """
        检测技术特征词（模块名、接口路径、版本号）

        Args:
            text: 用户消息文本

        Returns:
            匹配到的技术特征列表
        """
        features = []

        # 检测模块名（以 -service 结尾，确保以字母或数字开头）
        service_pattern = r'([a-zA-Z0-9][\w-]*-service)'
        services = re.findall(service_pattern, text, re.IGNORECASE)
        if services:
            features.extend([f"模块名:{s}" for s in services[:2]])  # 最多记录2个

        # 检测接口路径（以 / 开头，包含字母数字和常见路径字符）
        api_pattern = r'/[a-zA-Z0-9/_-]{2,}'
        apis = re.findall(api_pattern, text)
        if apis:
            features.extend([f"接口路径:{a}" for a in apis[:2]])  # 最多记录2个

        # 检测版本号（如 4.3.6、v1.2.3、V4.0，兼容中文环境）
        # 使用前后均可为非字母数字字符的模式，兼容中文标点
        version_pattern = r'(?<![a-zA-Z0-9])[vV]?\d+\.\d+(?:\.\d+)?(?![a-zA-Z0-9])'
        versions = re.findall(version_pattern, text)
        if versions:
            features.extend([f"版本号:{v}" for v in versions[:2]])  # 最多记录2个

        return features

    def _should_continue_agentic(self, messages: List[Message]) -> bool:
        """
        检测上下文延续：前一轮消息中是否提到代码相关意图

        Args:
            messages: 消息列表

        Returns:
            是否应该延续 Agentic 模式
        """
        # 检查最近3轮对话中是否有代码相关意图
        context_keywords = [
            "结合代码", "查代码", "看代码", "读代码", "分析代码",
            "代码在哪", "哪里实现", "定位代码", "代码排查"
        ]

        # 从倒数第二条消息开始检查（跳过当前消息）
        for i in range(len(messages) - 2, max(-1, len(messages) - 7), -1):
            if i < 0:
                break
            msg = messages[i]
            if msg.role == "user":
                msg_text = "".join(c.data for c in msg.content if c.type == "text").lower()
                if any(kw in msg_text for kw in context_keywords):
                    return True

        return False

    def _send_with_context(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
    ) -> str:
        """
        发送消息到 Claude 并获取回复

        根据消息内容自动选择模式：
        - 有图片或排查关键词 → Agentic 模式（支持工具调用）
        - 其他 → 标准 RAG 模式
        """
        # 判断是否使用 Agentic 模式
        if self._should_use_agentic_mode(messages):
            logger.info("使用 Agentic 模式（工具调用）")
            return self._send_with_context_agentic(messages, doc_context, session_id, max_rounds=self.max_rounds)

        # 标准 RAG 模式
        logger.info("使用标准 RAG 模式")
        return self._send_with_context_standard(messages, doc_context, session_id)

    def _send_with_context_agentic(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
        max_rounds: int = 6,
    ) -> str:
        """
        Agentic 模式：支持工具调用的多轮对话

        Args:
            messages: 消息列表
            doc_context: 文档上下文
            session_id: 会话 ID
            max_rounds: 最大工具调用轮数

        Returns:
            AI 最终回复
        """
        from ai_assistant.tools.git_tools import GIT_TOOLS_SCHEMA

        # 转换消息格式
        api_messages = []
        for msg in messages:
            content_parts = []
            for content in msg.content:
                if content.type == "text":
                    content_parts.append({"type": "text", "text": content.data})
                elif content.type == "image" and isinstance(content.data, dict):
                    content_parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": content.data.get("media_type", "image/png"),
                            "data": content.data["data"],
                        }
                    })

            if content_parts:
                api_messages.append({"role": msg.role, "content": content_parts})

        # 构建 system prompt（Agentic 排查指引）
        system_parts = [
            "你是一个智能代码排查助手，帮助用户分析日志并定位代码问题。",
            "",
            "工作流程：",
            "1. 如果用户只提供了日志截图但未说明版本或问题描述，请先询问补充信息（版本号、问题现象等），不要贸然排查",
            "2. 确认版本后，用 list_refs 工具查找对应的分支或 tag",
            "3. 从日志中提取关键信息（异常类名、错误信息、堆栈行号）",
            "4. 用 search_code 工具在对应版本的代码中搜索异常类或错误信息",
            "5. 用 read_file 工具读取定位到的文件，查看上下文代码（建议读取抛错位置前后 30 行）",
            "6. 综合日志和代码，给出根因分析、代码定位（文件:行号）、修复建议",
            "",
            "注意事项：",
            "- 必须始终使用中文回答",
            "- 代码定位要精确到文件名和行号",
            "- 如果无法定位问题，诚实告知并给出可能的排查方向",
            "- 工具调用失败时不要放弃，尝试其他搜索关键词或路径"
        ]

        # 注入版本号→分支映射提示
        if self.branch_hint:
            system_parts.append("")
            system_parts.append("版本号与分支映射规则：")
            system_parts.append(self.branch_hint)

        # 注入飞书文档（如果有）
        if doc_context:
            system_parts.append("")
            system_parts.append(doc_context)

        system_prompt = "\n".join(system_parts)

        # Agentic 循环
        round_num = 0
        while round_num < max_rounds:
            round_num += 1
            logger.info(f"Agentic 轮次 {round_num}/{max_rounds}")

            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=api_messages,
                    tools=GIT_TOOLS_SCHEMA,
                )

                logger.info(
                    f"Claude 响应: stop_reason={response.stop_reason}, "
                    f"tokens(input:{response.usage.input_tokens}, output:{response.usage.output_tokens})"
                )

                # 收集本轮的 assistant 消息内容
                assistant_content = []
                tool_uses = []

                for block in response.content:
                    if block.type == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input
                        })
                        tool_uses.append(block)

                # 将 assistant 消息加入对话历史
                api_messages.append({"role": "assistant", "content": assistant_content})

                # 如果没有工具调用，返回最终文本
                if response.stop_reason == "end_turn" or not tool_uses:
                    final_text = ""
                    for block in response.content:
                        if hasattr(block, 'text'):
                            final_text += block.text
                    logger.info(f"Agentic 完成: 总轮数={round_num}, 最终回复={len(final_text)}字符")
                    return final_text or "抱歉，未能生成有效回复。"

                # 执行工具调用
                tool_results = []
                for tool_use in tool_uses:
                    tool_name = tool_use.name
                    tool_input = tool_use.input
                    logger.info(f"执行工具: {tool_name}({tool_input})")

                    try:
                        result = self._execute_tool(tool_name, tool_input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps(result, ensure_ascii=False)
                        })
                        logger.debug(f"工具 {tool_name} 结果: {result}")
                    except Exception as e:
                        logger.error(f"工具 {tool_name} 执行失败: {e}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps({"error": str(e)}, ensure_ascii=False),
                            "is_error": True
                        })

                # 将工具结果加入对话历史
                api_messages.append({"role": "user", "content": tool_results})

            except anthropic.APITimeoutError:
                logger.error("Anthropic API 请求超时")
                return "⏱️ AI 服务响应超时，请稍后重试"
            except anthropic.APIConnectionError as e:
                logger.error(f"Anthropic API 连接失败: {e}")
                return "🔌 AI 服务连接失败，请检查网络或稍后重试"
            except anthropic.APIStatusError as e:
                logger.error(f"Anthropic API 错误: status={e.status_code}, message={e.message}")
                return f"❌ AI 服务调用失败: {e.message}"
            except Exception as e:
                logger.error(f"Agentic 循环异常: {e}", exc_info=True)
                return f"❌ 排查过程出错: {str(e)}"

        # 达到最大轮数
        logger.warning(f"达到最大轮数 {max_rounds}，返回当前结果")
        return "⚠️ 排查过程较复杂，已达到最大分析轮数。以上是目前的分析结果，如需继续请提供更多信息。"

    def _execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> Any:
        """
        执行工具调用

        Args:
            tool_name: 工具名称
            tool_input: 工具参数

        Returns:
            工具执行结果
        """
        if not self.git_tools:
            return {"error": "Git 工具未初始化"}

        if tool_name == "list_refs":
            return self.git_tools.list_refs(tool_input.get("pattern"))
        elif tool_name == "search_code":
            return self.git_tools.search_code(
                query=tool_input["query"],
                ref=tool_input.get("ref"),
                path_filter=tool_input.get("path_filter")
            )
        elif tool_name == "read_file":
            return self.git_tools.read_file(
                path=tool_input["path"],
                ref=tool_input.get("ref"),
                start_line=tool_input.get("start_line"),
                end_line=tool_input.get("end_line")
            )
        elif tool_name == "list_dir":
            return self.git_tools.list_dir(
                path=tool_input.get("path", ""),
                ref=tool_input.get("ref")
            )
        else:
            return {"error": f"未知工具: {tool_name}"}

    def _send_with_context_standard(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
    ) -> str:
        """标准 RAG 模式（原有实现）"""
        try:
            # 转换消息格式
            api_messages = []
            for msg in messages:
                content_parts = []
                for content in msg.content:
                    if content.type == "text":
                        content_parts.append({"type": "text", "text": content.data})
                    elif content.type == "image" and isinstance(content.data, dict):
                        # 图片内容：{"data": base64, "media_type": "image/png"}
                        content_parts.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": content.data.get("media_type", "image/png"),
                                "data": content.data["data"],
                            }
                        })

                if content_parts:
                    api_messages.append({"role": msg.role, "content": content_parts})

            # 构建 system prompt
            system_parts = ["你是一个智能助手，帮助用户回答问题。你必须始终使用中文回答，包括技术术语的解释也要用中文。即使用户用英文提问，你也要用中文回答。"]

            # 注入文档内容（由基类传入）
            if doc_context:
                system_parts.append(doc_context)
                system_parts.append(
                    "请基于以上文档内容回答用户的问题。如果文档中没有相关信息，请如实告知。\n"
                    "回答完成后，在末尾附加参考文档链接（除非用户明确要求不附加），格式如下：\n"
                    "---\n"
                    "📎 参考文档：\n"
                    "- [文档标题](原文链接)"
                )

            system_prompt = "\n\n".join(system_parts)

            # 调用 Anthropic API
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=api_messages,
            )

            reply = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    reply += block.text
            if not reply:
                reply = "抱歉，未能生成有效回复。"
            logger.info(
                f"Claude 回复已接收: {len(reply)} 字符, "
                f"tokens(input:{response.usage.input_tokens}, output:{response.usage.output_tokens})"
            )
            return reply

        except anthropic.APITimeoutError:
            logger.error("Anthropic API 请求超时")
            return "⏱️ AI 服务响应超时，请稍后重试"
        except anthropic.APIConnectionError as e:
            logger.error(f"Anthropic API 连接失败: {e}")
            return "🔌 AI 服务连接失败，请检查网络或稍后重试"
        except anthropic.APIStatusError as e:
            status_code = e.status_code
            logger.error(f"Anthropic API 错误: status={status_code}, message={e.message}")
            logger.error(f"请求详情: base_url={self.client.base_url}, model={self.model}")
            logger.error(f"响应体: {e.response.text if hasattr(e.response, 'text') else 'N/A'}")

            # 针对特定错误码返回友好消息
            if status_code == 401:
                return "❌ Anthropic API 认证失败，请检查 API Key 是否正确"
            elif status_code == 402:
                return "💳 Anthropic 账户余额不足，请充值后重试"
            elif status_code == 429:
                return "⏱️ Anthropic API 请求过于频繁，请稍后重试"
            elif status_code >= 500:
                return "🔧 Anthropic 服务暂时不可用，请稍后重试"
            else:
                return f"❌ AI 服务调用失败: {e.message}"

    def check_health(self) -> bool:
        """检查 Anthropic API 服务健康状态"""
        try:
            # 发送一个简单请求验证 API Key 有效
            response = self.client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception as e:
            logger.warning(f"Anthropic 健康检查失败: {e}")
            return False

    def filter_docs_by_relevance(self, query: str, candidates: List[Dict[str, Any]], max_docs: int = 3) -> List[int]:
        """用 Claude 判断候选文档标题与 query 的相关性，返回 0-based 下标列表"""
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

            response = self.client.messages.create(
                model=self.model,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            logger.info(f"Claude 标题过滤输出: {text}")

            selected_indices = []
            for part in text.replace('，', ',').split(','):
                try:
                    idx = int(part.strip())
                    if 1 <= idx <= len(candidates):
                        selected_indices.append(idx - 1)  # 转为 0-based
                except ValueError:
                    continue
            return selected_indices[:max_docs]
        except Exception as e:
            logger.warning(f"Claude 标题过滤失败: {e}")
            return []
