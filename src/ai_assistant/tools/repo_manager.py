"""
多仓库管理器

管理多个 GitTools 实例的生命周期、切换和后台定期 fetch。
支持并发安全：切换操作由 threading.Lock 保护。
支持多种认证模式：none / https / ssh。
"""

import subprocess
import threading
import time
from typing import List, Dict, Optional
from urllib.parse import urlparse, urlunparse, quote

from loguru import logger

from ai_assistant.tools.git_tools import GitTools


class RepoManager:
    """多仓库管理器，管理多个 GitTools 实例"""

    def __init__(self, repositories: List):
        """
        初始化多仓库管理器

        Args:
            repositories: 仓库配置列表，每个元素需具备
                name / repo_path / default_ref / description 属性（鸭子类型）
        """
        self._lock = threading.Lock()
        self._repos: Dict[str, GitTools] = {}
        self._repo_configs: Dict[str, object] = {}  # 存储原始 repo_config 用于认证 fetch
        self._descriptions: Dict[str, str] = {}
        self._default_refs: Dict[str, str] = {}
        self._order: List[str] = []
        self._current_name: Optional[str] = None

        for repo in repositories:
            try:
                git_tools = GitTools(repo.repo_path, repo.default_ref)
            except Exception as e:
                logger.warning(f"仓库初始化失败，已跳过: name={repo.name}, error={e}")
                continue

            self._repos[repo.name] = git_tools
            self._repo_configs[repo.name] = repo
            self._descriptions[repo.name] = repo.description
            self._default_refs[repo.name] = repo.default_ref
            self._order.append(repo.name)

        if self._order:
            self._current_name = self._order[0]
            logger.info(f"RepoManager 初始化完成: 仓库数={len(self._order)}, 默认激活={self._current_name}")
        else:
            logger.warning("RepoManager 初始化完成: 无可用仓库")

    def switch_repo(self, name: str) -> str:
        """
        切换当前活跃仓库

        Args:
            name: 仓库标识名

        Returns:
            确认信息；若仓库不存在则返回错误信息（不抛异常）
        """
        with self._lock:
            if name not in self._repos:
                available = ", ".join(self._order) if self._order else "（无）"
                logger.warning(f"switch_repo 失败: 仓库不存在 name={name}")
                return f"错误：仓库 '{name}' 不存在。可用仓库: {available}"

            self._current_name = name
            logger.info(f"已切换活跃仓库: {name}")
            return f"已切换到仓库: {name} ({self._descriptions[name]})"

    @property
    def current(self) -> Optional[GitTools]:
        """获取当前活跃的 GitTools 实例"""
        with self._lock:
            if self._current_name is None:
                return None
            return self._repos[self._current_name]

    @property
    def current_repo_name(self) -> str:
        """获取当前活跃仓库名"""
        with self._lock:
            return self._current_name or ""

    def get_repo_descriptions(self) -> str:
        """
        生成仓库描述文本（用于注入 system prompt）

        Returns:
            仓库列表及当前活跃仓库的描述文本
        """
        with self._lock:
            if not self._order:
                return "当前没有可用的代码仓库。"

            lines = ["可用代码仓库："]
            for name in self._order:
                lines.append(
                    f"- {name}: {self._descriptions[name]} (默认分支: {self._default_refs[name]})"
                )
            lines.append("")
            lines.append(f"当前活跃仓库: {self._current_name}")
            lines.append("使用 switch_repo 工具切换到其他仓库。")
            return "\n".join(lines)

    def list_repos(self) -> List[Dict[str, str]]:
        """
        返回仓库列表摘要

        Returns:
            形如 [{"name": ..., "description": ..., "active": True/False}, ...]
        """
        with self._lock:
            return [
                {
                    "name": name,
                    "description": self._descriptions[name],
                    "active": name == self._current_name,
                }
                for name in self._order
            ]

    def _fetch_repo(self, repo_config, git_tools: GitTools):
        """
        根据认证模式 fetch 单个仓库

        Args:
            repo_config: 仓库配置对象（含 auth_mode 等字段）
            git_tools: 对应的 GitTools 实例
        """
        repo_path = repo_config.repo_path
        auth_mode = getattr(repo_config, "auth_mode", "none") or "none"

        if auth_mode == "https" and getattr(repo_config, "auth_username", "") and getattr(repo_config, "auth_password", ""):
            self._fetch_with_https(repo_config, repo_path)
        elif auth_mode == "ssh" and getattr(repo_config, "auth_ssh_key", ""):
            self._fetch_with_ssh(repo_config, repo_path)
        else:
            # 默认模式：直接 git fetch --all
            git_tools.fetch_updates()

    def _fetch_with_https(self, repo_config, repo_path: str):
        """
        HTTPS 认证模式 fetch

        从 remote origin URL 解析地址，拼接用户名密码后执行 fetch。
        """
        try:
            # 获取当前 remote origin URL
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            origin_url = result.stdout.strip()

            # 解析 URL 并拼接认证信息
            parsed = urlparse(origin_url)

            # 对用户名和密码进行 URL 编码（处理特殊字符）
            encoded_user = quote(repo_config.auth_username, safe="")
            encoded_pass = quote(repo_config.auth_password, safe="")

            # 构建带认证的 URL
            auth_url = urlunparse((
                parsed.scheme or "https",
                f"{encoded_user}:{encoded_pass}@{parsed.hostname}" + (f":{parsed.port}" if parsed.port else ""),
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            ))

            # 执行 fetch（使用带认证的 URL，不用 --all）
            subprocess.run(
                ["git", "fetch", auth_url, "--tags", "--prune"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            logger.info(f"HTTPS 认证 fetch 完成: repo={repo_config.name}, user={repo_config.auth_username}")

        except subprocess.CalledProcessError as e:
            # 日志中不打印含密码的 URL
            logger.warning(
                f"HTTPS 认证 fetch 失败: repo={repo_config.name}, "
                f"user={repo_config.auth_username}, "
                f"可能原因: 认证失败/网络问题/仓库地址错误, "
                f"stderr={e.stderr[:200] if e.stderr else 'N/A'}"
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"HTTPS 认证 fetch 超时: repo={repo_config.name}")
        except Exception as e:
            logger.warning(f"HTTPS 认证 fetch 异常: repo={repo_config.name}, error={e}")

    def _fetch_with_ssh(self, repo_config, repo_path: str):
        """
        SSH 认证模式 fetch

        通过 GIT_SSH_COMMAND 环境变量指定私钥后执行 fetch。
        """
        try:
            import os
            env = os.environ.copy()
            env["GIT_SSH_COMMAND"] = f"ssh -i {repo_config.auth_ssh_key} -o StrictHostKeyChecking=no"

            subprocess.run(
                ["git", "fetch", "--all", "--tags", "--prune"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
                env=env,
            )
            logger.info(f"SSH 认证 fetch 完成: repo={repo_config.name}, key={repo_config.auth_ssh_key}")

        except subprocess.CalledProcessError as e:
            logger.warning(
                f"SSH 认证 fetch 失败: repo={repo_config.name}, "
                f"key={repo_config.auth_ssh_key}, "
                f"可能原因: 私钥无权限/网络问题/仓库地址错误, "
                f"stderr={e.stderr[:200] if e.stderr else 'N/A'}"
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"SSH 认证 fetch 超时: repo={repo_config.name}")
        except Exception as e:
            logger.warning(f"SSH 认证 fetch 异常: repo={repo_config.name}, error={e}")

    def start_background_fetch(self, interval: int = 1800):
        """
        启动后台定期 fetch 所有仓库（daemon 线程）

        Args:
            interval: fetch 间隔（秒），默认 1800（30 分钟）
        """
        if not self._repos:
            logger.warning("无可用仓库，跳过后台 fetch 启动")
            return

        def _fetch_loop():
            while True:
                time.sleep(interval)
                # 复制快照，避免遍历期间持锁执行耗时的 fetch
                with self._lock:
                    items = [(name, self._repo_configs[name], self._repos[name]) for name in self._order]
                for name, repo_config, git_tools in items:
                    try:
                        self._fetch_repo(repo_config, git_tools)
                        logger.info(f"后台 fetch 完成: {name}")
                    except Exception as e:
                        logger.error(f"后台 fetch 失败: name={name}, error={e}")

        thread = threading.Thread(target=_fetch_loop, daemon=True, name="repo-bg-fetch")
        thread.start()
        logger.info(f"后台 fetch 线程已启动: interval={interval}s, 仓库数={len(self._repos)}")
