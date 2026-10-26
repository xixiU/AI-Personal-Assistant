"""
富文本(post)消息的文件附件解析测试

背景 bug：用户在飞书发送 .log 文件或含日志的 .zip 时，通常是「文件 + 文字说明」
一起发，飞书会把这条消息打包成富文本(post)类型，附件以 media/file 标签内嵌在
content 里。而 _parse_post_content 当时只返回 (text, image_keys)，递归 walk()
只认 text/md/a 和 img 标签，完全没有处理 media/file 标签，于是富文本里的日志
附件被静默丢弃，机器人读不到日志内容，没法用日志排查问题。

复现步骤：
1. 在飞书里给机器人发一条消息：同时附带 app.log 文件 + 文字「帮我看下这个日志」
   （拖入文件后再输入文字，飞书合并为一条富文本消息）
2. 观察日志：出现 "解析富文本消息: text_len=11, images=0"，files 相关信息完全没有
3. AI 回复里对日志内容一无所知，也没有进入代码排查(Agentic)模式
4. 对比：单独只发 app.log（纯 file 类型消息）→ 能正常读到日志（分支实现不一致）

核心规则（回归重点）：
1. media 和 file 两种 tag 都要认（飞书不同版本/场景用法不一致）
2. 兼容有语言包裹(zh_cn/en_us)与无语言包裹(私有化部署)两种结构
3. 任意嵌套深度都要能找到附件（content 可能是二维数组或更深嵌套）
4. 返回值固定为三元组 (text, image_keys, files)，无文件时 files 为空列表，
   调用方按解包三个值使用（改签名时务必同步所有调用点）
"""
import json
import sys
import types
from unittest.mock import Mock

# 运行时依赖可能未安装（本项目按规范只做静态测试），注入最小桩模块，
# 保证被测的富文本解析逻辑可独立测试。
for _name in ("anthropic", "paramiko", "pywinauto", "jieba"):
    if _name not in sys.modules:
        _stub = types.ModuleType(_name)
        _stub.__getattr__ = lambda _attr: Mock()  # type: ignore[attr-defined]
        sys.modules[_name] = _stub

if "cryptography" not in sys.modules:
    _crypto = types.ModuleType("cryptography")
    _fernet = types.ModuleType("fernet")
    _fernet.Fernet = Mock
    _crypto.fernet = _fernet
    sys.modules["cryptography"] = _crypto
    sys.modules["cryptography.fernet"] = _fernet

from ai_assistant.main import AIAssistant


def _parse(content_data: dict) -> tuple:
    """调用被测函数（静态方法，无需实例）"""
    return AIAssistant._parse_post_content(content_data)


class TestReturnsTriple:
    """返回值契约：固定三元组，调用方按三个值解包"""

    def test_returns_three_values(self):
        text, image_keys, files = _parse({
            "zh_cn": {"content": [[{"tag": "text", "text": "你好"}]]}
        })
        assert text == "你好"
        assert image_keys == []
        assert files == []

    def test_unparseable_structure_returns_empty_triple(self):
        """定位不到 content 数组时也必须是三元组，不能是二元组"""
        assert _parse({"unknown": 123}) == ("", [], [])

    def test_no_file_returns_empty_list(self):
        """无附件时 files 为空列表（下游用 `or []` 消费，空列表与缺键均安全）"""
        _, _, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "text", "text": "纯文字提问"},
                {"tag": "img", "image_key": "img_v3_only"},
            ]]}
        })
        assert files == []


class TestFileTag:
    """file 标签（原始 bug 所在）"""

    def test_file_tag_extracted(self):
        text, image_keys, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "text", "text": "帮我看下这个日志"},
                {"tag": "file", "file_key": "file_v3_abc", "file_name": "app.log"},
            ]]}
        })
        assert text == "帮我看下这个日志"
        assert image_keys == []
        assert files == [{"file_key": "file_v3_abc", "file_name": "app.log"}]

    def test_file_without_name_falls_back(self):
        """飞书偶发不带 file_name，用兜底名而不是丢弃附件"""
        _, _, files = _parse({
            "zh_cn": {"content": [[{"tag": "file", "file_key": "file_v3_noname"}]]}
        })
        assert files == [{"file_key": "file_v3_noname", "file_name": "attachment"}]

    def test_file_without_key_skipped(self):
        """没有 file_key 无法下载，必须跳过而不是产出空 key 条目"""
        _, _, files = _parse({
            "zh_cn": {"content": [[{"tag": "file", "file_name": "app.log"}]]}
        })
        assert files == []

    def test_file_only_no_text(self):
        """只有附件没有文字说明：text 为空，files 仍要拿到"""
        text, _, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "file", "file_key": "file_v3_lone", "file_name": "err.log"},
            ]]}
        })
        assert text == ""
        assert len(files) == 1


class TestMediaTag:
    """media 标签（飞书另一种附件 tag 写法）"""

    def test_media_tag_extracted(self):
        _, _, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "media", "file_key": "file_v3_zip", "file_name": "logs.zip"},
            ]]}
        })
        assert files == [{"file_key": "file_v3_zip", "file_name": "logs.zip"}]

    def test_media_and_file_tags_both_collected(self):
        """两种 tag 混用时都要收集，顺序保持出现顺序"""
        _, _, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "media", "file_key": "k1", "file_name": "a.log"},
                {"tag": "file", "file_key": "k2", "file_name": "b.zip"},
            ]]}
        })
        assert [f["file_key"] for f in files] == ["k1", "k2"]


class TestLanguageWrapper:
    """语言包裹差异：云上带 zh_cn/en_us，私有化部署可能没有"""

    def test_zh_cn_wrapper(self):
        text, _, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "text", "text": "中文包裹"},
                {"tag": "file", "file_key": "k", "file_name": "a.log"},
            ]]}
        })
        assert text == "中文包裹"
        assert len(files) == 1

    def test_en_us_wrapper(self):
        text, _, files = _parse({
            "en_us": {"content": [[
                {"tag": "text", "text": "english wrapper"},
                {"tag": "media", "file_key": "k", "file_name": "a.log"},
            ]]}
        })
        assert text == "english wrapper"
        assert len(files) == 1

    def test_no_language_wrapper(self):
        """私有化部署：content 直接在顶层"""
        text, _, files = _parse({
            "content": [[
                {"tag": "text", "text": "无语言包裹"},
                {"tag": "file", "file_key": "k", "file_name": "svc.log"},
            ]]
        })
        assert text == "无语言包裹"
        assert files == [{"file_key": "k", "file_name": "svc.log"}]

    def test_unknown_language_key_fallback(self):
        """未知语言键（如 ja_jp）：兜底取第一个含 content 的字典字段"""
        text, _, files = _parse({
            "ja_jp": {"content": [[
                {"tag": "text", "text": "fallback"},
                {"tag": "file", "file_key": "k", "file_name": "x.log"},
            ]]}
        })
        assert text == "fallback"
        assert len(files) == 1


class TestMixedContent:
    """file + img + text 混合（真实"文件+截图+说明"场景）"""

    def test_file_img_text_mixed(self):
        text, image_keys, files = _parse({
            "zh_cn": {"content": [
                [{"tag": "text", "text": "服务启动失败"}],
                [{"tag": "img", "image_key": "img_v3_shot"}],
                [{"tag": "file", "file_key": "file_v3_log", "file_name": "boot.log"}],
                [{"tag": "text", "text": "日志和截图都在这"}],
            ]}
        })
        assert text == "服务启动失败 日志和截图都在这"
        assert image_keys == ["img_v3_shot"]
        assert files == [{"file_key": "file_v3_log", "file_name": "boot.log"}]

    def test_multiple_files_all_collected(self):
        """多附件：全部收集，_collect_log_files 支持列表批量处理"""
        _, _, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "file", "file_key": "k1", "file_name": "a.log"},
                {"tag": "file", "file_key": "k2", "file_name": "b.log"},
                {"tag": "media", "file_key": "k3", "file_name": "c.zip"},
            ]]}
        })
        assert len(files) == 3
        assert [f["file_name"] for f in files] == ["a.log", "b.log", "c.zip"]

    def test_at_placeholder_stripped_file_kept(self):
        """@机器人 占位符会被清掉，但不能影响附件提取"""
        text, _, files = _parse({
            "zh_cn": {"content": [[
                {"tag": "at", "user_id": "ou_bot", "text": "@_user_1"},
                {"tag": "text", "text": "看下日志"},
                {"tag": "file", "file_key": "k", "file_name": "a.log"},
            ]]}
        })
        assert "@_user_1" not in text
        assert text == "看下日志"
        assert len(files) == 1


class TestNestedStructure:
    """深层嵌套：content 可能不止二维"""

    def test_deeply_nested_file(self):
        text, image_keys, files = _parse({
            "zh_cn": {"content": [
                [[[{"tag": "file", "file_key": "deep_k", "file_name": "deep.log"}]]],
                [{"tag": "text", "text": "深层嵌套"}],
            ]}
        })
        assert text == "深层嵌套"
        assert image_keys == []
        assert files == [{"file_key": "deep_k", "file_name": "deep.log"}]

    def test_file_inside_node_children(self):
        """附件挂在节点的 content 子字段里（walk 会递归 children）"""
        _, _, files = _parse({
            "zh_cn": {"content": [[
                {
                    "tag": "div",
                    "content": [
                        {"tag": "text", "text": "外层"},
                        {"tag": "media", "file_key": "child_k", "file_name": "child.log"},
                    ],
                },
            ]]}
        })
        assert files == [{"file_key": "child_k", "file_name": "child.log"}]

    def test_non_dict_elements_ignored(self):
        """脏数据（字符串/None 混在数组里）不能抛异常"""
        _, _, files = _parse({
            "zh_cn": {"content": [[
                "脏字符串",
                None,
                {"tag": "file", "file_key": "k", "file_name": "a.log"},
            ]]}
        })
        assert len(files) == 1


class TestCallSiteContract:
    """调用点契约：json 原文 → 解析，与 webhook 实际路径一致"""

    def test_from_json_string_like_webhook(self):
        """webhook 拿到的是 content 字符串，反序列化后解析"""
        content_str = json.dumps({
            "zh_cn": {"content": [[
                {"tag": "text", "text": "这个日志报错了"},
                {"tag": "file", "file_key": "file_v3_real", "file_name": "ts-service.log"},
            ]]}
        }, ensure_ascii=False)

        text, image_keys, files = _parse(json.loads(content_str))
        assert text == "这个日志报错了"
        assert image_keys == []
        assert files[0]["file_name"] == "ts-service.log"
