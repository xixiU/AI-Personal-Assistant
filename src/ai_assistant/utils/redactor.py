"""
敏感信息脱敏工具

用于在【机器人对外回复】发送给用户之前，遮蔽可能泄露的真实敏感信息，
避免代码/文档/配置中的账号密码、IP、AK/SK、Token、私钥等被原文带出。

脱敏采用双层策略：
1. 已知密钥精确遮蔽：从配置中读取到的真实密钥值（API Key、密码、飞书
   app_secret 等），只要在回复文本中出现就整段替换（准确率最高、零误伤）。
2. 模式遮蔽兜底：通过正则匹配常见的敏感信息形态（IPv4、AK/SK、JWT、
   私钥块、`password=xxx` 赋值、URL 内嵌凭据等），即使这些值不在配置里
   （例如 AI 从业务代码/文档里读出来的），也能拦截。

设计为进程级单例，启动时调用 register_secrets_from_config(config) 注册一次，
后续所有回复路径调用 get_redactor().redact(text) 即可。
"""
import re
from typing import Any, List, Set
from loguru import logger

# 配置中"键名"匹配到这些词时，其字符串值会被登记为需精确遮蔽的已知密钥
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|app[_-]?secret|private[_-]?key|encrypt[_-]?key|"
    r"auth[_-]?username|webhook|\bak\b|\bsk\b)"
)

# 登记为已知密钥的最小长度（过短的值如空串、常见短词不登记，避免大面积误伤）
_MIN_SECRET_LEN = 5


class Redactor:
    """敏感信息脱敏器（进程级单例，见 get_redactor）"""

    def __init__(self) -> None:
        # 已登记的密钥，按长度降序存放（先替换长的，避免子串重叠导致的残留）
        self._secrets: List[str] = []
        self._secret_set: Set[str] = set()

    def register_secret(self, value: Any) -> None:
        """登记一个需要精确遮蔽的密钥值（非字符串或过短会被忽略）"""
        if not isinstance(value, str):
            return
        v = value.strip()
        if len(v) < _MIN_SECRET_LEN or v in self._secret_set:
            return
        self._secret_set.add(v)
        self._secrets.append(v)
        # 按长度降序，保证替换时长密钥优先
        self._secrets.sort(key=len, reverse=True)

    def register_from_obj(self, obj: Any, _depth: int = 0) -> None:
        """
        递归遍历任意配置对象（dataclass / dict / list），
        把"键名命中敏感词"的字符串值登记为已知密钥。

        自动覆盖 Config、RepositoryConfig、adapters 列表等，
        新增敏感字段（只要键名含 password/secret/token 等）无需改此处。
        """
        if _depth > 8 or obj is None:
            return

        if isinstance(obj, dict):
            items = obj.items()
        elif hasattr(obj, "__dict__"):
            items = vars(obj).items()
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                self.register_from_obj(item, _depth + 1)
            return
        else:
            return

        for key, val in items:
            if isinstance(val, str):
                if _SENSITIVE_KEY_RE.search(str(key)):
                    self.register_secret(val)
            elif isinstance(val, (dict, list, tuple)) or hasattr(val, "__dict__"):
                self.register_from_obj(val, _depth + 1)

    @property
    def secret_count(self) -> int:
        """已登记的已知密钥数量（仅用于日志，不暴露具体值）"""
        return len(self._secrets)

    def redact(self, text: str) -> str:
        """
        对文本执行脱敏：先精确遮蔽已登记的已知密钥，再用正则兜底遮蔽
        常见敏感形态。返回脱敏后的文本；非字符串或空串原样返回。
        """
        if not text or not isinstance(text, str):
            return text

        # 第一层：已知密钥精确遮蔽（长度降序，避免子串残留）
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "【已隐藏】")

        # 第二层：模式兜底遮蔽（顺序敏感，结构化/高置信度的先处理）
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)

        return text


# ============ 模式兜底规则 ============
# 不会被视为敏感的 IP（本地回环 / 通配 / 文档示例网段）
_IP_WHITELIST = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "1.1.1.1", "8.8.8.8"}


def _mask_ip(m: "re.Match") -> str:
    ip = m.group(0)
    if ip in _IP_WHITELIST:
        return ip
    # 每段必须 0-255，避免误伤版本号等（如 300.1.2.3 不算 IP）
    if all(0 <= int(o) <= 255 for o in ip.split(".")):
        return "【已隐藏:IP】"
    return ip


# 敏感赋值的键名（key: value / key=value / key="value" 形态）
_ASSIGN_KEYS = (
    r"password|passwd|pwd|secret|secret[_-]?key|api[_-]?key|access[_-]?key|"
    r"app[_-]?secret|private[_-]?key|encrypt[_-]?key|token|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|\bak\b|\bsk\b"
)

_PATTERNS = [
    # PEM 私钥 / 证书块（多行，最高优先）
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL),
     "【已隐藏:私钥】"),
    # AWS Access Key ID（AKIA/ASIA + 16 位）
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
     "【已隐藏:AK】"),
    # JWT（三段 base64url，点分隔）
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
     "【已隐藏:Token】"),
    # HTTP Authorization: Bearer/Basic <token>
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{10,}"),
     r"\1 【已隐藏:Token】"),
    # URL 内嵌凭据 scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s:/@]+@"),
     r"\1【已隐藏:凭据】@"),
    # key = value / key: value / key="value" 形态的敏感赋值
    # 值中排除 【 】，避免把前面规则已生成的占位符（如【已隐藏:Token】）二次替换
    (re.compile(
        r"(?i)\b(" + _ASSIGN_KEYS + r")(\s*[:=]\s*)([\"']?)([^\s\"',;【】]{3,})(\3)"),
     r"\1\2\3【已隐藏】\5"),
    # 中文标签 / 空格分隔的凭据（如「密码 Db@Pass2024」「账号：admin123」）
    # 值限定为 ASCII 凭据样式且 6 字符以上，避免误伤「密码错误」「密码是多少」
    # 这类纯中文叙述；同时排除【】保证对已脱敏文本幂等。
    (re.compile(
        r"(密码|口令|密钥|秘钥|账号|帐号|用户名|"
        r"(?i:password|passwd|pwd|token|api[_-]?key))"
        r"(\s*(?:是|为|:|：|=)?\s*)"
        r"([A-Za-z0-9][A-Za-z0-9._@#$%^&*!+\-/?~]{5,})"),
     r"\1\2【已隐藏】"),
    # IPv4（放最后，配合白名单和逐段校验）
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
     _mask_ip),
]


# ============ 进程级单例 ============
_redactor: "Redactor" = Redactor()


def get_redactor() -> "Redactor":
    """获取进程级脱敏器单例"""
    return _redactor


def register_secrets_from_config(config: Any) -> None:
    """
    启动时调用一次：从配置对象递归登记所有已知密钥。
    仅记录数量到日志，绝不打印具体密钥值。
    """
    _redactor.register_from_obj(config)
    logger.info(f"脱敏器已登记 {_redactor.secret_count} 条已知密钥")


def redact(text: str) -> str:
    """便捷函数：对文本执行脱敏（等价于 get_redactor().redact(text)）"""
    return _redactor.redact(text)
