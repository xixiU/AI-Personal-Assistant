"""
提示词攻击防护（输入侧）

与 redactor.py（输出侧脱敏）互补：redactor 负责回复发出前遮蔽泄露的敏感信息，
本模块负责在【调用大模型之前】识别恶意输入，拒绝响应并上报告警。

拦截两类攻击：
1. 指令覆盖 / 越狱：诱导模型忽略系统设定、泄露系统提示词、进入"开发者模式/DAN"等。
2. 敏感信息窃取：诱导模型输出代码/文档/配置里的账号密码、密钥、Token 等。

判定方式：不做关键词枚举（"忘记""列出"等词在正常场景大量出现，枚举必然误伤），
而是调用 Provider 自身的 AI 接口（classify_prompt_safety）由大模型结合语境自主判断。
判定失败一律 fail-open（放行），优先保证正常用户不被误伤。

设计要点：
- 进程级单例，启动时调用 configure_guard(enabled, alert_webhook) 配置一次。
- 命中后触发的告警内容会先经过 redact() 脱敏，保证告警本身不二次泄露敏感信息。
- 告警 webhook 复用飞书文档告警 webhook（feishu_docs.alert_webhook），不单独新增配置。
"""
from typing import Optional
from loguru import logger

# 命中攻击时返回给用户的统一话术（不透露判定细节，避免攻击者试探绕过）
REFUSAL_MESSAGE = (
    "抱歉，我无法响应这个请求。我只能协助解答与业务和技术相关的问题，"
    "不能提供账号、密码、密钥等敏感信息，也不会绕过既定的安全约束。"
    "如有正常的技术疑问，欢迎随时提问。"
)


class PromptGuard:
    """提示词攻击防护（进程级单例，见 get_guard）"""

    def __init__(self) -> None:
        self.enabled: bool = True
        self._alert_webhook: Optional[str] = None

    def configure(self, enabled: bool = True, alert_webhook: Optional[str] = None) -> None:
        """配置开关与告警 webhook"""
        self.enabled = enabled
        self._alert_webhook = alert_webhook or None

    def check(self, provider, text: str, session_id: str = "unknown", source: str = "unknown"):
        """
        用 provider 的 AI 接口判断输入是否为攻击。命中时记日志 + 异步上报告警，
        返回 PromptSafetyResult；未命中或未启用返回 None（放行）。

        Args:
            provider: AIProvider 实例（提供 classify_prompt_safety 能力）
            text: 用户输入文本
            session_id: 会话 ID
            source: 来源（feishu/wechat/web）
        """
        if not self.enabled or not text or not isinstance(text, str):
            return None

        try:
            result = provider.classify_prompt_safety(text)
        except Exception as e:
            # fail-open：判定异常时放行，不影响正常用户
            logger.warning(f"AI 安全判定异常（放行本次请求）: {e}")
            return None

        if not result or not result.is_attack:
            return None

        logger.warning(
            f"🚨 拦截疑似提示词攻击: type={result.attack_type}, reason={result.reason!r}, "
            f"session={session_id}, source={source}, query_len={len(text)}"
        )
        try:
            self._send_alert(result, text, session_id, source)
        except Exception as e:
            logger.warning(f"提示词攻击告警发送失败（不影响拦截）: {e}")
        return result

    def _send_alert(self, result, text: str, session_id: str, source: str) -> None:
        """向 alert_webhook 发送飞书告警卡片（未配置则跳过）"""
        if not self._alert_webhook:
            return

        from datetime import datetime
        import requests
        # 告警中回显的用户输入先脱敏再截断，避免告警本身泄露敏感信息
        from ai_assistant.utils.redactor import redact

        preview = redact(text)[:200]
        type_label = {
            "jailbreak": "指令覆盖/越狱尝试",
            "secret_extraction": "敏感信息窃取尝试",
        }.get(result.attack_type, result.attack_type or "未知")

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": "red",
                    "title": {"content": "🚨 提示词攻击拦截告警", "tag": "plain_text"}
                },
                "elements": [
                    {"tag": "div", "text": {"content": f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "tag": "lark_md"}},
                    {"tag": "div", "text": {"content": f"**类型**: {type_label}", "tag": "lark_md"}},
                    {"tag": "div", "text": {"content": f"**来源**: {source}", "tag": "lark_md"}},
                    {"tag": "div", "text": {"content": f"**会话**: {session_id}", "tag": "lark_md"}},
                    {"tag": "div", "text": {"content": f"**判定理由**: {redact(result.reason or '')[:100]}", "tag": "lark_md"}},
                    {"tag": "hr"},
                    {"tag": "div", "text": {"content": f"**输入预览(已脱敏)**: {preview}", "tag": "lark_md"}},
                    {"tag": "div", "text": {"content": "**处置**: 已拒绝响应，未调用大模型生成回答。", "tag": "lark_md"}}
                ]
            }
        }

        response = requests.post(self._alert_webhook, json=card, timeout=5)
        if response.status_code == 200:
            logger.info(f"提示词攻击告警已发送: session={session_id}, type={result.attack_type}")
        else:
            logger.warning(f"提示词攻击告警发送失败: status={response.status_code}")


# ============ 进程级单例 ============
_guard: "PromptGuard" = PromptGuard()


def get_guard() -> "PromptGuard":
    """获取进程级提示词防护单例"""
    return _guard


def configure_guard(enabled: bool = True, alert_webhook: Optional[str] = None) -> None:
    """启动时调用一次：配置提示词防护开关与告警 webhook"""
    _guard.configure(enabled=enabled, alert_webhook=alert_webhook)
    logger.info(
        f"提示词防护已配置: enabled={enabled}, alert_webhook={'已设置' if alert_webhook else '未设置'}"
    )


def guard_check(provider, text: str, session_id: str = "unknown", source: str = "unknown"):
    """便捷函数：用 AI 判断输入是否攻击，命中时上报告警并返回结果，否则返回 None"""
    return _guard.check(provider, text, session_id=session_id, source=source)
