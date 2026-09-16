#!/usr/bin/env python3
"""
图片读取变量隔离诊断：对 config.yaml 里同一个端点(base_url/model/key)，用同一张图
依次发 3 种请求，逐个加变量，定位"实际业务流读不到图"到底被哪个变量触发。

  用例 A: 图 + 短提问                      —— 纯净基线（已知能读图）
  用例 B: 图 + 超长 system prompt          —— 加长系统提示
  用例 C: 图 + 超长 system + tools         —— 再加 tools（复刻实际 agentic 请求形态）

判读（看每个用例的 [判读] 行）：
  - A 能读、C 读不到  → 是 tools（或长system+tools组合）触发网关丢图
  - A 能读、B 就读不到 → 是超长 system 触发
  - 三个都能读        → 端点无关，问题在业务代码更细的分支，回代码里继续查
  能否读图的判据：模型是否回读出图中文字（CORS / Access-Control / 版本号等），
  或是否直接回 NO_IMAGE。

用法（在能跑通业务的同一容器/环境里）：
  python scripts/diag_image_endpoint.py <图片路径>
  # 不传路径则生成一张带 "DIAG-IMG-7788 CORS ERROR" 文字的测试图
"""
import sys
import base64
import mimetypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import anthropic  # noqa: E402
from ai_assistant.core.config import Config  # noqa: E402

PROBE = "请逐字读出这张图片里的所有文字（尤其任何报错、编号、版本号）。如果你看不到图片，只回答：NO_IMAGE"

# 复刻 agentic 那段超长系统提示的规模（不必逐字一致，够长即可暴露长度相关问题）
LONG_SYSTEM = (
    "你是一个智能代码排查助手，帮助用户分析日志并定位代码问题。请先仔细阅读用户提供的截图，"
    "逐字读出图中出现的报错文字，再结合代码给出定位。\n" * 25
)

TOOLS = [{
    "name": "search_code",
    "description": "在代码仓库中按关键字搜索代码",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "搜索关键字"}},
        "required": ["query"],
    },
}]


def make_test_image() -> Path:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (480, 160), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 470, 150], outline="red", width=4)
    d.text((30, 70), "DIAG-IMG-7788  CORS ERROR", fill="black")
    out = Path("scripts/_diag_test_image.png")
    img.save(out)
    print(f"[i] 已生成测试图: {out}（文字: DIAG-IMG-7788 CORS ERROR）")
    return out


def run_case(client, model, name, source, *, system=None, tools=None):
    kwargs = dict(
        model=model,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": source},
                {"type": "text", "text": PROBE},
            ],
        }],
    )
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    try:
        resp = client.messages.create(**kwargs)
    except Exception as e:
        print(f"[用例 {name}] ✘ 请求异常: {e}")
        return
    text = "".join(getattr(b, "text", "") for b in resp.content)
    up = text.upper()
    read_ok = ("7788" in up) or ("CORS" in up) or ("ACCESS-CONTROL" in up) or ("4.3" in text)
    verdict = "✅ 读到图" if read_ok else ("❌ 没读到图(NO_IMAGE)" if "NO_IMAGE" in up else "⚠️ 未读出预期文字,人工看回复")
    print(f"[用例 {name}] stop={resp.stop_reason} input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens}")
    print(f"[用例 {name}] 判读: {verdict}")
    print(f"[用例 {name}] 回复前 200 字: {text[:200]!r}")
    print("-" * 60)


def main():
    cfg = Config.load("config.yaml")
    print(f"[i] 端点 base_url={cfg.ai_primary_base_url}")
    print(f"[i] model={cfg.ai_primary_model}  key={'<有>' if cfg.ai_primary_api_key else '<缺>'}")

    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else make_test_image()
    data = img_path.read_bytes()
    media = mimetypes.guess_type(str(img_path))[0] or "image/png"
    b64 = base64.b64encode(data).decode("utf-8")
    source = {"type": "base64", "media_type": media, "data": b64}
    print(f"[i] 图片 {img_path}  size={len(data)}B  media={media}  base64_len={len(b64)}")
    print("=" * 60)

    client = anthropic.Anthropic(api_key=cfg.ai_primary_api_key, base_url=cfg.ai_primary_base_url, timeout=90)
    run_case(client, cfg.ai_primary_model, "A 图+短提问", source)
    run_case(client, cfg.ai_primary_model, "B 图+长system", source, system=LONG_SYSTEM)
    run_case(client, cfg.ai_primary_model, "C 图+长system+tools", source, system=LONG_SYSTEM, tools=TOOLS)
    print("[i] 完成。对照顶部『判读』说明：哪个用例开始读不到图，就是那个变量触发的。")


if __name__ == "__main__":
    main()
