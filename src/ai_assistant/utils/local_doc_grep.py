"""
本地离线文档关键词直查（跳过向量索引）

local_docs 里没有显式写 mode: "index" 的目录不进向量索引，查询时直接用 AI 提取的
关键词在文件内容里逐行 grep，把命中行及前后几行注入 prompt。大 SQL 初始化脚本建
向量索引很慢，而且向量结果常把无关 SQL 顶进 Top5。

噪声控制（AI 提取的关键词里常有"日志""回传"这类泛词）：
- 命中文件数超过扫描文件一半（且多于 MAX_DOCS 个）的关键词视为泛词，不参与选文件、
  不触发取窗口，只在说明行里标出。命中的全是泛词时：若另有关键词一个文件都没命中
  （多半是查无此案号），不返回；否则退回按全部关键词排序。
- 其余关键词按文件频率加权 w = log(N/df) + 1，文件得分 = 命中关键词的 w 之和，
  得分相同再比命中行数。稀有的案号 / 表名比常见词权重高。
- 最多选 MAX_DOCS 个文件；第 2 个起得分须不低于第 1 名的 SELECT_SCORE_RATIO，
  只命中一个弱词的文件进不来。其余命中文件只列文件名。

只依赖标准库 + loguru（不依赖 jieba/chromadb/paramiko），方便单测。
服务端日志只记文件数、行数、耗时，不记关键词和命中正文（关键词可能是案号）。
"""

import heapq
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ai_assistant.utils.text_stream import clip_line, decode_line, is_binary, iter_chunks

MODE_GREP = "grep"
MODE_INDEX = "index"

# 与 FeishuDocManager._load_all_local_docs 的扩展名白名单保持一致
DOC_EXTENSIONS = ('.txt', '.md', '.sql', '.json', '.yaml', '.yml', '.csv')

MIN_KEYWORD_LEN = 2           # 单字关键词会命中几乎全部文件
MAX_KEYWORDS = 10
MAX_DOCS = 3                  # 最多展开的文件数
MAX_OTHER_FILES = 10          # 未展开的命中文件最多列出多少个文件名
CONTEXT_LINES = 3             # 命中行前后各带几行
MAX_DOC_CHARS = 15000         # 单篇上限（与 FeishuDocManager 的单篇截断一致）
MAX_TOTAL_CHARS = 25000       # grep 结果总量上限：50000 总预算的一半留给在线文档
MAX_LINE_CHARS = 500          # 单行最多展示的字符数（以命中位置为中心截取）
MAX_KEPT_HITS = 500           # 第二遍每个文件最多保留的候选命中行（按优先级）
COMMON_DF_RATIO = 0.5         # 命中超过该比例扫描文件的关键词视为泛词
SELECT_SCORE_RATIO = 0.5      # 第 2 个起入选文件的得分下限（相对第 1 名）
TIME_BUDGET_S = 10.0          # 块级预扫的时间预算；逐行统计、取片段各另给一半（最坏 20s）
READ_CHUNK_BYTES = 1 << 20    # 单次 readline 的字节上限
BINARY_SNIFF_BYTES = 8192
_DEADLINE_CHECK_EVERY = 5000  # 每扫多少块检查一次时间预算
_WINDOW_OVERHEAD = 40         # 每个窗口的段标题等额外字符估算


def split_local_docs_by_mode(
    local_docs: Optional[List[Any]],
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """
    按 mode 把 local_docs 配置分成 (向量索引目录, 关键词直查目录)

    mode 缺省 / 空值 / "grep" → 直查；只有显式 "index" 才进向量索引；
    无法识别的值打 warning 后按默认值 grep 处理。非 dict 项原样归入索引组，
    保持改前的处理方式。
    """
    index_cfgs: List[Any] = []
    grep_cfgs: List[Dict[str, Any]] = []
    for cfg in local_docs or []:
        if not isinstance(cfg, dict):
            index_cfgs.append(cfg)
            continue
        mode = str(cfg.get("mode") or MODE_GREP).strip().lower()
        if mode == MODE_INDEX:
            index_cfgs.append(cfg)
            continue
        if mode != MODE_GREP:
            logger.warning(
                f"local_docs 的 mode 取值无法识别（{cfg.get('mode')!r}），按默认 grep 处理: "
                f"path={cfg.get('path', '')}"
            )
        grep_cfgs.append(cfg)
    return index_cfgs, grep_cfgs


def _normalize_keywords(keywords: Any) -> List[str]:
    """strip、转小写、去重，丢掉过短的，最多 MAX_KEYWORDS 个"""
    if isinstance(keywords, str):
        keywords = [keywords]  # 容错：单个关键词直接传成字符串，不能按字符拆
    result: List[str] = []
    for k in keywords or []:
        if not isinstance(k, str):
            continue
        k = k.strip().lower()
        if len(k) >= MIN_KEYWORD_LEN and k not in result:
            result.append(k)
    return result[:MAX_KEYWORDS]


def _iter_doc_files(doc_configs: List[Dict[str, Any]]):
    """产出 (绝对路径, 标题, path 字段)，遍历顺序稳定"""
    for cfg in doc_configs:
        root_dir = cfg.get("path", "")
        description = cfg.get("description", "")
        if not root_dir or not os.path.isdir(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            dirs.sort()
            for fname in sorted(files):
                if not fname.endswith(DOC_EXTENSIONS):
                    continue
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, root_dir)
                yield fpath, f"[{description}] {rel_path}", f"{description}/{rel_path}"


def _line_mask(text: str, keywords: List[str]) -> int:
    """本行命中的关键词位掩码（text 已转小写）"""
    mask = 0
    for i, kw in enumerate(keywords):
        if kw in text:
            mask |= 1 << i
    return mask


def _byte_patterns(keywords: List[str]) -> Optional[List[List[bytes]]]:
    """
    字节级预过滤用的模式：每个关键词一组，含其 UTF-8 / GB18030 编码再按字节转小写

    原始行 raw 解码后（大小写不敏感）含关键词 ⇒ raw.lower() 含该组某个模式：两种编码
    里 ASCII 都是单字节原样编码，大小写只差在 ASCII 字母字节上，bytes.lower() 对两边
    的多字节部分做的改动相同。反过来不成立（GBK 尾字节可能拼出模式），所以预过滤
    命中后仍要解码确认。关键词含有大小写之分的非 ASCII 字符（如全角字母）时上述
    推理不成立，返回 None 不做预过滤。
    """
    result: List[List[bytes]] = []
    for kw in keywords:
        if any(ord(c) > 127 and c.upper() != c.lower() for c in kw):
            return None
        group: List[bytes] = []
        for encoding in ("utf-8", "gb18030"):
            try:
                p = kw.encode(encoding).lower()
            except UnicodeEncodeError:
                continue
            if p not in group:
                group.append(p)
        result.append(group)
    return result


def _flatten(patterns: Optional[List[List[bytes]]]) -> Optional[List[bytes]]:
    return None if patterns is None else [p for group in patterns for p in group]


def _raw_mask(raw: bytes, keywords: List[str], flat_patterns: Optional[List[bytes]]) -> int:
    """一块原始字节的命中掩码：先按字节预过滤，命中才解码（大文件绝大多数行不命中）"""
    if flat_patterns is not None:
        low = raw.lower()
        if not any(p in low for p in flat_patterns):
            return 0
    return _line_mask(decode_line(raw).lower(), keywords)


def _file_mask(fpath: str, patterns: List[List[bytes]], deadline: float) -> Tuple[int, bool]:
    """
    块级预扫：每个关键词的编码是否出现在文件字节里（可能多报，不会漏报）

    只用于算文件频率、挑出要逐行统计的文件。按 READ_CHUNK_BYTES 整块查找比逐行
    解码快一个数量级，多数文件不含案号 / 表名这类稀有词，不必逐行读。
    相邻块保留重叠，跨块的关键词也能找到。

    Returns:
        (关键词位掩码, 是否超时中断)
    """
    overlap = max((len(p) for group in patterns for p in group), default=1) - 1
    full = (1 << len(patterns)) - 1
    mask = 0
    tail = b""
    with open(fpath, "rb") as f:
        while mask != full:
            if time.monotonic() > deadline:
                return mask, True
            block = f.read(READ_CHUNK_BYTES)
            if not block:
                break
            buf = tail + block.lower()
            for i, group in enumerate(patterns):
                if not mask >> i & 1 and any(p in buf for p in group):
                    mask |= 1 << i
            tail = buf[len(buf) - overlap:] if overlap > 0 else b""
    return mask, False


def _count_hits(
    fpath: str, keywords: List[str], flat_patterns: Optional[List[bytes]], deadline: float,
    count_mask: int = -1,
) -> Tuple[int, int, bool]:
    """
    逐行统计一个文件的命中情况，不保留正文

    Args:
        count_mask: 只有命中这些关键词的行才计入命中行数（默认全部关键词）

    Returns:
        (命中关键词位掩码, 命中行数, 是否超时中断)
    """
    file_mask = 0
    hit_lines = 0
    last_hit_line = 0
    for n, (line_no, raw) in enumerate(iter_chunks(fpath, READ_CHUNK_BYTES)):
        if n % _DEADLINE_CHECK_EVERY == 0 and time.monotonic() > deadline:
            return file_mask, hit_lines, True
        mask = _raw_mask(raw, keywords, flat_patterns)
        if mask:
            file_mask |= mask
            if mask & count_mask and line_no != last_hit_line:  # 超长行被切成多块时只算一行
                hit_lines += 1
                last_hit_line = line_no
    return file_mask, hit_lines, False


def _top_hit_lines(
    fpath: str, keywords: List[str], weights: List[float], informative_mask: int,
    deadline: float,
) -> List[Tuple[float, int]]:
    """
    取片段第一步：只记行号和优先级，保留优先级最高的 MAX_KEPT_HITS 个命中行

    只有命中"非泛词"的行才算；行优先级 = 本行命中关键词的权重之和。
    分两遍是为了内存有界：命中几万行的大文件只留 500 个行号，不留正文。

    Returns:
        [(优先级, 行号)]，按优先级降序、行号升序
    """
    heap: List[Tuple[float, int]] = []  # 最小堆 (优先级, -行号)

    def push(line_no: int, mask: int) -> None:
        if not mask:
            return
        item = (sum(weights[i] for i in range(len(keywords)) if mask >> i & 1), -line_no)
        if len(heap) < MAX_KEPT_HITS:
            heapq.heappush(heap, item)
        else:
            heapq.heappushpop(heap, item)

    informative = [kw for i, kw in enumerate(keywords) if informative_mask >> i & 1]
    patterns = _flatten(_byte_patterns(informative))  # 只命中泛词的行不用解码
    cur_no, cur_mask = 0, 0  # 超长行被切成多块时，各块的命中合并成一行
    for n, (line_no, raw) in enumerate(iter_chunks(fpath, READ_CHUNK_BYTES)):
        if n % _DEADLINE_CHECK_EVERY == 0 and time.monotonic() > deadline:
            break
        if line_no != cur_no:
            push(cur_no, cur_mask)
            cur_no, cur_mask = line_no, 0
        cur_mask |= _raw_mask(raw, keywords, patterns) & informative_mask
    push(cur_no, cur_mask)
    return sorted(((s, -neg) for s, neg in heap), key=lambda h: (-h[0], h[1]))


def _excerpt(
    fpath: str, keywords: List[str], weights: List[float], informative_mask: int,
    budget: int, deadline: float,
) -> Tuple[str, int]:
    """
    取命中行及前后 CONTEXT_LINES 行：优先级高的命中先占 budget，最后按行号输出

    Returns:
        (正文片段, 展示的窗口数)
    """
    hits = _top_hit_lines(fpath, keywords, weights, informative_mask, deadline)
    if not hits:
        return "", 0
    hit_nos = {no for _, no in hits}
    wanted = {
        no for _, h in hits for no in range(h - CONTEXT_LINES, h + CONTEXT_LINES + 1) if no > 0
    }
    last_wanted = max(wanted)

    # 取片段第二步：只取窗口内的行（≤ 500 × 7 行，每行 ≤ 500 字符）
    texts: Dict[int, str] = {}
    hit_shown: set = set()  # 已取到含关键词那一块的命中行
    for n, (line_no, raw) in enumerate(iter_chunks(fpath, READ_CHUNK_BYTES)):
        if line_no > last_wanted:
            break
        if n % _DEADLINE_CHECK_EVERY == 0 and time.monotonic() > deadline:
            break
        if line_no not in wanted:
            continue
        is_hit = line_no in hit_nos
        if line_no in texts and (not is_hit or line_no in hit_shown):
            continue  # 超长行的后续块：上下文行只展示首块
        text = decode_line(raw).rstrip("\r\n")
        if is_hit:
            lowered = text.lower()
            found = [lowered.find(kw) for kw in keywords if kw in lowered]
            if found:  # 命中行展示含关键词的那一块，以命中位置为中心截取
                texts[line_no] = clip_line(text, MAX_LINE_CHARS, min(found))
                hit_shown.add(line_no)
                continue
        texts.setdefault(line_no, clip_line(text, MAX_LINE_CHARS))

    # 优先级高的命中先占预算；同优先级行号小的先
    chosen: set = set()
    used = 0
    for _, hit_no in hits:
        if hit_no not in texts:  # 超时没读到
            continue
        window = [
            no for no in range(hit_no - CONTEXT_LINES, hit_no + CONTEXT_LINES + 1)
            if no in texts and no not in chosen
        ]
        cost = sum(len(texts[no]) + 8 for no in window) + _WINDOW_OVERHEAD
        if used + cost > budget:
            continue
        chosen.update(window)
        used += cost

    if not chosen:
        return "", 0
    segments: List[List[int]] = []
    for no in sorted(chosen):
        if segments and no == segments[-1][-1] + 1:
            segments[-1].append(no)
        else:
            segments.append([no])
    parts = []
    for seg in segments:
        parts.append(f"--- 第 {seg[0]}-{seg[-1]} 行 ---")
        parts.extend(f"{no}: {texts[no]}" for no in seg)
    return "\n".join(parts), len(segments)


def grep_local_docs(doc_configs: List[Dict[str, Any]], keywords: Any) -> List[Dict[str, str]]:
    """
    在关键词直查目录里按内容逐行查找，返回与向量检索结果同构的文档列表

    Args:
        doc_configs: mode 为 grep 的 local_docs 配置项（见 split_local_docs_by_mode）
        keywords: AI 提取的关键词列表

    Returns:
        [{"token", "title", "path", "url", "content"}]，最多 MAX_DOCS 篇，
        content 为命中行及上下文；无命中返回 []
    """
    kws = _normalize_keywords(keywords)
    if not doc_configs or not kws:
        return []

    start = time.monotonic()
    deadline = start + TIME_BUDGET_S
    scanned = 0
    failed = 0
    timed_out = False
    patterns = _byte_patterns(kws)
    flat_patterns = _flatten(patterns)
    # 第一遍：块级预扫得到每个文件含哪些关键词（无法预过滤时直接逐行统计）
    stats: List[Tuple[str, str, str, int, int]] = []  # (fpath, title, path, mask, 行数)
    for fpath, title, path in _iter_doc_files(doc_configs):
        if time.monotonic() > deadline:
            timed_out = True
            break
        if is_binary(fpath, BINARY_SNIFF_BYTES):
            continue
        try:
            if patterns is None:
                mask, hit_lines, cut = _count_hits(fpath, kws, None, deadline)
            else:
                mask, cut = _file_mask(fpath, patterns, deadline)
                hit_lines = 0  # 逐行统计留给第二遍，只做命中非泛词的文件
        except OSError:
            failed += 1
            continue
        scanned += 1
        if mask:
            stats.append((fpath, title, path, mask, hit_lines))
        if cut:
            timed_out = True
            break
    scan_s = time.monotonic() - start

    if timed_out:
        logger.warning(
            f"本地文档关键词直查超出时间预算 {TIME_BUDGET_S:.0f}s，只用已扫描部分: "
            f"已扫描 {scanned} 个文件"
        )
    if not stats:
        logger.info(
            f"本地文档关键词直查无命中: 扫描 {scanned} 个文件, 失败 {failed}, 耗时 {scan_s:.2f}s"
        )
        return []

    # 文件频率与权重；泛词不参与选文件
    n_files = max(scanned, 1)
    df = [sum(1 for s in stats if s[3] >> i & 1) for i in range(len(kws))]
    hit_kw_mask = 0
    for s in stats:
        hit_kw_mask |= s[3]
    informative_mask = 0
    for i in range(len(kws)):
        # 命中文件数 ≤ MAX_DOCS 的词再常见也塞不满配额，不算泛词（小目录不误判）
        if df[i] and (df[i] <= n_files * COMMON_DF_RATIO or df[i] <= MAX_DOCS):
            informative_mask |= 1 << i
    if not informative_mask:
        if any(d == 0 for d in df):
            # 有关键词（多半是案号/表名）一个文件都没命中，命中的只有泛词：
            # 这些文件和问题无关，不返回，免得白占配额
            logger.info(
                f"本地文档关键词直查只命中泛词，不返回: 扫描 {scanned} 个文件, "
                f"命中 {len(stats)} 个, 耗时 {scan_s:.2f}s"
            )
            return []
        informative_mask = hit_kw_mask  # 每个词都命中、且都是泛词：退回按全部关键词排序
    weights = [math.log(n_files / d) + 1 if d else 0.0 for d in df]

    def score(mask: int) -> float:
        return sum(weights[i] for i in range(len(kws)) if (mask & informative_mask) >> i & 1)

    eligible = [s for s in stats if s[3] & informative_mask]
    # 逐行统计、取片段各自另给一半预算：前一步超时也不会把后一步饿死
    # （曾共用一个预算：全泛词时逐行统计耗尽 5s，入选文件一篇都取不到片段）
    count_deadline = time.monotonic() + TIME_BUDGET_S / 2
    if patterns is not None or informative_mask != hit_kw_mask:
        # 只对命中非泛词的文件逐行统计：拿到精确掩码（剔除预扫的误报）和命中行数。
        # 有泛词时只数命中非泛词的行：案号查询曾显示"共 6001 行命中"，其实命中
        # 案号的只有 1 行，其余全是"日志"，AI 会误以为有大量相关记录
        exact = []
        for fpath, title, path, _, _ in eligible:
            try:
                mask, hit_lines, cut = _count_hits(
                    fpath, kws, flat_patterns, count_deadline, informative_mask
                )
            except OSError:
                continue
            if mask & informative_mask:
                exact.append((fpath, title, path, mask, hit_lines))
            if cut:
                logger.warning(f"本地文档关键词直查逐行统计超时，只用已统计的 {len(exact)} 个文件")
                break
        eligible = exact
        if not eligible:
            logger.info(f"本地文档关键词直查逐行确认后无命中: 扫描 {scanned} 个文件")
            return []
    eligible.sort(key=lambda s: (-score(s[3]), -s[4], s[2]))
    top_score = score(eligible[0][3])
    selected = [eligible[0]] + [
        s for s in eligible[1:] if score(s[3]) >= top_score * SELECT_SCORE_RATIO
    ][:MAX_DOCS - 1]
    # 未展开的：先列命中非泛词的（按排名），再列只命中泛词的
    selected_paths = {s[0] for s in selected}
    eligible_paths = {s[0] for s in eligible}
    others = [s for s in eligible if s[0] not in selected_paths] + [
        s for s in stats if s[0] not in eligible_paths and not s[3] & informative_mask
    ]

    # 预留 1000 字符给说明行和"另有 K 个文件"
    budget = min(MAX_DOC_CHARS, MAX_TOTAL_CHARS // len(selected)) - 1000
    excerpt_deadline = time.monotonic() + TIME_BUDGET_S / 2
    docs: List[Dict[str, str]] = []
    total_windows = 0
    for fpath, title, path, mask, hit_lines in selected:
        try:
            body, windows = _excerpt(
                fpath, kws, weights, informative_mask, budget, excerpt_deadline
            )
        except OSError:
            continue
        if not body:
            continue
        total_windows += windows
        hit_names = "".join(f"「{kws[i]}」" for i in range(len(kws)) if mask >> i & 1)
        common_names = "".join(
            f"「{kws[i]}」" for i in range(len(kws))
            if mask >> i & 1 and not informative_mask >> i & 1
        )
        line_scope = ""
        if common_names:
            line_scope = "".join(
                f"「{kws[i]}」" for i in range(len(kws)) if (mask & informative_mask) >> i & 1
            )
        header = (
            f"（关键词直查：命中 {bin(mask).count('1')}/{len(kws)} 个关键词{hit_names}，"
            f"共 {hit_lines} 行命中{line_scope}；"
            f"以下为优先级最高的命中行及前后 {CONTEXT_LINES} 行"
        )
        if common_names:
            header += f"；{common_names}在多数文件中都出现，未计入行数、未单独展开"
        header += "）"
        docs.append({
            "token": "localgrep_" + path,
            "title": title,
            "path": path,
            "url": "",
            "content": f"{header}\n{body}",
        })
    if docs and others:
        names = "、".join(s[2] for s in others[:MAX_OTHER_FILES])
        more = "等" if len(others) > MAX_OTHER_FILES else ""
        docs[-1]["content"] += (
            f"\n\n（另有 {len(others)} 个文件也命中关键词，未展开：{names}{more}）"
        )

    logger.info(
        f"本地文档关键词直查: 扫描 {scanned} 个文件(失败 {failed}), 命中 {len(stats)} 个, "
        f"展开 {len(docs)} 个/{total_windows} 段, 耗时 {time.monotonic() - start:.2f}s"
        f"(扫描 {scan_s:.2f}s)"
    )
    return docs
