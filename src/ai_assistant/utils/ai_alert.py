"""
AI 调用失败告警（输出侧运维告警）

与 prompt_guard.py（输入侧攻击告警）互补：prompt_guard 关注"用户输入有问题"，
本模块关注"AI 服务本身出问题"——余额/额度耗尽、API Key 失效、限流、超时、
连接失败、5xx 等。这类故障用户只会看到一句"❌ AI 服务调用失败"，
如果不主动告警，维护者无法及时发现（典型场景：token 额度耗尽后所有提问全部失败）。

收口位置：AIProvider.call() 是 feishu/wechat/web 三个来源、
Agentic/Operations/RAG 三种模式的唯一公共出口，所以只在那里上报一次，
不需要在每个 Provider 的每个 except 分支重复接线。

设计要点：
- 进程级单例，启动时调用 configure_ai_alert(webhook) 配置一次。
- 复用统一告警通道 alert.webhook，不新增配置项。
- 告警内容先经 redact() 脱敏，避免告警本身把 API Key、base_url 里的凭据带出去。
- 异步发送（守护线程），不给已经失败的请求再叠加 5 秒网络等待。
- 同类错误在抑制窗口内只发一条，下一条会带上"期间抑制 N 次"，
  避免额度耗尽这类持续性故障把告警群刷爆。
"""
import re
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from loguru import logger

# 同类错误的告警抑制窗口（秒）。额度耗尽/限流会对每一次提问都报错，
# 不做抑制会在几分钟内产生上百条告警。
_SUPPRESS_WINDOW_SECONDS = 300

# 错误类型 → (中文标签, 卡片颜色, 处置建议)
# red 表示需要人工介入才能恢复，orange 表示大概率可自愈。
_ERROR_PROFILES: Dict[str, Tuple[str, str, str]] = {
    "quota": ("额度/余额不足", "red", "需要人工充值或调整额度，期间所有提问都会失败。"),
    "auth": ("API Key 认证失败", "red", "检查配置中的 API Key 是否过期或被撤销。"),
    "rate_limit": ("请求被限流", "orange", "短时并发过高，通常可自行恢复；持续出现需调低并发或提升配额。"),
    "server": ("上游服务 5xx", "orange", "AI 服务端故障，通常可自行恢复；持续出现需联系服务方。"),
    "timeout": ("请求超时", "orange", "检查网络与上游延迟，必要时调大 ai.timeout。"),
    "connection": ("连接失败", "orange", "检查网络、代理与 base_url 可达性。"),
    "api": ("API 返回错误", "red", "查看错误详情判断是参数问题还是服务方限制。"),
    "exception": ("内部异常", "red", "属于代码或依赖问题，需查看日志堆栈定位。"),
}

_DEFAULT_PROFILE = ("未知错误", "red", "查看日志中的完整堆栈与响应体。")


def _signature(error_type: str, detail: str) -> str:
    """
    生成用于抑制判断的错误指纹。

    把数字统一替换掉，让"剩余额度 1.74/需要 2.12"这类每次都变的数值
    不会绕过抑制，从而把同一个故障识别为同一类。
    """
    normalized = re.sub(r"\d+", "N", detail or "")[:120]
    return f"{error_type}|{normalized}"


class AIErrorAlerter:
    """AI 调用失败告警器（进程级单例，见 get_ai_alerter）"""

    def __init__(self) -> None:
        self.enabled: bool = True
        self._webhook: Optional[str] = None
        self._lock = threading.Lock()
        # signature -> (上次发送时间戳, 窗口内被抑制的次数)
        self._last_sent: Dict[str, Tuple[float, int]] = {}

    def configure(self, enabled: bool = True, webhook: Optional[str] = None) -> None:
        """配置开关与告警 webhook"""
        self.enabled = enabled
        self._webhook = webhook or None

    def report(
        self,
        error_type: str,
        detail: str,
        provider: str = "unknown",
        model: str = "unknown",
        mode: str = "unknown",
        session_id: str = "unknown",
        source: str = "unknown",
        query_preview: str = "",
    ) -> bool:
        """
        上报一次 AI 调用失败。

        Args:
            error_type: 错误类型（quota/auth/rate_limit/server/timeout/connection/api/exception）
            detail: 错误详情原文（会脱敏后再发出）
            provider: Provider 类名
            model: 模型名
            mode: 调用模式（agentic/operations/rag/unknown）
            session_id: 会话 ID
            source: 提问来源（feishu/wechat/web）
            query_preview: 用户提问摘要（会脱敏后再发出）

        Returns:
            True 表示已提交发送，False 表示未启用/未配置 webhook/被抑制
        """
        if not self.enabled or not self._webhook:
            return False

        sig = _signature(error_type, detail)
        now = time.time()
        with self._lock:
            last_ts, suppressed = self._last_sent.get(sig, (0.0, 0))
            if now - last_ts < _SUPPRESS_WINDOW_SECONDS:
                self._last_sent[sig] = (last_ts, suppressed + 1)
                logger.debug(
                    f"AI 错误告警已抑制（{_SUPPRESS_WINDOW_SECONDS}s 内同类第 {suppressed + 1} 次）: "
                    f"type={error_type}"
                )
                return False
            self._last_sent[sig] = (now, 0)
            suppressed_count = suppressed

        # 发送放到守护线程：本次请求已经失败了，不该再为告警多等 5 秒
        threading.Thread(
            target=self._send_safe,
            args=(error_type, detail, provider, model, mode, session_id, source, query_preview, suppressed_count),
            daemon=True,
            name="ai-error-alert",
        ).start()
        return True

    def _send_safe(self, *args) -> None:
        """线程入口：告警发送本身的任何异常都不允许向外扩散"""
        try:
            self._send(*args)
        except Exception as e:
            logger.warning(f"AI 错误告警发送异常: {e}")

    def _send(
        self,
        error_type: str,
        detail: str,
        provider: str,
        model: str,
        mode: str,
        session_id: str,
        source: str,
        query_preview: str,
        suppressed_count: int,
    ) -> None:
        """构造飞书告警卡片并发送"""
        import requests
        from ai_assistant.utils.redactor import redact

        label, color, advice = _ERROR_PROFILES.get(error_type, _DEFAULT_PROFILE)
        safe_detail = redact(detail or "")[:500]
        safe_query = redact(query_preview or "")[:150]

        elements = [
            {"tag": "div", "text": {"content": f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "tag": "lark_md"}},
            {"tag": "div", "text": {"content": f"**错误类型**: {label} (`{error_type}`)", "tag": "lark_md"}},
            {"tag": "div", "text": {"content": f"**Provider**: {provider} / {model}", "tag": "lark_md"}},
            {"tag": "div", "text": {"content": f"**调用模式**: {mode}", "tag": "lark_md"}},
            {"tag": "div", "text": {"content": f"**来源**: {source}", "tag": "lark_md"}},
            {"tag": "div", "text": {"content": f"**会话**: {session_id}", "tag": "lark_md"}},
        ]
        if safe_query:
            elements.append({"tag": "div", "text": {"content": f"**提问摘要**: {safe_query}", "tag": "lark_md"}})
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"content": f"**错误详情(已脱敏)**:\n{safe_detail}", "tag": "lark_md"}})
        elements.append({"tag": "div", "text": {"content": f"**处置建议**: {advice}", "tag": "lark_md"}})
        elements.append({
            "tag": "div",
            "text": {"content": "**影响**: 本次提问已向用户返回失败提示，未生成有效回答。", "tag": "lark_md"}
        })
        if suppressed_count > 0:
            elements.append({
                "tag": "div",
                "text": {
                    "content": f"**注**: 上一条告警后 {_SUPPRESS_WINDOW_SECONDS}s 内同类错误已抑制 {suppressed_count} 次。",
                    "tag": "lark_md",
                }
            })

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": color,
                    "title": {"content": "🚨 AI 服务调用失败告警", "tag": "plain_text"},
                },
                "elements": elements,
            },
        }

        response = requests.post(self._webhook, json=card, timeout=5)
        if response.status_code == 200:
            logger.info(f"AI 错误告警已发送: type={error_type}, provider={provider}, session={session_id}")
        else:
            logger.warning(f"AI 错误告警发送失败: status={response.status_code}")


# ============ 进程级单例 ============
_alerter: "AIErrorAlerter" = AIErrorAlerter()


def get_ai_alerter() -> "AIErrorAlerter":
    """获取进程级 AI 错误告警单例"""
    return _alerter


def configure_ai_alert(enabled: bool = True, webhook: Optional[str] = None) -> None:
    """启动时调用一次：配置 AI 错误告警开关与 webhook"""
    _alerter.configure(enabled=enabled, webhook=webhook)
    logger.info(
        f"AI 错误告警已配置: enabled={enabled}, webhook={'已设置' if webhook else '未设置'}"
    )


def report_ai_error(
    error_type: str,
    detail: str,
    provider: str = "unknown",
    model: str = "unknown",
    mode: str = "unknown",
    session_id: str = "unknown",
    source: str = "unknown",
    query_preview: str = "",
) -> bool:
    """便捷函数：上报一次 AI 调用失败告警（异常不外抛）"""
    try:
        return _alerter.report(
            error_type=error_type,
            detail=detail,
            provider=provider,
            model=model,
            mode=mode,
            session_id=session_id,
            source=source,
            query_preview=query_preview,
        )
    except Exception as e:
        logger.warning(f"AI 错误告警上报失败（不影响主流程）: {e}")
        return False
