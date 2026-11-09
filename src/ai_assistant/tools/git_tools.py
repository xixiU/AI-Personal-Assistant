"""
Git 只读代码工具

基于 git 命令提供代码检索和浏览能力，所有操作都是只读的，不修改工作目录。
支持并发安全：多个线程可以同时查询不同分支的代码，互不干扰。
"""

import subprocess
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from loguru import logger


class GitTools:
    """Git 只读代码工具集"""

    # git grep -P 依赖编译时带 libpcre2，是 git 程序本身的属性，进程内探测一次即可。
    # None 表示尚未探测
    _pcre_supported: Optional[bool] = None

    def __init__(self, repo_path: str, default_ref: str = "origin/main"):
        """
        初始化 Git 工具

        Args:
            repo_path: 仓库根目录路径
            default_ref: 默认引用（分支/tag），用户未指定时使用
        """
        self.repo_path = Path(repo_path).resolve()
        self.default_ref = default_ref

        if not self.repo_path.exists():
            raise ValueError(f"仓库路径不存在: {repo_path}")

        if not (self.repo_path / ".git").exists():
            raise ValueError(f"不是有效的 git 仓库: {repo_path}")

        logger.info(f"GitTools 初始化: repo={self.repo_path}, default_ref={default_ref}")

    @staticmethod
    def _english_messages_env() -> Dict[str, str]:
        """
        只把报错文案固定为英文，字符集设置保持不变。

        _is_pattern_error 按英文关键字识别模式错误，zh_CN 环境下报错被翻译就会失效。
        不能直接 LC_ALL=C：Linux 上 LC_CTYPE=C 会让正则按字节匹配，'资.训' 这类
        中文正则会静默漏搜。LC_ALL 优先级最高、会盖住 LC_MESSAGES，所以把它的值
        下放到影响匹配的 LC_CTYPE / LC_COLLATE 后再删掉。
        """
        env = dict(os.environ)
        lc_all = env.pop("LC_ALL", None)
        if lc_all:
            # LC_ALL 原本就盖住了这两项，直接覆盖才与原行为一致
            env["LC_CTYPE"] = lc_all
            env["LC_COLLATE"] = lc_all
        env["LC_MESSAGES"] = "C"
        env["LANGUAGE"] = "C"
        return env

    def _run_git_command(self, args: List[str], timeout: int = 30) -> str:
        """
        执行 git 命令（只读操作）

        Args:
            args: git 命令参数列表
            timeout: 超时时间（秒）

        Returns:
            命令输出

        Raises:
            subprocess.TimeoutExpired: 超时
            subprocess.CalledProcessError: 命令执行失败
        """
        try:
            result = subprocess.run(
                # core.quotePath 默认 true，中文路径会输出成 "\345\211\215..." 八进制转义，
                # AI 拿去 read_file 必然找不到；部署环境不一定有全局配置，这里统一关掉
                ["git", "-c", "core.quotePath=false"] + args,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,  # 不自动抛异常，让调用者处理退出码
                encoding='utf-8',
                errors='replace',  # 替换无法解码的字符，避免崩溃
                env=self._english_messages_env() if args[0] == "grep" else None,
            )
            # 检查退出码，但 grep 的1（没匹配）不算错误
            if result.returncode != 0:
                # git grep 返回1表示没匹配，属于正常情况
                if args[0] == "grep" and result.returncode == 1:
                    return ""  # 返回空字符串表示没结果
                # grep 返回 128 多为正则语法无效，调用方会换写法降级重试，
                # 这里不按 ERROR 记录，避免正常的重试流程看起来像故障
                if args[0] == "grep" and result.returncode == 128:
                    logger.debug(f"Git grep 正则无效: {' '.join(args)}, stderr={result.stderr}")
                else:
                    logger.error(f"Git 命令失败: {' '.join(args)}, returncode={result.returncode}, stderr={result.stderr}")
                raise subprocess.CalledProcessError(result.returncode, ["git"] + args, result.stdout, result.stderr)
            return result.stdout
        except subprocess.TimeoutExpired as e:
            logger.error(f"Git 命令超时: {' '.join(args)}")
            raise

    def _validate_ref(self, ref: str) -> bool:
        """
        验证 ref 是否存在（安全检查，防止命令注入）

        Args:
            ref: git 引用（分支/tag/commit）

        Returns:
            ref 是否存在
        """
        try:
            self._run_git_command(["rev-parse", "--verify", ref], timeout=5)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    def _validate_path(self, path: str) -> bool:
        """
        验证路径是否在仓库内（防止目录穿越）

        Args:
            path: 相对于仓库根目录的路径

        Returns:
            路径是否合法
        """
        try:
            # 解析为绝对路径并检查是否在仓库内
            full_path = (self.repo_path / path).resolve()
            return full_path.is_relative_to(self.repo_path)
        except (ValueError, OSError):
            return False

    def list_refs(self, pattern: Optional[str] = None) -> Dict[str, Any]:
        """
        列出分支和 tag

        Args:
            pattern: 可选的过滤模式（如 "4.3" 匹配包含 4.3 的分支/tag）

        Returns:
            包含 branches 和 tags 的字典
        """
        try:
            # 获取远程分支
            branches_output = self._run_git_command(["branch", "-r"])
            branches = [
                line.strip().replace("origin/", "")
                for line in branches_output.strip().split("\n")
                if line.strip() and "HEAD" not in line
            ]

            # 获取 tags
            tags_output = self._run_git_command(["tag", "-l"])
            tags = [line.strip() for line in tags_output.strip().split("\n") if line.strip()]

            # 如果有 pattern，过滤结果
            if pattern:
                pattern_lower = pattern.lower()
                branches = [b for b in branches if pattern_lower in b.lower()]
                tags = [t for t in tags if pattern_lower in t.lower()]

            logger.info(f"list_refs: pattern={pattern}, branches={len(branches)}, tags={len(tags)}")
            return {
                "branches": branches[:20],  # 限制返回数量
                "tags": tags[:20],
                "default_ref": self.default_ref
            }
        except Exception as e:
            logger.error(f"list_refs 失败: {e}")
            return {"branches": [], "tags": [], "error": str(e)}

    @staticmethod
    def _normalize_pathspec(path_filter: str) -> List[str]:
        """
        git pathspec 是从仓库根目录开始匹配的，裸文件名（如 "RecordList.vue"）
        只会匹配根目录下的同名文件，深层目录里的文件一个都匹配不到，且退出码为
        1（等同"没找到"），不会报错——这会让调用方误以为代码里没有该内容。

        因此同时给出原始写法和 `*` 前缀写法，两种都作为 pathspec 传入（git 对多个
        pathspec 取并集，匹配不到的那个不会报错）。

        Args:
            path_filter: 调用方给的路径过滤

        Returns:
            pathspec 列表
        """
        spec = path_filter.strip()
        specs = [spec]
        if not spec.startswith("*"):
            specs.append("*" + spec)
        # 末段不含 "." 时视为目录名，补一个 */dir/* 形式匹配目录下所有文件
        last_segment = spec.rstrip("/").rsplit("/", 1)[-1]
        if "." not in last_segment:
            specs.append("*" + spec.rstrip("/") + "/*")
        return specs

    def _pcre_available(self) -> bool:
        """
        探测当前 git 是否支持 grep -P（结果进程级缓存）。

        用一个只有 PCRE 才认的模式对暂存区跑一次：退出码 0/1（有/无匹配）说明支持；
        128 说明没编译 libpcre2 或不认 -P。--cached 不依赖 HEAD，空仓库也能探测。
        超时不缓存，下次再探。
        """
        if GitTools._pcre_supported is None:
            try:
                self._run_git_command(
                    ["grep", "-P", "-q", "-e", r"(?i)\d(?:x)*?", "--cached"], timeout=10)
                GitTools._pcre_supported = True
            except subprocess.CalledProcessError:
                GitTools._pcre_supported = False
            except subprocess.TimeoutExpired:
                return False
            logger.info(f"git grep -P 支持情况: {GitTools._pcre_supported}")
        return GitTools._pcre_supported

    @staticmethod
    def _is_pattern_error(e: subprocess.CalledProcessError, patterns: List[str]) -> bool:
        """
        判断 grep 失败是否由模式本身引起（只有这种才值得降级换写法重试）。

        坏 ref、坏 pathspec 同样返回 128，降级换写法也救不回来，不能算作模式错误。
        git 报模式错误时会把模式加单引号带进 stderr（"-e option, '<模式>': ..."），
        要连引号一起比，否则 'a' 这类短模式会在任何报错文字里"命中"。
        PCRE 运行期错误（如回溯超限）带 "pcre" 字样。
        """
        if e.returncode != 128:
            return False
        stderr = (e.stderr or "").lower()
        if any(p and f"'{p.lower()}'" in stderr for p in patterns):
            return True
        return any(k in stderr for k in ("pcre", "regex", "regular expression"))

    @staticmethod
    def _perl_to_ere(pattern: str) -> Optional[str]:
        """
        把常见的 Perl 风格写法转成 POSIX ERE。

        只做等价或只会放宽匹配的转换（多搜到无妨，漏搜会让 AI 误判"代码里没有"）：
        \\d \\s \\w → 字符类；(?: / 命名组 / 原子组 → (；懒惰/占有量词去掉后缀；
        \\b \\B 去掉（放宽）；开头的 (?i) 等内联标记去掉（search_code 固定 -i）。
        遇到无法等价表达的（反向引用、环视、\\x 转义、(?x) 等）返回 None，交给下一级。

        Returns:
            ERE 模式；无法安全转换时返回 None
        """
        classes = {
            "d": "[0-9]", "D": "[^0-9]",
            "s": "[[:space:]]", "S": "[^[:space:]]",
            "w": "[[:alnum:]_]", "W": "[^[:alnum:]_]",
        }
        in_bracket = {"d": "0-9", "s": "[:space:]", "w": "[:alnum:]_"}
        ere_meta = set(".[]()*+?{}|^$\\")
        out: List[str] = []
        depth = 0
        i, n = 0, len(pattern)
        after_quantifier = False

        while i < n:
            c = pattern[i]
            if after_quantifier and c in "?+":
                # 懒惰（*?）或占有（*+）后缀：判断"这一行是否匹配"时与贪婪等价
                i += 1
                after_quantifier = False
                continue
            after_quantifier = False

            if c == "\\":
                if i + 1 >= n:
                    return None
                e = pattern[i + 1]
                i += 2
                if e in classes:
                    out.append(classes[e])
                elif e in "bB":
                    pass  # 单词边界只收窄匹配，去掉即放宽
                elif e == "A":
                    out.append("^")
                elif e in "zZ":
                    out.append("$")
                elif e == "t":
                    out.append("\t")
                elif e.isalnum():
                    return None  # \x \p \n \1 \K 等，ERE 无等价写法
                elif e in ere_meta:
                    out.append("\\" + e)
                else:
                    # \- \< \/ 等：Perl 里就是该字符本身。ERE 里 \< 是词首，要去掉反斜杠
                    out.append(e)
                continue

            if c == "[":
                j = i + 1
                body = ["["]
                if j < n and pattern[j] == "^":
                    body.append("^")
                    j += 1
                if j < n and pattern[j] == "]":
                    body.append("]")
                    j += 1
                while j < n and pattern[j] != "]":
                    if pattern[j] == "\\":
                        if j + 1 >= n:
                            return None
                        e = pattern[j + 1]
                        if e in in_bracket:
                            body.append(in_bracket[e])
                        elif not e.isalnum() and e not in "]\\^-[":
                            body.append(e)
                        else:
                            return None  # 方括号内 \] \\ \- 等在 ERE 里写法不同，不冒险
                        j += 2
                    elif pattern.startswith("[:", j):
                        end = pattern.find(":]", j + 2)
                        if end < 0:
                            return None
                        body.append(pattern[j:end + 2])
                        j = end + 2
                    else:
                        body.append(pattern[j])
                        j += 1
                if j >= n:
                    return None
                body.append("]")
                out.append("".join(body))
                i = j + 1
                continue

            if c == "(":
                if pattern.startswith("(?", i):
                    flags = re.match(r"\(\?([a-zA-Z]*)(?:-([a-zA-Z]*))?(:|\))", pattern[i:])
                    named = re.match(r"\(\?(?:P?<[A-Za-z_]\w*>|'[A-Za-z_]\w*')", pattern[i:])
                    if flags:
                        if "x" in (flags.group(1) or "") + (flags.group(2) or ""):
                            return None  # (?x) 会忽略模式里的空白，去掉它会改变含义
                        i += flags.end()
                        if flags.group(3) == ":":
                            out.append("(")
                            depth += 1
                        continue  # 单独的 (?i) 等直接去掉
                    if named:
                        out.append("(")
                        depth += 1
                        i += named.end()
                        continue
                    if pattern.startswith("(?>", i):
                        out.append("(")
                        depth += 1
                        i += 3
                        continue
                    return None  # 环视、条件组、注释等
                out.append("(")
                depth += 1
                i += 1
                continue

            if c == ")":
                depth -= 1
                if depth < 0:
                    return None
                out.append(")")
                i += 1
                continue

            if c in "*+?":
                out.append(c)
                after_quantifier = True
                i += 1
                continue

            if c == "{":
                interval = re.match(r"\{\d+(?:,\d*)?\}", pattern[i:])
                if interval:
                    out.append(interval.group(0))
                    after_quantifier = True
                    i += interval.end()
                else:
                    out.append("[{]")  # Perl 里非区间的 { 是字面量
                    i += 1
                continue

            if c == "}":
                out.append("[}]")
                i += 1
                continue

            out.append(c)
            i += 1

        if depth != 0:
            return None
        return "".join(out)

    @staticmethod
    def _extract_keywords(pattern: str, limit: int = 10) -> List[str]:
        """
        从正则里提取有意义的字面片段，作为"任一命中"的关键词。

        按元字符（| . * ( ) [ ] 等）切开连续的字面字符；量词 ? * {0,..} 让前一个
        字符变成可选，把它从片段里去掉；否定环视里的内容是"不能出现"的，丢弃。
        长度小于 2 或不含字母数字/汉字的片段丢弃，大小写不敏感去重。

        Returns:
            关键词列表（可能为空）
        """
        fragments: List[str] = []
        run: List[str] = []
        negative_stack: List[bool] = []
        skip_depth = 0  # 处于否定环视内时 > 0
        i, n = 0, len(pattern)

        def flush():
            if run and skip_depth == 0:
                fragments.append("".join(run))
            run.clear()

        while i < n:
            c = pattern[i]
            if c == "\\" and i + 1 < n:
                e = pattern[i + 1]
                i += 2
                if not e.isalnum():
                    run.append(e)  # \( \. 等转义的字面字符
                    continue
                flush()
                if e in "pPxu" and i < n and pattern[i] == "{":
                    end = pattern.find("}", i)
                    i = end + 1 if end >= 0 else n
                continue
            if c == "[":
                flush()
                j = i + 1
                if j < n and pattern[j] == "^":
                    j += 1
                if j < n and pattern[j] == "]":
                    j += 1
                while j < n and pattern[j] != "]":
                    j += 2 if pattern[j] == "\\" else 1
                i = j + 1
                continue
            if c == "(":
                flush()
                header = re.match(r"\(\?(?:<?[=!]|[a-zA-Z]*(?:-[a-zA-Z]*)?[:)]|P?<\w+>|'\w+'|>|)",
                                  pattern[i:])
                consumed = header.end() if header else 1
                is_negative = "!" in pattern[i:i + consumed]
                if pattern[i:i + consumed].endswith(")") and consumed > 1:
                    i += consumed  # 单独的 (?i) 标记，没有开组
                    continue
                negative_stack.append(is_negative)
                if is_negative:
                    skip_depth += 1
                i += consumed
                continue
            if c == ")":
                flush()
                if negative_stack and negative_stack.pop():
                    skip_depth -= 1
                i += 1
                continue
            if c in "?*" or (c == "{" and re.match(r"\{0?,", pattern[i:])):
                if run:
                    run.pop()  # 前一个字符可选
                flush()
                if c == "{":
                    end = pattern.find("}", i)
                    i = end + 1 if end >= 0 else n
                else:
                    i += 1
                continue
            if c in "+|.^$":
                flush()
                i += 1
                continue
            if c == "{":
                flush()
                interval = re.match(r"\{\d+(?:,\d*)?\}", pattern[i:])
                i += interval.end() if interval else 1
                continue
            run.append(c)
            i += 1
        flush()

        keywords: List[str] = []
        seen = set()
        for frag in fragments:
            frag = frag.strip()
            if len(frag) < 2 or not any(ch.isalnum() for ch in frag):
                continue
            if frag.lower() in seen:
                continue
            seen.add(frag.lower())
            keywords.append(frag)
            if len(keywords) >= limit:
                break
        return keywords

    def _grep_with_fallback(self, args_prefix: List[str], query: str, ref: str,
                            pathspecs: List[str]) -> tuple:
        """
        按多级策略执行 git grep，只有"模式本身执行不了"才降到下一级：

        1. pcre：-P，AI 习惯的 Perl 风格写法（(?i) \\d \\b 懒惰量词等）直接可用；
           需要 git 带 libpcre2，进程内探测一次
        2. ere：-E，Perl 写法经 _perl_to_ere 转换（合法 ERE 原样不变）
        3. keywords：-F 多个 -e，按正则里提取的字面片段搜任一命中
        4. literal：-F，整串当字面量（提取不出关键词时才用）

        "无匹配"（退出码 1）不是失败，不降级，否则一个正确但确实没结果的正则会被
        逐级放宽，搜出一堆无关内容。

        历史教训：原实现按"是否含特殊字符"猜测，导致 'class.*Service' 这类真正的
        正则被当作字面量搜索。现在合法的正则一定在第 1 或第 2 级按正则执行。

        模式一律用 -e 传入，避免以 "-" 开头的 query 被 git 当成选项。

        Returns:
            (输出文本, 实际使用的策略名, 第 3 级使用的关键词列表)
        """
        attempts = []
        if self._pcre_available():
            attempts.append(("pcre", "-P", [query]))
        ere = self._perl_to_ere(query)
        if ere is not None:
            attempts.append(("ere", "-E", [ere]))
        keywords = self._extract_keywords(query)
        if keywords:
            attempts.append(("keywords", "-F", keywords))
        else:
            attempts.append(("literal", "-F", [query]))

        last_error = None
        for strategy, flag, patterns in attempts:
            args = ["grep", flag] + args_prefix
            for p in patterns:
                args += ["-e", p]
            args.append(ref)
            if pathspecs:
                args += ["--"] + pathspecs
            try:
                return self._run_git_command(args), strategy, keywords
            except subprocess.CalledProcessError as e:
                if not self._is_pattern_error(e, patterns):
                    raise
                logger.info(f"grep 策略 {strategy} 无法执行该模式，降级: query='{query}'")
                last_error = e
        raise last_error

    @staticmethod
    def _strategy_notice(strategy: str, keywords: List[str]) -> Optional[str]:
        """
        生成给 AI 看的降级说明；按原正则执行（pcre）时返回 None。

        AI 拿到空结果容易误判"代码里没有这个功能"，降级后必须让它知道实际搜了什么。
        """
        if strategy == "ere":
            return "当前环境不支持 Perl 风格正则，已转换为等价的 POSIX 扩展正则执行（\\b 等边界会被放宽）。"
        if strategy == "keywords":
            return (f"原正则无法执行，已改为按关键词 {'、'.join(keywords)} 搜索（命中任一即返回）；"
                    "结果为空不代表代码里没有，请换成更简单的关键词重试。")
        if strategy == "literal":
            return ("原正则无法执行，也提取不出关键词，已把整串当普通文本搜索；"
                    "结果为空不代表代码里没有，请换成更简单的关键词重试。")
        return None

    def search_code(
        self,
        query: str,
        ref: Optional[str] = None,
        path_filter: Optional[str] = None,
        max_results: int = 50,
        context: int = 0
    ) -> Dict[str, Any]:
        """
        在代码中搜索关键词（git grep）

        Args:
            query: 搜索关键词（支持正则，执行不了时逐级降级，见 _grep_with_fallback）
            ref: git 引用（分支/tag），为 None 时使用默认
            path_filter: 可选的路径过滤（如 "*.py"；裸文件名会自动补全匹配）
            max_results: 最大返回结果数
            context: 每个匹配额外返回的上下文行数（0 表示只返回匹配行）

        Returns:
            搜索结果字典
        """
        ref = ref or self.default_ref

        # 安全检查
        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}", "results": []}

        try:
            args_prefix = ["-n", "-i"]
            if context > 0:
                args_prefix += ["-C", str(context)]

            pathspecs = self._normalize_pathspec(path_filter) if path_filter else []
            output, strategy, keywords = self._grep_with_fallback(
                args_prefix, query, ref, pathspecs)
            # 降级说明放在 results 前面，让 AI 先看到"实际是怎么搜的"
            meta = {"query": query, "ref": ref, "strategy": strategy}
            notice = self._strategy_notice(strategy, keywords)
            if notice:
                meta["notice"] = notice

            if not output:
                logger.info(
                    f"search_code: query='{query}', ref={ref}, strategy={strategy}, no results")
                return {**meta, "results": [], "total": 0}

            lines = output.strip().split("\n") if output.strip() else []

            # 解析结果。git grep 对匹配行用 ":" 分隔，对上下文行用 "-" 分隔：
            #   匹配行：  ref:path:line:content
            #   上下文行：ref:path-line-content
            # 上下文行里的 path 可能含 "-"，所以借上一处匹配已知的 path 来切分。
            results = []
            truncated = False
            pending_context = []  # 出现在匹配行之前的上下文，先缓存再挂到下一个匹配
            last_file = None

            for line in lines:
                if not line or line == "--":
                    continue

                parts = line.split(":", 3)
                is_match = len(parts) >= 4 and parts[2].isdigit()

                if is_match:
                    if len(results) >= max_results:
                        truncated = True
                        break
                    last_file = parts[1]
                    item = {
                        "file": last_file,
                        "line": int(parts[2]),
                        "content": parts[3].strip()
                    }
                    if pending_context:
                        item["context"] = pending_context
                        pending_context = []
                    results.append(item)
                    continue

                if context <= 0:
                    continue

                # 上下文行：去掉 "ref:" 前缀后形如 "path-行号-内容"
                body = line.split(":", 1)[1] if ":" in line else ""
                ctx_match = re.match(r'^(.*?)-(\d+)-(.*)$', body)
                if not ctx_match:
                    continue

                ctx_file, ctx_line, ctx_text = ctx_match.groups()
                entry = f"{ctx_line}: {ctx_text}"
                # 属于当前匹配的后置上下文，否则是下一个匹配的前置上下文
                if results and ctx_file == results[-1]["file"] and \
                        int(ctx_line) > results[-1]["line"]:
                    results[-1].setdefault("context", []).append(entry)
                else:
                    pending_context.append(entry)

            logger.info(
                f"search_code: query='{query}', ref={ref}, strategy={strategy}, "
                f"results={len(results)}"
            )
            return {
                **meta,
                "results": results,
                "total": len(results),
                "truncated": truncated
            }
        except subprocess.CalledProcessError as e:
            # grep 未找到结果时返回 exit code 1
            if e.returncode == 1:
                logger.info(f"search_code: query='{query}', ref={ref}, no results")
                return {"query": query, "ref": ref, "results": [], "total": 0}
            logger.error(f"search_code 失败: {e}")
            return {"error": str(e), "results": []}
        except Exception as e:
            logger.error(f"search_code 异常: {e}")
            return {"error": str(e), "results": []}

    def find_files(
        self,
        name_pattern: str,
        ref: Optional[str] = None,
        max_results: int = 100
    ) -> Dict[str, Any]:
        """
        按文件名查找文件（git ls-files），返回完整路径。

        用于"只知道文件名、不知道在哪个目录"的场景。拿到完整路径后再配合
        read_file 或 search_code 的 path_filter 使用，避免反复 list_dir 试探。

        Args:
            name_pattern: 文件名或片段（如 "RecordList.vue" / "DataCenter"）
            ref: git 引用，为 None 时使用默认
            max_results: 最大返回数量

        Returns:
            匹配的文件路径列表
        """
        ref = ref or self.default_ref

        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}", "files": []}

        try:
            pattern = name_pattern.strip()
            # 两头加 * 以支持"文件名片段"匹配，且能跨目录层级
            if not pattern.startswith("*"):
                pattern = "*" + pattern
            if not pattern.endswith("*") and "." not in pattern.rsplit("/", 1)[-1]:
                pattern = pattern + "*"

            output = self._run_git_command(
                ["ls-files", f"--with-tree={ref}", "--", pattern]
            )
            files = [line.strip() for line in output.strip().split("\n") if line.strip()]

            logger.info(
                f"find_files: pattern='{name_pattern}', ref={ref}, files={len(files)}"
            )
            return {
                "name_pattern": name_pattern,
                "ref": ref,
                "files": files[:max_results],
                "total": len(files),
                "truncated": len(files) > max_results
            }
        except subprocess.CalledProcessError as e:
            logger.error(f"find_files 失败: {e}")
            return {"error": str(e), "files": []}
        except Exception as e:
            logger.error(f"find_files 异常: {e}")
            return {"error": str(e), "files": []}

    def read_file(
        self,
        path: str,
        ref: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        读取文件内容（git show）

        Args:
            path: 文件路径（相对于仓库根目录）
            ref: git 引用，为 None 时使用默认
            start_line: 起始行号（1-based，包含），为 None 时从第一行开始
            end_line: 结束行号（1-based，包含），为 None 时到最后一行

        Returns:
            文件内容字典
        """
        ref = ref or self.default_ref

        # 安全检查
        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}"}

        if not self._validate_path(path):
            return {"error": f"无效的路径: {path}"}

        try:
            output = self._run_git_command(["show", f"{ref}:{path}"])
            lines = output.split("\n")

            # 截取指定行范围
            if start_line is not None or end_line is not None:
                start = (start_line - 1) if start_line else 0
                end = end_line if end_line else len(lines)
                lines = lines[start:end]

            content = "\n".join(lines)
            logger.info(
                f"read_file: path={path}, ref={ref}, "
                f"lines={start_line or 1}-{end_line or len(lines)}, size={len(content)}"
            )

            return {
                "path": path,
                "ref": ref,
                "content": content,
                "start_line": start_line or 1,
                "end_line": end_line or len(lines),
                "total_lines": len(lines)
            }
        except subprocess.CalledProcessError as e:
            if "does not exist" in e.stderr or "Path" in e.stderr:
                logger.warning(f"read_file: 文件不存在: {path} @ {ref}")
                return {"error": f"文件不存在: {path}"}
            logger.error(f"read_file 失败: {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"read_file 异常: {e}")
            return {"error": str(e)}

    def list_dir(self, path: str = "", ref: Optional[str] = None) -> Dict[str, Any]:
        """
        列出目录内容（git ls-tree）

        Args:
            path: 目录路径（相对于仓库根目录），空字符串表示根目录
            ref: git 引用，为 None 时使用默认

        Returns:
            目录内容字典
        """
        ref = ref or self.default_ref

        # 安全检查
        if not self._validate_ref(ref):
            return {"error": f"无效的 ref: {ref}"}

        if path and not self._validate_path(path):
            return {"error": f"无效的路径: {path}"}

        try:
            target = f"{ref}:{path}" if path else ref
            output = self._run_git_command(["ls-tree", "--name-only", target])
            entries = [line.strip() for line in output.strip().split("\n") if line.strip()]

            logger.info(f"list_dir: path={path or '/'}, ref={ref}, entries={len(entries)}")
            return {
                "path": path or "/",
                "ref": ref,
                "entries": entries
            }
        except subprocess.CalledProcessError as e:
            if "not a tree" in e.stderr:
                return {"error": f"不是目录: {path}"}
            logger.error(f"list_dir 失败: {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"list_dir 异常: {e}")
            return {"error": str(e)}

    def run_git_command(self, args: List[str]) -> Dict[str, Any]:
        """
        执行任意只读 git 命令（白名单控制）

        Args:
            args: git 命令参数列表（不包括 'git' 本身），如 ['log', '--oneline', '-10']

        Returns:
            包含 output 和 error 的字典
        """
        # 只读命令白名单（排除所有写操作）
        readonly_commands = {
            'log', 'show', 'diff', 'blame', 'annotate',
            'ls-files', 'ls-tree', 'cat-file',
            'rev-parse', 'rev-list', 'describe',
            'branch', 'tag', 'reflog',
            'grep', 'status', 'remote',
        }

        if not args or args[0] not in readonly_commands:
            return {"error": f"不允许的命令: {args[0] if args else '(空)'}，仅支持只读命令: {', '.join(sorted(readonly_commands))}"}

        # 修正常见的参数冲突：-L 与 --follow 不兼容
        # git log -L <start>,<end>:<file> 本身就会追踪行的历史，不需要 --follow
        if args[0] == 'log' and any(arg.startswith('-L') for arg in args):
            if '--follow' in args:
                args = [a for a in args if a != '--follow']
                logger.info("git log -L 与 --follow 冲突，已自动去除 --follow")

        try:
            output = self._run_git_command(args, timeout=60)
            logger.info(f"run_git_command: {' '.join(args)}, output_lines={len(output.splitlines())}")
            return {
                "command": ' '.join(args),
                "output": output,
                "lines": len(output.splitlines())
            }
        except subprocess.CalledProcessError as e:
            logger.error(f"run_git_command 失败: {' '.join(args)}, stderr={e.stderr}")
            return {"error": f"命令执行失败: {e.stderr}", "command": ' '.join(args)}
        except Exception as e:
            logger.error(f"run_git_command 异常: {e}")
            return {"error": str(e), "command": ' '.join(args)}

    def fetch_updates(self) -> bool:
        """
        从远程拉取最新引用（定期调用以保持 ref 最新）

        Returns:
            是否成功
        """
        try:
            self._run_git_command(["fetch", "--all", "--tags", "--prune"], timeout=60)
            logger.info("Git fetch 完成")
            return True
        except Exception as e:
            logger.error(f"Git fetch 失败: {e}")
            return False


# Anthropic tool schema 定义（供 Provider 调用）
GIT_TOOLS_SCHEMA = [
    {
        "name": "run_git_command",
        "description": "执行只读 git 命令查询代码历史、变更、作者等信息。支持: log(查历史), blame(查作者), diff(查变更), show(查提交), describe(查版本), 等。示例: ['log', '--oneline', '-10', 'pom.xml'] 查最近10次提交; ['blame', '-L', '100,120', 'pom.xml'] 查100-120行作者; ['log', '-p', '--all', '--', 'pom.xml'] 查文件所有历史。",
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "git 命令参数列表（不含'git'本身）。只支持只读命令：log, show, diff, blame, annotate, ls-files, ls-tree, cat-file, rev-parse, rev-list, describe, branch, tag, reflog, grep, status, remote。"
                }
            },
            "required": ["args"]
        }
    },
    {
        "name": "list_refs",
        "description": "列出代码仓库的分支和标签，用于确认用户提到的版本号对应哪个分支或 tag。可选过滤模式匹配版本号。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "可选的过滤模式，如 '4.3' 会匹配包含 4.3 的分支/tag"
                }
            }
        }
    },
    {
        "name": "search_code",
        "description": "在指定分支/tag 的代码中搜索关键词（如异常类名、错误信息、函数名）。返回匹配的文件、行号和代码片段。支持常见正则写法（优先按 Perl 风格执行，环境不支持时自动转换或拆成关键词，结果里的 notice 会说明实际怎么搜的）；不区分大小写，不需要写 (?i)。用 context 参数可同时拿到匹配行周围的代码，省去再调 read_file。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的关键词，支持正则表达式（如 'class.*Service'）"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称（如 'release/4.3.6'），不指定则使用默认分支"
                },
                "path_filter": {
                    "type": "string",
                    "description": "可选的路径过滤。可用通配符（'*.java'）、目录（'src/main/java'）或直接给文件名（'RecordList.vue'，会自动匹配任意目录下的该文件）"
                },
                "context": {
                    "type": "integer",
                    "description": "每个匹配额外返回的上下文行数（如 5 表示前后各5行）。想看匹配点周围代码时用它，比再调一次 read_file 更快"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "find_files",
        "description": "按文件名（或文件名片段）查找文件的完整路径。当你只知道文件名、不知道它在哪个目录时用这个，不要用 list_dir 一层层试探。拿到完整路径后再 read_file 或作为 search_code 的 path_filter。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_pattern": {
                    "type": "string",
                    "description": "文件名或片段，如 'RecordList.vue'、'DataCenterService'"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称，不指定则使用默认分支"
                }
            },
            "required": ["name_pattern"]
        }
    },
    {
        "name": "read_file",
        "description": "读取指定分支/tag 中某个文件的内容。可以指定行号范围，用于查看异常抛出位置的上下文代码。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于仓库根目录）"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称，不指定则使用默认分支"
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（1-based），不指定则从第一行开始"
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（1-based），不指定则到最后一行"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_dir",
        "description": "列出指定分支/tag 中某个目录的内容，用于探索代码结构。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径（相对于仓库根目录），空字符串表示根目录"
                },
                "ref": {
                    "type": "string",
                    "description": "分支或 tag 名称，不指定则使用默认分支"
                }
            }
        }
    }
]
