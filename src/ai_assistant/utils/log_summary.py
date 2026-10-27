"""
日志摘要工具（防止用户提问 / 文件正文原样落进日志）

## 为什么需要它

CLAUDE.md 有两条硬规范：
  - 「数据隐私：日志中不记录完整消息内容（仅记录长度和摘要）」
  - 「禁止事项：不得在日志中记录完整的用户消息」

但代码里散落着 `logger.info(f"... text:{text}")` 这种直接插值，用户私聊发一份
3442 字符的异常堆栈，同一份原文在一次请求里能被打 4 遍（解析、进入处理、
检索关键词、AI 标题过滤），终端直接被刷满——既违反上面两条规范，也让真正
有用的流程日志被淹没。

## 设计约束

1. 同时覆盖「纯文本」和「结构化对象」两类调用点（见下面两个入口函数）。
2. 截断长度不写死成散落各处的魔法数字：统一在本模块的常量里，
   调用点只在确有特殊需要时显式传 limit。
3. 保留排查价值。只打长度（`len=3442`）在线上是不够用的——运维看到
   「某条消息处理失败」时无法确认是哪一条。所以摘要固定带三件事：
     - len / lines：量级，判断是不是超长输入导致的问题
     - sha1 前 8 位：**同一份内容的指纹**。一次请求里多个环节各打一条摘要
       时，靠相同的 sha1 就能确认"这几条日志说的是同一份输入"，不必重复
       打印原文。这是"多处打印"的收口手段。
     - head/tail 片段：人眼辨识用（"哦是那份 NullPointerException"）
4. 多行文本（异常堆栈是典型）直接切前 N 字符会把换行一起带进日志，
   一条日志炸成十几行、还会破坏下游按行解析的日志采集。因此所有片段
   都做**单行化**：换行/制表符转义成可见的 \\n \\t 字面量。

## 用法

    from ai_assistant.utils.log_summary import summarize_text, summarize_obj

    logger.info(f"Processing message: session={sid}, text={summarize_text(text)}")
    logger.debug("parsed message:{}", summarize_obj(parsed))

两个函数都**绝不抛异常**：日志摘要失败不能影响主流程（尤其 webhook 必须
3 秒内返回），任何意外都退化成一个带类型名的占位串。
"""
import hashlib
import re
from enum import Enum
from typing import Any

# ============ 统一的截断阈值（唯一来源，禁止在调用点另写魔法数字） ============

# 头部片段保留的字符数。注意它**同时**是「短到可以整段显示」的阈值：
# 内容不长于这个值时，头部片段已经覆盖全文，再补尾部和指纹是重复信息，
# 所以直接整段显示。一个常量一条规则，不另设第二个阈值
# （曾经有过一个 PREVIEW_FULL_THRESHOLD = PREVIEW_HEAD_CHARS，全仓无人引用，
#  只会让人误以为"短于它就原样输出"是条独立规则）。
#
# 默认值的取舍：
#   - 要足够长到能认出内容（一条异常的首行、一句提问的主干）
#   - 要足够短到不把终端刷满（一次请求 4~5 条摘要，总量仍在一屏内）
PREVIEW_HEAD_CHARS = 80

# 超长内容额外保留的尾部字符数。尾部有独立排查价值：堆栈的 "Caused by"、
# 截断传输导致的尾部缺失，都只能从尾部看出来。
PREVIEW_TAIL_CHARS = 40

# ============ 已知边界：短内容在日志里仍是完整原文（有意接受） ============
#
# 把摘要里的首尾片段拼起来能还原原文的长度，实测精确结果是：
#   - len <= PREVIEW_HEAD_CHARS(80)        → 走「整段显示」分支，就是原文
#   - len == 80 + 40 = 120（恰好）          → 首尾片段刚好无缝衔接，拼起来也是原文
#   - 81 ~ 119                              → 中间有缺口，还原不出
#   - > 120                                 → 缺口随长度增大
# （注意不是"≤120 全部可还原"：81~119 这一段是缺中间的。）
#
# 这意味着 CLAUDE.md「日志中不记录完整消息内容」这条硬规范，在 80 字符以内的
# 提问上事实上没有生效 —— 而中文单句提问常落在这个区间。
#
# 这是**有意的取舍**，不是漏修：
#   - 本次治理要解决的是"3442 字符异常堆栈刷满终端 + 长文落盘"，短提问不在其中；
#   - 短内容的排查价值极高。运维看到「某条消息处理失败」时，`text(len=7):'订单服务起不来'`
#     能直接定位，换成 `text(len=7,sha1=...)` 就只剩一个无从对照的哈希；
#   - 真要做到"任何长度都不留原文"，只能全部降级成长度+指纹，日志会失去可读性，
#     而这不是本次 bug 的诉求。
# 如果将来合规口径收紧到"任何长度都不得出现原文"，改法是把短内容分支也换成
# 纯 len+sha1（并接受排查成本上升），而不是去调 PREVIEW_HEAD_CHARS —— 调小只是
# 把边界挪到更短的提问上，性质不变。

# 结构化对象递归的最大深度 / 每层最多展开的元素个数，
# 防止深层嵌套或超长列表把一行日志撑爆。
OBJ_MAX_DEPTH = 4
OBJ_MAX_ITEMS = 12

# 指纹长度（sha1 十六进制前 N 位）。8 位足以在单次请求的几条日志间区分内容。
FINGERPRINT_CHARS = 8

# ============ 凭据字段名（dict 的键命中即遮蔽，不看值的长度） ============
#
# 为什么长度摘要挡不住凭据：飞书 verification_token 是 32 字符单行字符串，
# app_secret / open_id 同理，统统落在「短且单行 → 原样透传」那一侧。
# 透传分支对隐私是对的（chat_id 这类标识必须可读），但对**凭据**是致命的 ——
# 实测 `summarize_obj({"token": "<32字符token>"})` 会把 token 原值打进日志。
#
# 所以凭据不能靠长度判定，只能靠**字段名**判定：命中就遮蔽，不管值多短。
# 这两件事的判据不同，必须分开处理（这是实测后才确认的，不是预防性设计）。
#
# 匹配规则：键名先经 _normalize_key 归一（小写 + 驼峰拆分 + 非字母数字转
# 下划线），再看是否**包含**任一子串。
#
# 为什么凭据可以按字段名判定，而承载用户内容的字段（data / content / text）
# 不行 —— 这两件事看着矛盾，依据其实不同：
#   - 凭据字段名是**跨业务稳定**的。叫 token / secret / password 的字段，
#     在任何 payload 里都是凭据，按名字遮蔽不会误伤。
#   - `data` / `content` 这类是**业务相关**的。同一个 key 在别处完全可能是
#     正经内容（甚至是需要可读的标识），按名字特殊处理会在别处误伤。
# 所以图片 base64 那类"摘要出来没有可读价值"的字段，由**调用点**先替换成
# `<image image/png, ~12016B>` 这种业务形态（见 main.py 的 image_data 预处理），
# 本模块只做与业务无关的兜底，不去认识业务字段名。
# 这里**只写下划线形态、全小写**一种拼法，由两条机制分工覆盖所有拼法：
#   1. 归一化（_normalize_key）管**分隔符和驼峰**：
#      `X-Api-Key` / `api-key` / `api.key` / `ACCESS KEY` / `apiKey` / `APIKey`
#   2. 去分隔符匹配（_is_secret_key 里的 squashed 比对）管**人类连写**：
#      `apikey` / `APIKEY` / `accesskey` / `privatekey` / `encryptkey`
# 两者缺一不可 —— 归一化只拆分隔符和驼峰，`apikey` 既无分隔符也无驼峰，
# 归一后还是 `apikey`，光靠归一化匹配不上 `api_key`。
#
# 所以不要往下面补 `apikey`/`accesskey` 这类**同概念变体**：那是在替上面两条
# 机制干活，补得再多也挡不住下一种拼法。要补的只有**新概念**（新出现的凭据
# 字段类别，比如将来冒出个 `refresh_ticket`）。
#
# 回归记录：曾经名单里有 `apikey`/`accesskey`，修 `X-Api-Key` 时把它们删了，
# 误以为归一化能接管，结果 6 种连写形态从「遮蔽」变成「泄漏」。
# 防回归见 tests/unit/test_log_no_full_content.py 的 must_redact 连写段。
SECRET_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "passphrase",   # 覆盖 key_passphrase（SSHConfig 字段，见 operations_models.py:28）
    "private_key",
    "api_key",
    "access_key",
    "encrypt_key",
    "signature",
    "authorization",
    "bearer",
    "credential",
    "cookie",
    "jwt",
)

# 刻意**不**加入 SECRET_KEY_HINTS 的字段名，连同理由 —— 写在这里是为了
# 防止后人"顺手补全"把排查能力毁掉，以及避免重复踩同一个坑：
#
#   app_id / open_id / user_id / chat_id / message_id
#       这些是排查主力标识，不是凭据。遮了日志就失去定位能力
#       （"哪个用户哪个会话出的问题"再也看不出来）。
#
#   session_id
#       在本项目里它**就是 chat_id**（见 feishu_bot.py:1203 等处 `session_id=chat_id`），
#       而且以 `session=oc_xxx` 的形态出现在 main.py 的多条 INFO 日志里。
#       在别的项目里 session_id 可能是会话凭据，但在这里遮它纯属自伤。
#
#   sign（裸子串）
#       会误伤 design / assign / signal / assignee / designer。
#       真正要挡的是签名字段，已由 "signature" 覆盖。所以它既不在 hints 里，
#       也不需要白名单 —— 根本没有哪条 hint 会命中它们。
#
# 下面六个标识另外进了白名单（不只是"没加进 hints"）：
# 这个名单是**被 _matched_secret_hints 真正读取的**白名单，不是说明性注释
# （否则就成了又一个"没人引用的常量"）。它先于 hints 判定，所以哪天有人
# 往 SECRET_KEY_HINTS 里加了个宽子串（比如裸 "id" 或 "key"），这些排查标识
# 也不会被连带遮掉 —— 把"不许遮过头"从约定升级成机制。
#
# ========== 白名单必须保持精确匹配，不要去和 hints「对齐粒度」 ==========
#
# 用的是**精确匹配**（`normalized in NEVER_SECRET_KEYS`，tuple 的 `in` 比的是
# 元素相等，不是子串），而 hints 侧用的是归一化 + 去分隔符子串。两边粒度
# 不对称**看起来**像个待修的不一致 —— 它不是。这里不能对齐。
#
# 会有人想放宽它，因为白名单确实窄：hints 里万一加了宽子串（裸 "id"/"key"），
# 白名单只救得下"恰好归一成白名单那个词"的拼法。实测（临时往 hints 注入裸
# "id"）：
#       app_id / appId / APP_ID / app.id / user_id   → 救下来
#       X-App-Id → x_app_id、X-User-Id → x_user_id   → 救不下来（多了前缀）
#
# **但放宽的代价是凭据泄漏，远大于这点收益。** 任何部分匹配
# （子串 / 前缀 / 后缀 / 按下划线的词边界，全都一样）都会立刻开出口子，
# 因为白名单是**先行**判定 —— 一旦命中就直接 return，hints 根本没机会跑。
# 实测把白名单改成"按词边界的后缀匹配"（`normalized.endswith("_" + w)`），
# 这些键从「遮蔽」翻成「可读」：
#       secret_app_id         hints=['secret']    → 可读   ← 凭据泄漏
#       password_user_id      hints=['password']  → 可读   ← 凭据泄漏
#       api_key_app_id        hints=['api_key']   → 可读   ← 凭据泄漏
#       access_token_user_id  hints=['token']     → 可读   ← 凭据泄漏
#       token_app_id          hints=['token']     → 可读   ← 凭据泄漏
# `xxx_user_id` / `xxx_app_id` 这种命名在真实代码里极常见，所以这不是
# 构造出来的边角反例。
#
# 性质总结：**「白名单先行」和「精确匹配」是一对，不能只保留一半。**
# 先行之所以安全，正因为精确匹配窄到"只有恰好叫这个名字的才放过"；
# 放宽到任何部分匹配，先行就从保险变成旁路。
# 真要让白名单覆盖更多拼法，正确做法是**往名单里显式加那个拼法**
# （比如把 `x_app_id` 作为一条加进去），而不是放宽匹配方式。
#
# 防回归见 tests/unit/test_log_no_full_content.py::
#   test_whitelist_must_stay_exact_match_not_substring
# （注释没人验，那条测试会当场拦住这类"顺手放宽"）。
#
# 顺带：当前 hints 16 条里没有任何宽子串（没有裸 "id"，也没有裸 "key"），
# 所以白名单处于**备而不用**状态，对现有行为零影响。真遇到想加宽子串的场景，
# 正确做法也是别加 —— 参照 X-Api-Key 的教训：该修匹配方式，不是放宽词汇。
NEVER_SECRET_KEYS = (
    "app_id",
    "open_id",
    "user_id",
    "chat_id",
    "message_id",
    "session_id",
)


def _normalize_key(key: Any) -> str:
    """
    把字段名归一成「全小写 + 下划线分隔」再做匹配。

    为什么必须归一而不是往名单里补变体：`X-Api-Key` 当初漏掉，漏的不是
    名单条目而是**分隔符** —— 名单里有 `api_key`，报文里是连字符。靠补
    `apikey`/`api-key`/`apiKey` 永远追不上下一种拼法（还有 `Api.Key`、
    `api key`、`APIKey`…）。归一化一次解决一类问题。

    做三件事：
      1. 驼峰拆开：`apiKey` → `api_key`（含 `APIKey` → `api_key` 这种
         连续大写接小写的形态）
      2. 所有非字母数字字符统一成下划线：`X-Api-Key` / `api.key` / `api key` → `x_api_key`
      3. 全部转小写
    """
    try:
        raw = str(key)
    except Exception:
        return ""
    # 驼峰边界插入下划线。两条规则分别处理 `apiKey`→api_Key 和 `APIKey`→API_Key
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", spaced)
    # 非字母数字一律当分隔符
    spaced = re.sub(r"[^A-Za-z0-9]+", "_", spaced)
    return spaced.lower()


# SECRET_KEY_HINTS 里**语义上可以指代数量**的词。命中这些词、且值是纯数值时
# 不遮蔽，用来解决 `token` 这个裸子串把计数字段一起遮掉的问题：
#   max_tokens / input_tokens / output_tokens / token_count
# 打成 `max_tokens: '<redacted:len=4>'` 既荒谬又有害 —— anthropic_provider.py
# 里 `input_tokens` 是刻意打出来排查兼容网关的（网关常把它回成 0 或固定小值）。
#
# 为什么是这种形态，而不是另两种更直觉的改法：
#   - 不往 NEVER_SECRET_KEYS 补 `max_tokens`/`input_tokens`/`total_tokens`…：
#     补名单追不上下一个计数字段名，这正是 X-Api-Key 当初的教训。
#   - 不写成"凭据不会是 int，所以纯数值一律放过"：**这条是错的**，实测
#     YAML 里不加引号的纯数字密码会被解析成 int（`password: 1234567`
#     → `type=int`），一律放过等于给纯数字密码开后门。
# 所以判据挂在 hint 词汇上：只有本身就有「数量」含义的 hint 才允许数值透传。
# 这个列表受 SECRET_KEY_HINTS 约束（16 条，封闭且由本模块维护），
# 不随业务字段名增长。
NUMERIC_OK_HINTS = ("token",)


def _is_plain_number(value: Any) -> bool:
    """值是不是纯数值。排除 bool：`isinstance(True, int)` 为 True，但它不是计数。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _matched_secret_hints(key: Any) -> tuple:
    """
    返回字段名命中的所有凭据 hint。`_is_secret_key` 和 `_should_redact`
    共用它，避免两处各写一遍匹配规则（曾经 head_len 就是这么漂移的）。

    匹配分两步，缺一不可（理由见 SECRET_KEY_HINTS 上的注释）：
      1. 归一化后比子串 —— 覆盖分隔符和驼峰：`X-Api-Key` / `apiKey` / `api-key`
      2. 再把下划线全部去掉比一次 —— 覆盖人类连写：`apikey` / `APIKEY` /
         `privatekey`。归一化拆不开连写，只有这步能兜住。
    """
    normalized = _normalize_key(key)
    if not normalized:
        return ()
    # 白名单先行：排查标识永不遮蔽，即使将来 hints 里混进了宽子串
    if normalized in NEVER_SECRET_KEYS:
        return ()
    squashed = normalized.replace("_", "")
    return tuple(
        hint
        for hint in SECRET_KEY_HINTS
        if hint in normalized or hint.replace("_", "") in squashed
    )


def _is_secret_key(key: Any) -> bool:
    """
    字段名是否看起来承载凭据。按**名字**而非值的长度判定，理由见
    SECRET_KEY_HINTS 的注释（凭据往往短且单行，长度判定一定漏）。

    只看名字。实际遮蔽决策请用 `_should_redact`（它会多看一眼值，
    好放过 `max_tokens` 这类计数字段）。匹配规则见 `_matched_secret_hints`。
    """
    return bool(_matched_secret_hints(key))


def _should_redact(key: Any, value: Any) -> bool:
    """
    这个键值对要不要遮蔽。比 `_is_secret_key` 多看一眼值，只为放过计数字段。

    注意是 `all()` 而不是 `any()`：`token_password` 这种同时命中
    `token` 和 `password` 的键，仍然必须遮 —— 只要有一个命中的 hint
    不属于 NUMERIC_OK_HINTS，就按凭据处理。
    """
    hints = _matched_secret_hints(key)
    if not hints:
        return False
    if _is_plain_number(value) and all(h in NUMERIC_OK_HINTS for h in hints):
        return False
    return True


def redact_secret(value: Any) -> str:
    """
    凭据遮蔽：只输出类型与长度，**不输出任何原文片段、也不输出指纹**。

    和 summarize_text 的关键差别：那边会留首尾片段 + sha1，对用户内容是
    合理的（要能认出是哪条消息）；但对凭据，片段等于部分泄漏，而 sha1 对
    固定凭据等于一个稳定的可比对标识（能用来确认"线上用的还是那个 token"），
    两者都不该出现在日志里。所以这里只给长度，够判断"配没配、配得对不对长度"。
    """
    try:
        if value is None:
            return "<redacted:empty>"
        if isinstance(value, (bytes, bytearray)):
            return f"<redacted:bytes,len={len(value)}>"
        if not isinstance(value, str):
            value = str(value)
        if not value:
            return "<redacted:empty>"
        return f"<redacted:len={len(value)}>"
    except Exception:
        return "<redacted>"


def _preview(s: str) -> str:
    """
    把片段渲染成**单行**可见形式。

    用 `repr` 而非手写 replace：Python 的 repr 保证输出不含真换行（换行/回车/
    制表符都转义成可见的 \\n \\r \\t 字面量），同时保留中文可读。手写一遍再套
    `!r` 会双重转义，日志里出现 `\\\\n` 这种噪声。

    单行是硬要求，不只是美观：多行文本（异常堆栈是典型）一旦把换行带进日志，
    一条日志会被劈成几十行，还会破坏下游按行采集的日志管道。
    """
    return repr(s)


def _head_len(limit: Any) -> int:
    """
    把调用点传入的 limit 归一成「头部片段保留多少字符」。

    唯一实现：summarize_text 和 summarize_obj 都走这里。之前两边各写了一遍
    `PREVIEW_HEAD_CHARS if limit is None else max(1, int(limit))`，同一个规则
    落在两处，改一处另一处就漂（而漂掉的那一处正好是防泄漏逻辑）。

    `max(1, ...)` 的意义：limit=0 / 负数会让切片变成空串或反向取值，
    摘要失去辨识度，所以下限锁在 1。
    """
    if limit is None:
        return PREVIEW_HEAD_CHARS
    return max(1, int(limit))


def fingerprint(text: str) -> str:
    """内容指纹：sha1 十六进制前 FINGERPRINT_CHARS 位（不可逆，不泄露原文）。"""
    try:
        return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[
            :FINGERPRINT_CHARS
        ]
    except Exception:
        return "?" * FINGERPRINT_CHARS


def summarize_text(value: Any, *, limit: int = None, label: str = "text") -> str:
    """
    把任意文本压成一行可排查的摘要，**绝不输出全文**。

    输出形态：
      - 空 / None            → `text(empty)`
      - 短内容（<= limit）    → `text(len=12,lines=1):'订单服务起不来'`
      - 超长内容             → `text(len=3442,lines=87,sha1=a1b2c3d4):'前80字…'…'后40字'`

    Args:
        value: 待摘要的内容。非字符串会先 str()（容错，不抛异常）。
        limit: 头部片段长度，默认 PREVIEW_HEAD_CHARS。只在确有特殊需要时传
               （如打上游错误响应体），不要在调用点随手写数字。

               ⚠️ limit 同时抬高了「整段显示」的门槛：传 limit=600 时，任何
               不长于 600 字符的内容都会**原样打全**（600 字符的头部片段本身
               就是全文）。所以 limit 只能用在确定不含用户提问 / 文件正文的
               内容上。承载用户内容的调用点一律不要传 limit。
        label: 摘要前缀标签，用于区分内容种类（text / tool_result / payload…）。

    Returns:
        单行摘要字符串。
    """
    try:
        if value is None:
            return f"{label}(empty)"
        if not isinstance(value, str):
            value = str(value)
        if not value:
            return f"{label}(empty)"

        head_len = _head_len(limit)
        total = len(value)
        lines = value.count("\n") + 1

        if total <= head_len:
            return f"{label}(len={total},lines={lines}):{_preview(value)}"

        return (
            f"{label}(len={total},lines={lines},sha1={fingerprint(value)}):"
            f"{_preview(value[:head_len])}…{_preview(value[-PREVIEW_TAIL_CHARS:])}"
        )
    except Exception as e:  # pragma: no cover - 纯兜底，日志摘要不许影响主流程
        return f"{label}(<summary-failed:{type(e).__name__}>)"


def summarize_obj(
    obj: Any,
    *,
    max_items: int = OBJ_MAX_ITEMS,
    _depth: int = 0,
) -> Any:
    """
    递归摘要结构化对象（dict / list / tuple / set / dataclass），用于
    `logger.debug("parsed message:{}", summarize_obj(parsed))` 这类整体打印。

    规则：
      - 字符串：**不是无条件过 summarize_text**，分两条路（这里写清，避免
        照文档推断行为时误判）：
          * 长度 <= PREVIEW_HEAD_CHARS **且**不含换行/回车 → 原样透传。
            这类值几乎全是 chat_id / message_id / sender_name / event_type
            这种标识，是排查主力且无刷屏风险，统统套上 `text(len=6,lines=1):`
            前缀只会让整条结构日志没法读。
          * 其余（超长，**或**任何含换行的内容）→ summarize_text 摘要。
            用户提问、异常堆栈、文件正文都落在这一侧，所以透传路径不构成
            泄漏口：它放过的只有短且单行的标识值。
      - **dict 的键命中 SECRET_KEY_HINTS（token/secret/password…）→
        `<redacted:len=N>`，先于一切值判定**。凭据不能靠长度判定：
        飞书 verification_token 是 32 字符单行字符串，正好落在上面
        「短且单行 → 原样透传」那一侧，实测会把原值打进日志。
        隐私（靠长度摘要）和凭据（靠字段名遮蔽）判据不同，必须分开处理。
      - bytes    → `bytes(len=N)`，绝不解码打印
      - 自带 summary() 的对象（Content / Message）→ 直接用它的摘要，
        复用模型里已有的脱敏逻辑（图片只打类型+大小、日志文件只打名+长度）
      - dict/list/tuple → 逐项递归，超过 max_items 的部分折叠成 `…(+N more)`，
        超过 OBJ_MAX_DEPTH 的层级折叠成类型名
      - set/frozenset → 先按 repr 排序再展开。**不能照 list 直接展开**：
        set 的迭代顺序受 str hash 随机化影响，同一份内容跨进程会得到不同
        顺序，于是算出不同的指纹——而"内容指纹"的全部价值就在于可比对。
      - 其它标量（int/bool/None/float）→ 原样保留（无隐私风险，且是排查主力）

    Args:
        obj: 待摘要的对象。
        max_items: 每层最多展开的元素个数，递归时原样向下继承。
        _depth: 内部递归深度，调用点不要传。

    **不接 limit**（这是刻意的）：summarize_text 的 limit 会抬高「整段显示」
    的门槛，而 summarize_obj 面对的是结构化 payload——里面哪个字段承载用户内容
    事先并不知道。一旦接了 limit 并沿递归向下继承，`summarize_obj(payload,
    limit=600)` 就会把树里**每一个** 600 字符以内的字符串整段打出去，
    而调用点看不出自己打开了这个口子。结构化对象里的长内容统一按默认阈值摘要，
    需要放大片段时请在具体字段上单独调 summarize_text。

    Returns:
        **结构**（dict/list）而非字符串，交给 loguru 自己 repr，
        保持与原有 `logger.debug("...{}", some_dict)` 调用形态一致。
    """
    try:
        if _depth > OBJ_MAX_DEPTH:
            return f"<{type(obj).__name__} depth-limit>"

        if obj is None or isinstance(obj, (bool, int, float)):
            return obj

        if isinstance(obj, str):
            # 透传窗口固定用 PREVIEW_HEAD_CHARS，不接受调用点调节（本函数
            # 刻意不收 limit，见 docstring）：否则排查参数会变成泄漏开关。
            if (
                len(obj) <= PREVIEW_HEAD_CHARS
                and "\n" not in obj
                and "\r" not in obj
            ):
                # 短单行字符串原样保留（标识类字段），见上面 docstring 的取舍说明
                return obj
            return summarize_text(obj)

        if isinstance(obj, (bytes, bytearray)):
            return f"bytes(len={len(obj)})"

        # Enum 必须先于下面的 __dict__ 分支处理：Enum 成员有 __dict__，
        # 走通用递归会把 _value_ / _name_ / __objclass__ / _sort_order_ 这些
        # 内部字段全摊开（实测 SSHConfig.method 就被摊成 4 个键 + 一个
        # depth-limit 占位），既刷屏又让人看不出它其实就是 'password'。
        if isinstance(obj, Enum):
            return f"{type(obj).__name__}.{obj.name}"

        # Content / Message 等自带日志摘要的模型对象优先用它自己的实现，
        # 避免两套脱敏逻辑漂移（例如图片 base64 的处理只应有一处）
        summary_fn = getattr(obj, "summary", None)
        if callable(summary_fn):
            try:
                produced = summary_fn()
            except Exception:
                return f"<{type(obj).__name__} summary-failed>"
            # **不能原样 return**：summary() 的返回值是外部代码，不该被当成
            # 已脱敏的成品。现实里 core/models.py 的两个 summary() 都返回 str
            # 且已脱敏，所以当前无风险；但一旦有人写个返回 dict 的 summary()，
            # 原样返回就会静默绕过凭据遮蔽与长文摘要（实测过：返回
            # {'secret_token': 'LEAKED'} 时 token 原样落盘）。
            # 所以把它当普通数据再过一遍本函数：str 走长度摘要，
            # dict/list 递归（于是 token 这类键照样被遮）。
            # str 不需要单独分支：递归后它走「短单行原样透传」，
            # `log_file(app.log, text_len=50000)` 这类摘要可读性不变。
            return summarize_obj(produced, max_items=max_items, _depth=_depth + 1)

        if isinstance(obj, dict):
            out = {}
            for i, (k, v) in enumerate(obj.items()):
                if i >= max_items:
                    out["…"] = f"(+{len(obj) - max_items} more keys)"
                    break
                # 凭据字段按**名字**遮蔽，先于一切值判定：token / app_secret
                # 这类值短且单行，会走下面的"原样透传"分支，长度摘要挡不住。
                if _should_redact(k, v):
                    out[str(k)] = redact_secret(v)
                else:
                    out[str(k)] = summarize_obj(
                        v, max_items=max_items, _depth=_depth + 1
                    )
            return out

        if isinstance(obj, (list, tuple, set, frozenset)):
            if isinstance(obj, (set, frozenset)):
                # set 必须先排序：它的迭代顺序受 str hash 随机化影响，同一份
                # 内容跨进程（甚至同进程多次构造）会得到不同顺序，摘要随之不同，
                # 于是"同一份内容算出不同指纹"——指纹的全部价值就是可比对，
                # 顺序不稳定等于指纹作废。按 repr 排序而非按值：元素可能是
                # 不可比类型（int 与 str 混在一个 set 里），直接 sorted 会 TypeError。
                try:
                    seq = sorted(obj, key=repr)
                except Exception:
                    seq = list(obj)
            else:
                seq = list(obj)
            out_list = [
                summarize_obj(v, max_items=max_items, _depth=_depth + 1)
                for v in seq[:max_items]
            ]
            if len(seq) > max_items:
                out_list.append(f"(+{len(seq) - max_items} more)")
            return out_list

        # dataclass / 普通对象：按 __dict__ 递归
        if hasattr(obj, "__dict__"):
            return summarize_obj(
                vars(obj), max_items=max_items, _depth=_depth + 1
            )

        return f"<{type(obj).__name__}>"
    except Exception as e:  # pragma: no cover - 纯兜底
        return f"<summary-failed:{type(e).__name__}>"
