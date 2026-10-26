#!/usr/bin/env python3
"""统计 chat_history 目录下每个 jsonl 文件的问题数（即记录条数）。"""
import json
import sys
from pathlib import Path


def count_questions(jsonl_path: Path) -> tuple[int, int]:
    """返回 (问题数, 解析失败的行数)。"""
    total = 0
    bad = 0
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if "query" in obj:
                total += 1
            else:
                bad += 1
    return total, bad


def main():
    default_dir = Path(__file__).parent.parent / "data" / "chat_history"
    target_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else default_dir
    jsonl_files = sorted(target_dir.glob("*.jsonl"))

    if not jsonl_files:
        print(f"未在 {target_dir} 下找到 jsonl 文件")
        return

    grand_total = 0
    rows = []
    for fp in jsonl_files:
        count, bad = count_questions(fp)
        grand_total += count
        rows.append((fp.name, count, bad))

    name_width = max(len(r[0]) for r in rows)
    print(f"{'文件名'.ljust(name_width)}  问题数  异常行数")
    for name, count, bad in rows:
        print(f"{name.ljust(name_width)}  {count:>4}  {bad:>4}")

    print("-" * (name_width + 20))
    print(f"文件总数: {len(rows)}")
    print(f"问题总数: {grand_total}")


if __name__ == "__main__":
    main()
