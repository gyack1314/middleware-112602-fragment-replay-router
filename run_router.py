"""回放消息路由器命令行：读取一段演练 JSONL，逐条输出路由结果。

用法：

    python3 run_router.py examples/late-directory-rewrite.jsonl
    python3 run_router.py examples/same-sequence-two-runs.jsonl --state /tmp/rr

不指定 --state 时只在内存中处理；指定后输入消息会追加到本地日志，
后续可以用 --recover 从该日志重建分片保管与回放进度。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# -I 隔离模式不自动加入脚本目录，这里显式登记以便导入同目录模块。
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from replay_router import JournalStore, Router  # noqa: E402
from scenario_reader import iter_messages  # noqa: E402

SUPPORTED = set(
    json.loads((ROOT / "examples" / "message_types.json").read_text(
        encoding="utf-8"))
)


def route_file(path, store=None):
    router = Router(store=store)
    rows = list(iter_messages(Path(path), SUPPORTED))
    output = []
    for row in rows:
        for event in router.handle(row):
            output.append(event)
    return router, output


def main():
    parser = argparse.ArgumentParser(description="训练回放消息路由器")
    parser.add_argument("scenario", type=Path, help="演练 JSONL 文件")
    parser.add_argument(
        "--state", type=Path, default=None,
        help="本地状态目录：输入消息追加保存，用于重启恢复",
    )
    parser.add_argument(
        "--recover", action="store_true",
        help="不读场景文件，直接从 --state 的日志重放恢复",
    )
    args = parser.parse_args()

    if args.recover:
        if args.state is None:
            parser.error("--recover 需要同时指定 --state")
        rows = JournalStore(args.state / "journal.jsonl").rows()
        router = Router.replay_journal(rows)
        print(json.dumps({
            "kind": "recovery_report",
            "requests": len(router.requests),
            "fragments": len(router.fragments),
        }, ensure_ascii=False))
        return

    store = None
    if args.state is not None:
        store = JournalStore(args.state / "journal.jsonl")
    router, output = route_file(args.scenario, store=store)
    for event in output:
        print(json.dumps(event, ensure_ascii=False))
    print(json.dumps({
        "kind": "run_summary",
        "requests": len(router.requests),
        "fragments": len(router.fragments),
        "held_fragments": sum(
            1 for fragment in router.fragments.values()
            if fragment.state == "held"
        ),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
