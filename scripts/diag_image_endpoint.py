#!/usr/bin/env python3
"""
最简图片读取诊断：直接对 config.yaml 里配置的同一个端点(base_url/model/key)发一张图，
看模型能否读出图里的文字，从而判断"端点/图片格式"这一层到底能不能读图。

用法（在能跑通业务的同一环境/容器里）：
  python scripts/diag_image_endpoint.py <图片路径>
  # 不传路径则自动生成一张带 "DIAG-IMG-7788 CORS" 文字的测试图
"""
import sys
import base64
import mimetypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import anthropic  # noqa: E402
from ai_assistant.core.config import Config  # noqa: E402


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


def main():
    cfg = Config.load("config.yaml")
    base_url = cfg.ai_primary_base_url
    api_key = cfg.ai_primary_api_key
    model = cfg.ai_primary_model
    print(f"[i] 端点 base_url={base_url}")
    print(f"[i] model={model}  key={'<有>' if api_key else '<缺>'}")

    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else make_test_image()
    data = img_path.read_bytes()
    media = mimetypes.guess_type(str(img_path))[0] or "image/png"
    b64 = base64.b64encode(data).decode("utf-8")
    print(f"[i] 图片 {img_path}  size={len(data)}B  media={media}  base64_len={len(b64)}")

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=90)
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": "请逐字读出这张图片里的所有文字。如果你看不到图片，只回答：NO_IMAGE"},
            ],
        }],
    )
    text = "".join(getattr(b, "text", "") for b in resp.content)
    print(f"[i] input_tokens={resp.usage.input_tokens} output_tokens={resp.usage.output_tokens} stop={resp.stop_reason}")
    print("=" * 60)
    print("模型回复：")
    print(text)
    print("=" * 60)
    if "7788" in text or "NO_IMAGE" not in text.upper() and text.strip():
        if "7788" in text or "CORS" in text.upper():
            print("[结论] ✅ 端点能读图（模型回读出了图里的文字）")
        else:
            print("[结论] ⚠️ 模型有回复但没读出预期文字，请人工看上面回复内容判断")
    if "NO_IMAGE" in text.upper():
        print("[结论] ❌ 端点读不到图（模型明确回 NO_IMAGE）——问题在端点/图片格式这一层")


if __name__ == "__main__":
    main()
