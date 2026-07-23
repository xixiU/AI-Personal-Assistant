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
        self.timeout_mode = "time"  # 超时模式: time / rounds
        self.max_time = 300  # 总时间限制（秒）
        self.tool_timeout = 30  # 单个工具超时（秒），并行批量时生效
        self.repo_manager = None  # 多仓库管理器（由外部注入）

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

    def set_repo_manager(self, repo_manager):
        """
        设置多仓库管理器（用于多仓库代码排查）

        Args:
            repo_manager: RepoManager 实例
        """
        self.repo_manager = repo_manager
        self.git_tools = repo_manager.current
        self.git_tools_enabled = True
        logger.info(f"多仓库管理器已注入: 当前仓库={repo_manager.current_repo_name}")

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

        触发条件（仅两种，必须明确）：
        1. Git 工具已启用（前提）
        2. 显式斜杠指令（/排查、/查代码、/code）
        3. 图片消息（日志截图）

        注：不根据任何关键词（报错、异常、版本号等）自动触发，
        避免误触发普通文档查询（如"fastjson2 报错怎么解决"其实是查文档）。
        用户需要代码排查时，必须用斜杠指令或发送日志截图。

        Args:
            messages: 消息列表

        Returns:
            是否使用 Agentic 模式
        """
        if not self.git_tools_enabled or not self.git_tools:
            return False

        # 1. 显式斜杠指令检测
        last_user_text = self._extract_last_user_text(messages)
        if self._has_explicit_command(last_user_text):
            logger.info("触发 Agentic 模式：显式指令")
            return True

        # 2. 检查是否有图片（日志截图）
        for msg in messages:
            for content in msg.content:
                if content.type == "image":
                    logger.info("触发 Agentic 模式：检测到图片消息")
                    return True

        return False

    def _has_explicit_command(self, text: str) -> bool:
        """
        检测显式斜杠指令

        Args:
            text: 用户消息文本

        Returns:
            是否包含显式指令
        """
        explicit_commands = ["/code", "/排查", "/查代码"]
        text_lower = text.lower()
        return any(cmd in text_lower for cmd in explicit_commands)

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
            return self._send_with_context_agentic(
                messages, 
                doc_context, 
                session_id, 
                max_rounds=self.max_rounds,
                timeout_mode=self.timeout_mode,
                max_time=self.max_time
            )

        # 标准 RAG 模式
        logger.info("使用标准 RAG 模式")
        return self._send_with_context_standard(messages, doc_context, session_id)

    def _send_with_context_agentic(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
        max_rounds: int = 6,
        timeout_mode: str = "time",
        max_time: int = 300,
    ) -> str:
        """
        Agentic 模式：支持工具调用的多轮对话

        Args:
            messages: 消息列表
            doc_context: 文档上下文
            session_id: 会话 ID
            max_rounds: 最大工具调用轮数
            timeout_mode: 超时模式 "time" / "rounds"
            max_time: 总时间限制（秒），timeout_mode="time" 时生效

        Returns:
            AI 最终回复
        """
        from ai_assistant.tools.git_tools import GIT_TOOLS_SCHEMA

        # 构建工具列表（多仓库时追加 switch_repo 工具）
        tools_schema = list(GIT_TOOLS_SCHEMA)
        if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
            tools_schema.append({
                "name": "switch_repo",
                "description": "切换当前代码排查的目标仓库。切换后，search_code、read_file、list_dir 等工具将在新仓库中执行。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_name": {
                            "type": "string",
                            "description": "目标仓库名称"
                        }
                    },
                    "required": ["repo_name"]
                }
            })

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
            "工具使用最佳实践（重要）：",
            "",
            "【并行调用】",
            "- 当需要尝试多种搜索策略时，一次返回多个 tool_use 并行执行，显著提升速度",
            "  示例场景：",
            "  · 不确定用哪个关键词：同时搜 'servicelog'、'ServiceLog'、'internet_service_log'",
            "  · 查实体类对应的表：同时搜 'InternetServiceLog' 和 'class InternetServiceLog'",
            "  · 多个独立文件：同时 read_file 多个无依赖关系的文件",
            "",
            "【search_code 策略】",
            "- 优先使用简单关键词，避免复杂正则（git grep 对正则支持有限）",
            "  ✅ 正确：search_code('servicelog') 然后过滤 *Controller.java",
            "  ❌ 错误：search_code('@RequestMapping.*servicelog') # 会失败",
            "- 查找实体类对应的表：搜 'class <实体名>' 找 pojo 文件，再读文件前 30 行查看 @Table 注解",
            "",
            "【read_file 策略】",
            "- 大文件（>300行）应先用 search_code 定位关键行号，再用 start_line/end_line 读取上下文",
            "  示例：search_code 找到第 150 行 → read_file(path=..., start_line=120, end_line=180)",
            "- 小文件（<200行）直接全读",
            "- 只需要查看注解或类定义时，只读前 50 行：read_file(path=..., end_line=50)",
            "",
            "【list_dir 策略】",
            "- 不确定具体文件名时使用",
            "- 确定文件名后直接 read_file 或 search_code，不要反复 list_dir",
            "",
            "注意事项：",
            "- 必须始终使用中文回答",
            "- 代码定位要精确到文件名和行号",
            "- 如果无法定位问题，诚实告知并给出可能的排查方向",
            "- 工具调用失败时尝试其他搜索关键词，但避免重复相同的失败策略",
            "- 优先使用并行工具调用减少总轮数，提升响应速度"
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

        # 注入多仓库描述（帮助 AI 判断该去哪个仓库查）
        if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
            system_parts.append("")
            system_parts.append(self.repo_manager.get_repo_descriptions())

        system_prompt = "\n".join(system_parts)

        # Agentic 循环
        import time
        start_time = time.time()
        round_num = 0
        
        while True:
            round_num += 1
            
            # 检查超时条件
            elapsed = time.time() - start_time
            if timeout_mode == "time":
                if elapsed > max_time:
                    logger.warning(f"达到总时间限制 {max_time}s，已执行 {round_num-1} 轮，耗时 {elapsed:.1f}s")
                    break
                logger.info(f"Agentic 轮次 {round_num}/∞, 已耗时 {elapsed:.1f}s/{max_time}s")
            elif timeout_mode == "rounds":
                if round_num > max_rounds:
                    logger.warning(f"达到最大轮数 {max_rounds}，返回当前结果")
                    break
                logger.info(f"Agentic 轮次 {round_num}/{max_rounds}")
            else:
                logger.info(f"Agentic 轮次 {round_num}")

            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=api_messages,
                    tools=tools_schema,
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
                        result_str = json.dumps(result, ensure_ascii=False)
                        # 多仓库模式下，工具结果前缀加当前仓库名
                        if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
                            result_str = f"[当前仓库: {self.repo_manager.current_repo_name}] {result_str}"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result_str
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

        # 达到超时限制
        if timeout_mode == "time":
            return "⚠️ 排查过程较复杂，已达到总时间限制。以上是目前的分析结果，如需继续请提供更多信息。"
        else:
            return "⚠️ 排查过程较复杂，已达到最大分析轮数。以上是目前的分析结果，如需继续请提供更多信息。"

    def _execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> Any:
        """
        执行工具调用，带智能批量优化

        Args:
            tool_name: 工具名称
            tool_input: 工具参数

        Returns:
            工具执行结果（可能包含批量扩展的结果）
        """
        if not self.git_tools:
            return {"error": "Git 工具未初始化"}

        # 【应用层智能批量】针对高频场景自动扩展并行任务
        if tool_name == "search_code":
            query = tool_input["query"]
            ref = tool_input.get("ref")
            path_filter = tool_input.get("path_filter")

            # 检测模式：查找实体类对应的表（Java 后端高频场景）
            # 启发规则：查询关键词看起来像类名（首字母大写，驼峰）
            if self._looks_like_entity_search(query, path_filter):
                logger.info(f"检测到实体类搜索模式，自动扩展并行搜索: {query}")
                # 并行搜索多种模式
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                    futures = {
                        "原始": executor.submit(self.git_tools.search_code, query, ref, path_filter),
                        "类定义": executor.submit(self.git_tools.search_code, f"class {query}", ref, "*.java"),
                        "表注解": executor.submit(self.git_tools.search_code, f"@Table.*{query}", ref, "*/pojo/*.java")
                    }

                    # 收集结果
                    merged_results = []
                    for label, future in futures.items():
                        try:
                            result = future.result(timeout=self.tool_timeout)
                            if result.get("results"):
                                # 给每个结果加标签
                                for r in result["results"]:
                                    r["_search_type"] = label
                                merged_results.extend(result["results"])
                        except Exception as e:
                            logger.warning(f"并行搜索 {label} 失败: {e}")

                    # 去重（同一文件+行号只保留一个）
                    seen = set()
                    unique_results = []
                    for r in merged_results:
                        key = (r["file"], r["line"])
                        if key not in seen:
                            seen.add(key)
                            unique_results.append(r)

                    if unique_results:
                        # 如果找到唯一的 pojo 文件，预读前 30 行（可能包含 @Table 注解）
                        pojo_files = [r["file"] for r in unique_results if "/pojo/" in r["file"] or r["file"].endswith("/" + query + ".java")]
                        if len(set(pojo_files)) == 1:
                            logger.info(f"找到唯一实体类文件 {pojo_files[0]}，预读注解")
                            try:
                                file_content = self.git_tools.read_file(pojo_files[0], ref, end_line=30)
                                return {
                                    "query": query,
                                    "ref": ref or self.git_tools.default_ref,
                                    "results": unique_results,
                                    "total": len(unique_results),
                                    "entity_file_preview": file_content  # 附加预读结果
                                }
                            except Exception as e:
                                logger.warning(f"预读实体类文件失败: {e}")

                        return {
                            "query": query,
                            "ref": ref or self.git_tools.default_ref,
                            "results": unique_results,
                            "total": len(unique_results),
                            "note": "已自动扩展搜索：原始关键词 + 类定义 + 表注解"
                        }

            # 常规搜索（无扩展）
            return self.git_tools.search_code(query=query, ref=ref, path_filter=path_filter)

        elif tool_name == "list_refs":
            return self.git_tools.list_refs(tool_input.get("pattern"))
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
        elif tool_name == "switch_repo":
            if not self.repo_manager:
                return {"error": "多仓库管理器未初始化"}
            result_msg = self.repo_manager.switch_repo(tool_input["repo_name"])
            # 切换后更新当前 git_tools 引用
            self.git_tools = self.repo_manager.current
            return {"message": result_msg}
        else:
            return {"error": f"未知工具: {tool_name}"}

    def _looks_like_entity_search(self, query: str, path_filter: str = None) -> bool:
        """
        判断是否像在搜索实体类（Java 后端场景）

        启发规则：
        - 首字母大写，驼峰命名
        - 长度 > 5 字符（排除单个词）
        - 没有特殊字符（不是正则表达式）
        - 路径过滤为空或包含 Java 相关
        """
        if not query or len(query) < 5:
            return False

        # 首字母大写 + 包含大写字母（驼峰）
        if not query[0].isupper() or not any(c.isupper() for c in query[1:]):
            return False

        # 不包含正则特殊字符
        if any(c in query for c in r'.*+?[]{}()^$|\\'):
            return False

        # 路径过滤检查
        if path_filter:
            if not any(ext in path_filter.lower() for ext in ['.java', '*.java', 'java']):
                return False

        return True

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
