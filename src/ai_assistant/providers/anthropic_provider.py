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
    PromptSafetyResult,
    _KEYWORD_EXTRACTION_SYSTEM_PROMPT,
    _parse_keyword_extraction_response,
    _PROMPT_SAFETY_SYSTEM_PROMPT,
    _parse_prompt_safety_response,
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
        feedback_manager=None,
    ):
        """
        Args:
            api_key: Anthropic API Key
            model: 模型名称
            base_url: API 基础 URL（可选，用于代理或兼容服务）
            timeout: 请求超时时间（秒）
            feedback_manager: 反馈管理器实例（可选）
        """
        super().__init__()
        self.model = model
        self.timeout = timeout
        self.feedback_manager = feedback_manager

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
        self.tool_timeout = 30  # 单个工具超时（秒），由外部配置注入
        self.repo_manager = None  # 多仓库管理器（由外部注入）

        # 会话级 Agentic 粘性标记：一旦某 session 进入过工具使用（代码排查）模式，
        # 就把 session_id 记在这里。后续该 session 的所有追问都自动继承 Agentic 模式，
        # 不再依赖上下文消息窗口中是否还留有 mode=="agentic" 的历史回复。
        #
        # 背景（修复的原始 bug）：原实现靠遍历 messages 找 assistant.metadata.mode=="agentic"
        # 来判断"追问继承"。但 ContextManager 只保留最近 max_messages 条消息（默认 10），
        # 多轮追问后，最早那条带 agentic 标记的回复会被挤出窗口，导致第二/第三次追问
        # 掉回 RAG 模式。改用会话级集合持久记录，彻底摆脱窗口滑动的影响。
        import threading as _threading
        self._agentic_sessions: set = set()
        self._agentic_sessions_lock = _threading.Lock()

        # 运维工具（由外部注入）
        self.operations_tools = None
        self.operations_enabled = False
        self._operations_sessions: set = set()  # 运维模式会话粘性标记
        self._operations_sessions_lock = _threading.Lock()

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

    def set_operations_tools(self, operations_tools, enabled: bool = True):
        """
        设置运维工具（用于运维操作）

        Args:
            operations_tools: OperationsTools 实例
            enabled: 是否启用
        """
        self.operations_tools = operations_tools
        self.operations_enabled = enabled
        logger.info(f"运维工具已{'启用' if enabled else '禁用'}")

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

    def classify_prompt_safety(self, query_text: str) -> PromptSafetyResult:
        """
        使用 Claude 判断用户输入是否为提示词攻击 / 敏感信息窃取。
        判定失败时 fail-open（返回放行值），避免误伤正常用户。
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=150,
                messages=[{"role": "user", "content": query_text}],
                system=_PROMPT_SAFETY_SYSTEM_PROMPT,
            )
            text = response.content[0].text.strip()
            result = _parse_prompt_safety_response(text, query_text[:50])
            if result.is_attack:
                logger.info(
                    f"Claude 安全判定: attack={result.is_attack}, type={result.attack_type}, "
                    f"query='{query_text[:50]}'"
                )
            return result
        except Exception as e:
            logger.warning(f"Claude 安全判定失败，放行本次请求: {e}")
            return PromptSafetyResult(is_attack=False, attack_type="none", reason="")

    def _should_use_agentic_mode(self, messages: List[Message], session_id: Optional[str] = None) -> bool:
        """
        判断是否应该使用 Agentic 模式（工具调用）

        触发条件（命中任一即进入）：
        1. Git 工具已启用（前提）
        2. 会话级粘性：该 session 曾进入过 Agentic 模式（持久记忆，不受上下文窗口影响）
        3. 显式斜杠指令（/排查、/查代码、/code）
        4. 工具意图关键词（"查代码""看源码""排查"等自然语言，用户引用追问也生效）
        5. 图片消息（日志截图）
        6. 追问兜底：历史对话消息里仍留有 Agentic 模式回复（自动继承）

        设计说明：
        - 条件 2 是修复"多轮追问后模式丢失"的核心。只要 session 进过一次工具模式，
          后续所有追问（包括基于第二/第三次回答的引用追问）都自动保持工具模式。
        - 条件 4 满足"普通文档检索模式下，追问携带查代码等关键词则自动切工具模式"的诉求；
          文档检索结果仍会作为上下文注入，辅助 AI 判断。
        - 关键词采用明确的"工具/代码意图"词，避免误伤纯文档查询（如"fastjson2 报错怎么解决"）。

        Args:
            messages: 消息列表
            session_id: 会话 ID（用于会话级粘性判断）

        Returns:
            是否使用 Agentic 模式
        """
        if not self.git_tools_enabled or not self.git_tools:
            return False

        # 1. 会话级粘性：该 session 进过工具模式，则一直保持（不受消息窗口滑动影响）
        if session_id and self._is_agentic_session(session_id):
            logger.info(f"触发 Agentic 模式：会话 {session_id} 已进入工具使用模式（会话级粘性继承）")
            return True

        last_user_text = self._extract_last_user_text(messages)

        # 2. 显式斜杠指令检测
        if self._has_explicit_command(last_user_text):
            logger.info("触发 Agentic 模式：显式指令")
            return True

        # 3. 工具意图关键词检测（自然语言，支持引用追问触发）
        if self._has_tool_intent_keyword(last_user_text):
            logger.info("触发 Agentic 模式：命中工具意图关键词（如查代码/看源码/排查）")
            return True

        # 4. 检查是否有图片（日志截图）
        for msg in messages:
            for content in msg.content:
                if content.type == "image":
                    logger.info("触发 Agentic 模式：检测到图片消息")
                    return True

        # 5. 追问兜底：检查历史是否有 Agentic 模式回复（窗口内仍能命中时）
        for msg in messages:
            if msg.role == "assistant" and msg.metadata.get("mode") == "agentic":
                logger.info("触发 Agentic 模式：历史对话中使用过代码排查模式（自动继承）")
                return True

        return False

    def _is_agentic_session(self, session_id: str) -> bool:
        """判断 session 是否已被标记为 Agentic 粘性会话（线程安全）"""
        with self._agentic_sessions_lock:
            return session_id in self._agentic_sessions

    def _mark_agentic_session(self, session_id: Optional[str]) -> None:
        """
        将 session 标记为 Agentic 粘性会话（线程安全）。

        由 _send_with_context 在确认走 Agentic 分支后调用，保证后续同 session
        的追问持续继承工具使用模式。
        """
        if not session_id:
            return
        with self._agentic_sessions_lock:
            if session_id not in self._agentic_sessions:
                self._agentic_sessions.add(session_id)
                logger.info(f"会话 {session_id} 已标记为工具使用模式（后续追问自动继承）")

    def _is_operations_session(self, session_id: str) -> bool:
        """判断 session 是否已被标记为运维模式会话（线程安全）"""
        with self._operations_sessions_lock:
            return session_id in self._operations_sessions

    def _mark_operations_session(self, session_id: Optional[str]) -> None:
        """
        将 session 标记为运维模式会话（线程安全）。

        由 _send_with_context 在确认走 Operations 分支后调用，保证后续同 session
        的追问持续继承运维模式。
        """
        if not session_id:
            return
        with self._operations_sessions_lock:
            if session_id not in self._operations_sessions:
                self._operations_sessions.add(session_id)
                logger.info(f"会话 {session_id} 已标记为运维模式（后续追问自动继承）")

    def _should_use_operations_mode(self, messages: List[Message], session_id: Optional[str] = None) -> bool:
        """
        判断是否应该使用 Operations 模式（运维工具调用）

        触发条件（命中任一即进入）：
        1. 运维工具已启用（前提）
        2. 会话级粘性：该 session 曾进入过 Operations 模式
        3. 显式 /运维 前缀

        Args:
            messages: 消息列表
            session_id: 会话 ID（用于会话级粘性判断）

        Returns:
            是否使用 Operations 模式
        """
        if not self.operations_enabled or not self.operations_tools:
            logger.debug(f"运维模式未启用: operations_enabled={self.operations_enabled}, operations_tools={self.operations_tools is not None}")
            return False

        # 1. 会话级粘性：该 session 进过运维模式，则一直保持
        if session_id and self._is_operations_session(session_id):
            return True

        last_user_text = self._extract_last_user_text(messages)

        # 2. 检测 /运维 或 /ops（支持前面有 @ 提及等前缀）
        text_stripped = last_user_text.strip()
        if "/运维" in text_stripped or "/ops" in text_stripped:
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

    def _has_tool_intent_keyword(self, text: str) -> bool:
        """
        检测自然语言中的"工具/代码意图"关键词。

        用于满足诉求：普通文档检索模式下，用户在（引用）追问中携带"查代码"等关键词时，
        自动进入工具使用模式；文档检索结果仍会注入上下文辅助判断。

        关键词选取偏保守，均为明确指向"查看/排查代码、源码、实现、调用链"的表达，
        避免误伤纯文档查询（如"报错怎么解决""某功能怎么用"）。

        Args:
            text: 用户消息文本

        Returns:
            是否命中工具意图关键词
        """
        if not text:
            return False
        text_lower = text.lower()
        tool_intent_keywords = [
            "查代码", "查下代码", "查一下代码", "查查代码", "查看代码", "看代码", "看下代码",
            "看源码", "查源码", "看看源码", "读源码", "读代码",
            "查实现", "看实现", "实现逻辑", "源码实现", "代码实现",
            "调用链", "调用关系", "谁调用", "调用了", "在哪里定义", "定义在哪",
            "排查", "排查一下", "排查下", "定位问题", "定位一下", "定位下", "trace",
            "查提交", "查commit", "改动历史", "谁改的", "哪次提交",
        ]
        return any(kw in text_lower for kw in tool_intent_keywords)

    def _send_with_context(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
    ) -> tuple[str, dict]:
        """
        发送消息到 Claude 并获取回复

        根据消息内容自动选择模式：
        - /运维 前缀 → Operations 模式（运维工具调用）
        - 有图片或排查关键词 → Agentic 模式（代码排查工具调用）
        - 其他 → 标准 RAG 模式

        Returns:
            (reply_text, metadata) 元组
            metadata 包含：tool_rounds（Agentic/Operations）或 doc_count（RAG）等
        """
        # 判断是否使用 Operations 模式
        if self._should_use_operations_mode(messages, session_id):
            logger.info("使用 Operations 模式（运维工具调用）")
            self._mark_operations_session(session_id)
            return self._send_with_context_operations(
                messages,
                doc_context,
                session_id,
                max_rounds=self.max_rounds,
                timeout_mode=self.timeout_mode,
                max_time=self.max_time
            )

        # 判断是否使用 Agentic 模式
        if self._should_use_agentic_mode(messages, session_id):
            logger.info("使用 Agentic 模式（工具调用）")
            # 标记会话为工具使用模式，后续追问自动继承（修复多轮追问模式丢失）
            self._mark_agentic_session(session_id)
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
    ) -> tuple[str, dict]:
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
            (reply_text, metadata) 元组
            metadata 包含 tool_rounds（实际工具调用轮数）
        """
        from ai_assistant.tools.git_tools import GIT_TOOLS_SCHEMA

        # 构建工具列表（多仓库时追加 switch_repo 工具）
        tools_schema = list(GIT_TOOLS_SCHEMA)
        if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
            tools_schema.append({
                "name": "switch_repo",
                "description": "切换当前代码排查的目标仓库。切换后，search_code、find_files、read_file、list_dir、run_git_command 等工具将在新仓库中执行。注意：本工具会改变全局状态，必须单独一轮调用，不要和查询类工具放在同一轮。",
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
            "1. 【默认先动手排查，不要先反问】用户发日志截图就是要你分析。",
            "   - 未指定版本 → 直接用仓库的默认分支（default_ref）排查，不必先问版本",
            "   - 未指定组件 → 从日志的包名/类名/路径推断仓库",
            "     （如 com.iflytek.icourt + ts-service 路径 → ts-service），",
            "     推断不出再看仓库描述里的默认仓库",
            "   - 只有在\"信息缺失到根本无法动手\"时才提问",
            "     （例如截图完全无法辨认、或同一异常在多个仓库都存在且无法区分）",
            "   - 需要澄清时也要先给出已能得到的分析结论，把问题放在最后，不要只回一堆问题",
            "2. 从日志中提取关键信息（异常类名、错误信息、堆栈类名+行号）",
            "3. 用 search_code / find_files 定位异常抛出点（可与第 2 步的多个关键词并行搜索）",
            "4. 用 read_file 读取定位到的文件，查看上下文代码（建议读取抛错位置前后 30 行）",
            "5. 沿调用链向上追（谁调用了它、超时/参数从哪来），必要时用 run_git_command 看改动历史",
            "6. 综合日志和代码，给出根因分析、代码定位（文件:行号）、修复建议",
            "",
            "【回复质量要求 —— 很重要】",
            "- 日志里已有的信息（异常类型、堆栈行号、方法名）只是起点，不是结论。",
            "  仅仅把日志内容复述一遍、不去读代码，是不合格的回复",
            "- 必须真正打开代码验证：抛错那一行在做什么、超时时间配在哪、",
            "  调用的下游地址/参数从哪来、有没有重试和兜底",
            "- 给结论时要落到具体文件:行号，并说明\"为什么会走到这里\"",
            "- 如果排查后仍无法确定根因，说明已排除了什么、还剩哪些可能、下一步怎么验证",
            "",
            "工具使用最佳实践（重要）：",
            "",
            "【并行调用 —— 这是提速的关键，请尽量用】",
            "- search_code / find_files / read_file / list_dir 都是只读工具，一次返回多个",
            "  tool_use 会并行执行，耗时约等于最慢的那一个，而不是相加",
            "- 每一轮都先想清楚\"这一步我需要哪几份信息\"，一次全部发出，不要一个个试",
            "  示例场景：",
            "  · 不确定用哪个关键词：同时搜 'servicelog'、'ServiceLog'、'internet_service_log'",
            "  · 前后端一起查：同时搜后端字段名和前端调用名",
            "  · 多个独立文件：同时 read_file 多个无依赖关系的文件",
            "- 注意：switch_repo 会改变当前仓库，必须单独一轮调用，不要和查询混在一起",
            "",
            "【search_code 策略】",
            "- 支持正则；若正则语法无效会自动退回字面量搜索，不必自我审查语法",
            "- 想看匹配点周围的代码，直接加 context 参数，不要再单独调 read_file",
            "  ✅ 高效：search_code('judgeUseSimplifyNum', context=10) # 一次拿到匹配+上下文",
            "  ❌ 低效：search_code(...) 拿到行号 → 再 read_file 读那一段 # 多花一整轮",
            "- 搜不到时，先换更短、更可能出现在代码里的关键词（函数名/字段名/API 路径），",
            "  而不是反复搜界面文案。界面文案常被拆分或写在别处，搜不到不代表功能不存在",
            "",
            "【find_files 策略（只知道文件名时用它）】",
            "- 只知道文件名、不知道路径：find_files('RecordList.vue') 直接拿到完整路径",
            "- 不要用 list_dir 一层层试探目录，那会浪费很多轮次",
            "",
            "【path_filter 用法】",
            "- 可以直接给文件名（'RecordList.vue'），系统会自动匹配任意目录下的该文件",
            "- 也可用通配符（'*.java'）或目录（'src/main/java'）",
            "- 重要：带 path_filter 搜不到时，去掉 path_filter 再搜一次全仓库确认，",
            "  不要仅凭一次带过滤的空结果就断定代码里没有",
            "",
            "【read_file 策略】",
            "- 大文件（>300行）先用 search_code 定位行号，再用 start_line/end_line 读上下文",
            "- 小文件（<200行）直接全读；只看注解或类定义时只读前 50 行",
            "",
            "【查提交历史/作者】",
            "- 用 run_git_command，例如：",
            "  · 谁改的、什么时候：['log', '--oneline', '-10', '--', '<文件路径>']",
            "  · 某几行是谁写的：['blame', '-L', '520,540', '--', '<文件路径>']",
            "  · 按提交信息找改动：['log', '--all', '--grep=log4j', '--oneline']",
            "- 不要回复\"我无法查看 git 历史\"，你有这个能力",
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

        # 注入负反馈提示
        feedback_prompt = self._build_feedback_prompt(session_id)
        if feedback_prompt:
            system_parts.append(feedback_prompt)

        # 注入多仓库描述（帮助 AI 判断该去哪个仓库查）
        if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
            system_parts.append("")
            system_parts.append(self.repo_manager.get_repo_descriptions())

        system_prompt = "\n".join(system_parts)

        # Agentic 循环
        import time
        start_time = time.time()
        round_num = 0

        # 智能搜索失败检测
        search_fail_count = 0
        repo_switch_count = 0

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

                # 如果没有工具调用,返回最终文本
                if response.stop_reason == "end_turn" or not tool_uses:
                    final_text = ""
                    for block in response.content:
                        if hasattr(block, 'text'):
                            final_text += block.text
                    logger.info(f"Agentic 完成: 总轮数={round_num}, 最终回复={len(final_text)}字符")

                    # 构建 metadata（Agentic 模式）
                    metadata = {
                        "mode": "agentic",
                        "tool_rounds": round_num,
                    }

                    return (final_text or "抱歉，未能生成有效回复。"), metadata

                # 执行工具调用（search_code 可并行，其他串行）
                tool_results = []

                # 纯读工具可安全并行（无共享状态）；switch_repo 会改变当前仓库，必须串行
                PARALLEL_SAFE = {"search_code", "find_files", "read_file", "list_dir"}
                all_readonly = all(tool_use.name in PARALLEL_SAFE for tool_use in tool_uses)
                can_parallel = all_readonly and len(tool_uses) >= 2

                if can_parallel:
                    # 并行执行多个只读查询
                    names = ", ".join(t.name for t in tool_uses)
                    logger.info(f"并行执行 {len(tool_uses)} 个只读工具: {names}")
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(tool_uses), 5)) as executor:
                        futures = {}
                        for tool_use in tool_uses:
                            future = executor.submit(self._execute_tool, tool_use.name, tool_use.input)
                            futures[future] = tool_use

                        for future in concurrent.futures.as_completed(futures):
                            tool_use = futures[future]
                            tool_name = tool_use.name
                            tool_input = tool_use.input
                            try:
                                result = future.result(timeout=30)
                                result_str = json.dumps(result, ensure_ascii=False)
                                if self.repo_manager and len(self.repo_manager.list_repos()) > 1:
                                    result_str = f"[当前仓库: {self.repo_manager.current_repo_name}] {result_str}"
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "content": result_str
                                })
                                logger.info(f"工具 {tool_name} 完成: {len(result_str)} 字符")
                            except Exception as e:
                                logger.error(f"并行工具 {tool_name} 失败: {e}")
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "content": json.dumps({"error": str(e)}, ensure_ascii=False),
                                    "is_error": True
                                })
                else:
                    # 串行执行（包含非搜索工具或单个工具）
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

                            # === 智能检测逻辑（仅监控，不注入额外消息避免API错误）===
                            # 1. 检测连续搜索失败
                            if tool_name == "search_code":
                                if isinstance(result, dict) and (not result.get("results") or len(result.get("results", [])) == 0):
                                    search_fail_count += 1
                                    if search_fail_count >= 3:
                                        logger.warning(f"⚠️ 连续{search_fail_count}次搜索失败，AI可能需要调整策略")
                                        search_fail_count = 0  # 重置避免重复日志
                                else:
                                    search_fail_count = 0  # 搜索成功，重置计数

                            # 2. 检测仓库频繁切换
                            if tool_name == "switch_repo":
                                repo_switch_count += 1
                                if repo_switch_count >= 3:
                                    logger.warning(f"⚠️ 第{repo_switch_count}次切换仓库，性能损耗较大")

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
                return "⏱️ AI 服务响应超时，请稍后重试", {"mode": "agentic", "error": "timeout"}
            except anthropic.APIConnectionError as e:
                logger.error(f"Anthropic API 连接失败: {e}")
                return "🔌 AI 服务连接失败，请检查网络或稍后重试", {"mode": "agentic", "error": "connection"}
            except anthropic.APIStatusError as e:
                logger.error(f"Anthropic API 错误: status={e.status_code}, message={e.message}")

                # 400错误通常是请求格式问题，可能是tool_result错误，尝试移除最后一轮并继续
                if e.status_code == 400 and "tool_result" in str(e.message).lower() and round_num > 1:
                    logger.warning(f"检测到tool_result错误，回退到上一轮并继续（当前轮次: {round_num}）")
                    # 移除最后一轮的assistant+user消息（tool_use和tool_result）
                    if len(api_messages) >= 2:
                        api_messages = api_messages[:-2]
                        logger.info(f"已回退，当前消息数: {len(api_messages)}")
                        continue  # 重试当前轮次

                return f"❌ AI 服务调用失败: {e.message}", {"mode": "agentic", "error": "api"}
            except Exception as e:
                logger.error(f"Agentic 循环异常: {e}", exc_info=True)
                return f"❌ 排查过程出错: {str(e)}", {"mode": "agentic", "error": "exception"}

        # 达到超时限制
        timeout_msg = "⚠️ 排查过程较复杂，已达到总时间限制。以上是目前的分析结果，如需继续请提供更多信息。" if timeout_mode == "time" else "⚠️ 排查过程较复杂，已达到最大分析轮数。以上是目前的分析结果，如需继续请提供更多信息。"
        return timeout_msg, {"mode": "agentic", "tool_rounds": round_num, "timeout": True}

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

        # 简单直接地执行工具调用
        if tool_name == "run_git_command":
            args = tool_input.get("args", [])
            return self.git_tools.run_git_command(args)
        elif tool_name == "search_code":
            return self.git_tools.search_code(
                query=tool_input["query"],
                ref=tool_input.get("ref"),
                path_filter=tool_input.get("path_filter"),
                context=tool_input.get("context", 0)
            )
        elif tool_name == "find_files":
            return self.git_tools.find_files(
                name_pattern=tool_input["name_pattern"],
                ref=tool_input.get("ref")
            )
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

    def _send_with_context_operations(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
        max_rounds: int = 6,
        timeout_mode: str = "time",
        max_time: int = 300,
    ) -> tuple[str, dict]:
        """
        Operations 模式：支持运维工具调用的多轮对话

        Args:
            messages: 消息列表
            doc_context: 文档上下文
            session_id: 会话 ID
            max_rounds: 最大工具调用轮数
            timeout_mode: 超时模式 "time" / "rounds"
            max_time: 总时间限制（秒），timeout_mode="time" 时生效

        Returns:
            (reply_text, metadata) 元组
            metadata 包含 tool_rounds（实际工具调用轮数）
        """
        if not self.operations_tools:
            return "❌ 运维工具未初始化", {"mode": "operations", "error": "not_initialized"}

        # 生成运维工具的 Claude tools schema
        tools_schema = self._generate_operations_tools_schema()

        # 转换消息格式，移除 /运维 前缀
        api_messages = []
        for msg in messages:
            content_parts = []
            for content in msg.content:
                if content.type == "text":
                    text = content.data
                    # 移除 /运维 或 /ops 前缀
                    if text.strip().startswith("/运维"):
                        text = text.strip()[3:].strip()
                    elif text.strip().startswith("/ops"):
                        text = text.strip()[4:].strip()
                    content_parts.append({"type": "text", "text": text})
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

        # 获取操作者身份信息（从最后一条消息的 metadata 中提取）
        operator_info = "未知用户"
        operator_id = ""
        operator_name = ""
        source = "feishu"
        chat_id = None  # 用于群组白名单检查
        if messages:
            last_msg = messages[-1]
            if last_msg.metadata:
                operator_name = last_msg.metadata.get("operator_name", "")
                operator_id = last_msg.metadata.get("operator_id", "")
                source = last_msg.metadata.get("source", "") or "feishu"
                chat_id = last_msg.metadata.get("chat_id")  # 提取 chat_id
                if operator_name or operator_id:
                    operator_info = f"{operator_name} ({operator_id}, {source})"

        # 只读鉴权门：仅允许白名单内的运维同事 / 授权群组使用查询能力
        if self.operations_tools and self.operations_tools.operations_manager:
            from ai_assistant.core.operations_models import OperatorIdentity
            ops_manager = self.operations_tools.operations_manager
            operator = OperatorIdentity(
                user_id=operator_id or "unknown",
                username=operator_name or operator_id or "unknown",
                metadata={"channel": source},
            )
            # 任取一台机器做白名单/群组校验（授权是渠道级别，与具体机器无关）
            any_machine = next(iter(ops_manager.machines.values()), None)
            if not ops_manager.is_authorized(
                operator, any_machine, chat_id=chat_id
            ):
                logger.warning(
                    f"运维查询被拒绝：用户 {operator.username} "
                    f"(user_id={operator_id}, chat_id={chat_id}) 不在授权白名单/群组中"
                )
                return (
                    "抱歉，您没有运维查询权限。请联系管理员将您加入运维白名单，"
                    "或在已授权的运维群组中使用本功能。",
                    {"mode": "operations", "error": "unauthorized"},
                )

        # 构建 system prompt（运维助手指引）
        system_parts = [
            "你是运维助手，当前用户是授权运维人员。",
            "",
        ]

        # 添加可用机器列表
        if self.operations_tools and self.operations_tools.operations_manager:
            ops_manager = self.operations_tools.operations_manager
            machines = ops_manager.machines
            applications = ops_manager.applications  # Dict[str, Application]
            if machines:
                system_parts.append("可用机器列表：")
                for machine in machines.values():  # machines 是 Dict[str, Machine]，需要 .values()
                    # 别名/自然语言映射来自 display_name 和 tags
                    alias_parts = []
                    if machine.display_name and machine.display_name != machine.name:
                        alias_parts.append(machine.display_name)
                    if machine.tags:
                        alias_parts.extend(machine.tags)
                    aliases = ", ".join(alias_parts) if alias_parts else machine.name
                    # host 在 ssh_config 中
                    host = machine.ssh_config.host if machine.ssh_config else "未知"
                    machine_desc = "- {} (别名/标签: {}, 主机: {})".format(
                        machine.name, aliases, host
                    )
                    system_parts.append(machine_desc)

                    # 列出该机器上的应用详情（别名、部署路径、描述）
                    machine_apps = [
                        app for app in applications.values()
                        if machine.name in app.machines
                    ]
                    for app in machine_apps:
                        app_aliases = ", ".join(app.aliases) if app.aliases else "无"
                        app_line = "    · 应用「{}」(别名: {}".format(app.name, app_aliases)
                        if app.path:
                            app_line += ", 部署路径: {}".format(app.path)
                        if app.description:
                            app_line += ", 说明: {}".format(app.description)
                        app_line += ")"
                        system_parts.append(app_line)
                system_parts.append("")
                # 引导 AI 用部署路径精确区分同名服务
                system_parts.append(
                    "重要：当多个应用同名但部署路径不同时（如不同版本/架构的 ts-service），"
                    "必须依据上面列出的『部署路径』来区分。查询进程时用 get_app_status 的 app_name "
                    "传入应用的部署路径（或路径中的唯一片段），而不是笼统的服务名，"
                    "以免把不同版本的进程混在一起。\n"
                    "重要：判断『某端口是否有服务在运行』或『哪个进程占用了某端口』时，"
                    "必须调用 get_port_info(machine_name, port)，禁止用 get_app_status(app_name=端口号)——"
                    "端口号不在进程命令行里，那样查不到。get_port_info 返回的 cwd（工作目录）和 cmdline "
                    "可与上面的部署路径比对，从而确认到底是哪个版本的服务在监听该端口。"
                )
                system_parts.append("")

        system_parts.extend([
            "可用工具（所有工具都需要 machine_name 参数指定目标机器）：",
            "- get_app_status(machine_name, app_name) - 查看应用运行状态",
            "- get_app_version(machine_name, source, jar_path/log_path/version_file/api_url) - 查看应用版本/分支",
            "- get_jar_info(machine_name, jar_path) - 分析 Java 应用 jar 包（获取 MANIFEST、Maven 信息、Git 版本）",
            "- get_process_info(machine_name, pid/app_name) - 查看进程详情（CPU、内存、端口、线程数）",
            "- get_port_info(machine_name, port) - 按端口号查询监听进程（PID、命令行、工作目录），判断端口是否有服务在运行时必须用它",
            "- get_system_metrics(machine_name) - 查看系统资源（CPU、内存、磁盘、负载）",
            "- get_logs(machine_name, log_path, lines, grep_pattern) - 查看应用日志",
            "",
            "本助手仅提供【只读查询】能力，不提供重启/停止/启动等任何有副作用的操作。"
            "如果用户要求重启、停止、启动、部署、修改配置等写操作，请礼貌说明本助手只支持查询，"
            "相关变更请运维同事手动执行。",
            "",
            "工具使用规则：",
            "1. **machine_name 参数**：使用上面列出的机器名称或别名",
            "2. **自然语言理解**：用户说'研发环境'时，使用别名映射到对应的机器名",
            "3. **错误处理**：如果工具返回 success=false，查看 error 字段了解原因",
            "4. **只读约束**：只能查询配置中定义的机器和应用，不得尝试执行任何变更类命令",
            "",
            f"当前用户：{operator_info}",
            "",
            "工作流程：",
            "1. 理解用户的查询需求（查看状态、版本、日志、端口、资源等）",
            "2. 使用合适的只读工具收集信息",
            "3. 给出清晰的结论和建议",
            "",
            "回复格式要求：",
            "- 使用中文回答",
            "- 查询时明确说明目标（哪台机器、哪个应用）",
            "- 查询完成后给出清晰的结果摘要（状态、资源使用、版本信息等）",
            "- 如果工具调用失败，解释可能的原因（机器不存在、连接失败等）",
        ])

        # 注入飞书文档（如果有）
        if doc_context:
            system_parts.append("")
            system_parts.append(doc_context)

        system_prompt = "\n".join(system_parts)

        # Operations Agentic 循环
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
                logger.info(f"Operations 轮次 {round_num}/∞, 已耗时 {elapsed:.1f}s/{max_time}s")
            elif timeout_mode == "rounds":
                if round_num > max_rounds:
                    logger.warning(f"达到最大轮数 {max_rounds}，返回当前结果")
                    break
                logger.info(f"Operations 轮次 {round_num}/{max_rounds}")
            else:
                logger.info(f"Operations 轮次 {round_num}")

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
                    logger.info(f"Operations 完成: 总轮数={round_num}, 最终回复={len(final_text)}字符")

                    # 构建 metadata（Operations 模式）
                    metadata = {
                        "mode": "operations",
                        "tool_rounds": round_num,
                    }

                    return (final_text or "抱歉，未能生成有效回复。"), metadata

                # 执行工具调用（串行执行，因为运维操作可能有状态变更）
                tool_results = []

                for tool_use in tool_uses:
                    tool_name = tool_use.name
                    tool_input = tool_use.input
                    logger.info(f"执行运维工具: {tool_name}({tool_input})")

                    try:
                        result = self._execute_operations_tool(tool_name, tool_input, chat_id=chat_id)
                        result_str = json.dumps(result, ensure_ascii=False)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result_str
                        })
                        logger.debug(f"运维工具 {tool_name} 结果: {result}")

                    except Exception as e:
                        logger.error(f"运维工具 {tool_name} 执行失败: {e}")
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
                return "⏱️ AI 服务响应超时，请稍后重试", {"mode": "operations", "error": "timeout"}
            except anthropic.APIConnectionError as e:
                logger.error(f"Anthropic API 连接失败: {e}")
                return "🔌 AI 服务连接失败，请检查网络或稍后重试", {"mode": "operations", "error": "connection"}
            except anthropic.APIStatusError as e:
                logger.error(f"Anthropic API 错误: status={e.status_code}, message={e.message}")
                return f"❌ AI 服务调用失败: {e.message}", {"mode": "operations", "error": "api"}
            except Exception as e:
                logger.error(f"Operations 循环异常: {e}", exc_info=True)
                return f"❌ 运维操作过程出错: {str(e)}", {"mode": "operations", "error": "exception"}

        # 达到超时限制
        timeout_msg = "⚠️ 运维操作较复杂，已达到总时间限制。以上是目前的结果，如需继续请提供更多信息。" if timeout_mode == "time" else "⚠️ 运维操作较复杂，已达到最大轮数。以上是目前的结果，如需继续请提供更多信息。"
        return timeout_msg, {"mode": "operations", "tool_rounds": round_num, "timeout": True}

    def _generate_operations_tools_schema(self) -> List[Dict[str, Any]]:
        """
        从 OperationsTools 实例自动生成 Claude tools schema

        Returns:
            Claude tools schema 列表
        """
        if not self.operations_tools:
            return []

        import inspect

        schema = []

        # 定义需要暴露给 AI 的方法（排除内部方法和辅助方法）
        exposed_methods = [
            'get_app_status',
            'get_app_version',
            'get_jar_info',
            'get_process_info',
            'get_port_info',
            'get_system_metrics',
            'get_logs',
        ]

        for method_name in exposed_methods:
            method = getattr(self.operations_tools, method_name, None)
            if not method or not callable(method):
                continue

            # 获取方法签名和文档
            sig = inspect.signature(method)
            doc = inspect.getdoc(method) or f"{method_name} 方法"

            # 提取文档第一行作为描述
            description_lines = doc.split('\n\n')[0].strip().split('\n')
            description = ' '.join(line.strip() for line in description_lines if line.strip())

            tool = {
                "name": method_name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }

            # 首先添加 machine_name 参数（AI 传入机器名称字符串）
            tool["input_schema"]["properties"]["machine_name"] = {
                "type": "string",
                "description": "目标机器名称（支持模糊匹配，如 'web-1' 或 '生产服务器1'）"
            }
            tool["input_schema"]["required"].append("machine_name")

            # 解析其他参数（跳过 self 和 machine）
            for param_name, param in sig.parameters.items():
                if param_name in ('self', 'machine', 'operator_id'):
                    # operator_id 由系统自动注入，不需要 AI 提供
                    continue

                # 根据类型注解确定参数类型
                param_type = "string"
                if param.annotation != inspect.Parameter.empty:
                    if param.annotation == int:
                        param_type = "integer"
                    elif param.annotation == bool:
                        param_type = "boolean"
                    elif hasattr(param.annotation, '__origin__'):
                        # 处理 Optional[str] 等类型
                        origin = getattr(param.annotation, '__origin__', None)
                        if origin is list:
                            param_type = "array"

                # 从文档中提取参数描述
                param_description = f"{param_name} 参数"
                if 'Args:' in doc:
                    args_section = doc.split('Args:')[1].split('Returns:')[0]
                    for line in args_section.split('\n'):
                        if param_name + ':' in line or param_name + ' :' in line:
                            desc_parts = line.split(':', 1)
                            if len(desc_parts) > 1:
                                param_description = desc_parts[1].strip()
                                break

                tool["input_schema"]["properties"][param_name] = {
                    "type": param_type,
                    "description": param_description
                }

                # 必需参数（没有默认值且不是 Optional）
                if param.default == inspect.Parameter.empty:
                    tool["input_schema"]["required"].append(param_name)

            schema.append(tool)

        logger.info(f"自动生成 {len(schema)} 个运维工具 schema")
        return schema

    def _execute_operations_tool(self, tool_name: str, tool_input: Dict[str, Any], chat_id: Optional[str] = None) -> Any:
        """
        执行运维工具调用

        Args:
            tool_name: 工具名称
            tool_input: 工具参数
            chat_id: 群组/会话ID（用于群组白名单检查）

        Returns:
            工具执行结果
        """
        if not self.operations_tools:
            return {"success": False, "error": "运维工具未初始化", "data": None}

        try:
            # 获取方法
            method = getattr(self.operations_tools, tool_name, None)
            if not method or not callable(method):
                return {"success": False, "error": f"未知运维工具: {tool_name}", "data": None}

            # OperationsTools 的方法第一个参数是 Machine 对象
            # AI 传入的是机器名称字符串，需要转换
            # 但并非所有工具都需要 machine 参数（如 list_machines）

            # 检查方法签名中是否需要 machine 参数和 chat_id 参数
            import inspect
            sig = inspect.signature(method)
            params = list(sig.parameters.keys())

            # 如果方法需要 machine 参数，从 operations_manager 查找
            if 'machine' in params and self.operations_tools.operations_manager:
                machine_name = tool_input.pop('machine_name', None)
                if not machine_name:
                    return {"success": False, "error": "缺少 machine_name 参数", "data": None}

                machine = self.operations_tools.operations_manager.find_machine(machine_name)
                if not machine:
                    return {
                        "success": False,
                        "error": f"未找到机器: {machine_name}",
                        "data": None
                    }

                # 如果方法需要 chat_id 参数，传入
                if 'chat_id' in params:
                    result = method(machine, chat_id=chat_id, **tool_input)
                else:
                    result = method(machine, **tool_input)
            else:
                # 不需要 machine 参数，直接调用
                if 'chat_id' in params:
                    result = method(chat_id=chat_id, **tool_input)
                else:
                    result = method(**tool_input)

            # 确保返回格式统一
            if not isinstance(result, dict):
                result = {"success": True, "data": result, "error": None}

            return result

        except Exception as e:
            logger.error(f"运维工具 {tool_name} 执行异常: {e}", exc_info=True)
            return {"success": False, "error": str(e), "data": None}

    def _send_with_context_standard(
        self,
        messages: List[Message],
        doc_context: str,
        session_id: Optional[str] = None,
    ) -> tuple[str, dict]:
        """
        标准 RAG 模式（原有实现）

        Returns:
            (reply_text, metadata) 元组
            metadata 包含 doc_count（检索到的文档数）等
        """
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

            # 注入负反馈提示
            feedback_prompt = self._build_feedback_prompt(session_id)
            if feedback_prompt:
                system_parts.append(feedback_prompt)

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

            # 构建 metadata（标准 RAG 模式）
            # 统计文档数：doc_context 中每个 "## " 开头表示一篇文档
            doc_count = 0
            if doc_context:
                doc_count = doc_context.count("\n## ")
                # 如果第一行就是 ##，需要额外加1
                if doc_context.startswith("## "):
                    doc_count += 1

            metadata = {
                "mode": "rag",
                "doc_count": doc_count,
            }

            return reply, metadata

        except anthropic.APITimeoutError:
            logger.error("Anthropic API 请求超时")
            return "⏱️ AI 服务响应超时，请稍后重试", {"mode": "rag", "error": "timeout"}
        except anthropic.APIConnectionError as e:
            logger.error(f"Anthropic API 连接失败: {e}")
            return "🔌 AI 服务连接失败，请检查网络或稍后重试", {"mode": "rag", "error": "connection"}
        except anthropic.APIStatusError as e:
            status_code = e.status_code
            logger.error(f"Anthropic API 错误: status={status_code}, message={e.message}")
            logger.error(f"请求详情: base_url={self.client.base_url}, model={self.model}")
            logger.error(f"响应体: {e.response.text if hasattr(e.response, 'text') else 'N/A'}")

            # 针对特定错误码返回友好消息
            if status_code == 401:
                return "❌ Anthropic API 认证失败，请检查 API Key 是否正确", {"mode": "rag", "error": "auth"}
            elif status_code == 402:
                return "💳 Anthropic 账户余额不足，请充值后重试", {"mode": "rag", "error": "quota"}
            elif status_code == 429:
                return "⏱️ Anthropic API 请求过于频繁，请稍后重试", {"mode": "rag", "error": "rate_limit"}
            elif status_code >= 500:
                return "🔧 Anthropic 服务暂时不可用，请稍后重试", {"mode": "rag", "error": "server"}
            else:
                return f"❌ AI 服务调用失败: {e.message}", {"mode": "rag", "error": "api"}

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

    def _build_feedback_prompt(self, session_id: Optional[str]) -> str:
        """
        构建负反馈提示文本

        Args:
            session_id: 会话 ID

        Returns:
            反馈提示文本，如果没有负反馈则返回空字符串
        """
        if not self.feedback_manager or not session_id:
            return ""

        feedbacks = self.feedback_manager.get_session_negative_feedbacks(session_id, limit=3)
        if not feedbacks:
            return ""

        # 提取 record_ids，并用 chat_history 反查原始 query/answer
        record_ids = [fb.get("record_id") for fb in feedbacks if fb.get("record_id")]
        if not record_ids or not self._chat_history:
            return ""

        records = self._chat_history.get_records_by_ids(record_ids)

        lines = ["\n\n【用户反馈提示】", "在本次对话中，用户对你的以下回答表示不满意：", ""]

        injected = 0
        for fb in feedbacks:
            record = records.get(fb.get("record_id"))
            if not record:
                continue  # 找不到历史记录就跳过

            query = record.get("query", "")
            answer = record.get("answer", "")
            lines.append(f"问题：「{query[:100]}」")
            lines.append(f"你的回答：「{answer[:150]}...」")
            if fb.get("feedback_text"):
                lines.append(f"用户反馈：「{fb['feedback_text']}」")
            lines.append("")
            injected += 1

        if injected == 0:
            return ""

        lines.append("请在本次回答中注意改进，提供更准确、更具体的信息。")

        logger.info(f"注入 {injected} 条负反馈到 Prompt")
        return "\n".join(lines)

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

