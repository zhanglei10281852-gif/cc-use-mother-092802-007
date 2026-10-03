"""命令行入口：启动 HTTP 服务或对 JSON 文件做一次性试算。

用法：
  python -m grid_resilience.cli serve --host 127.0.0.1 --port 8000
  python -m grid_resilience.cli rank --input sample_plan.json
"""

import argparse
import json
import sys

from .codec import parse_weights
from .planning import evaluate
from .serialization import data_from_dict, result_to_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grid_resilience")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动排序 HTTP 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    rank = sub.add_parser("rank", help="对 JSON 输入做一次性排序试算")
    rank.add_argument("--input", required=True)

    args = parser.parse_args(argv)

    if args.command == "serve":
        from .app import make_server

        httpd = make_server(args.host, args.port)
        print(f"电网韧性排序服务监听 http://{args.host}:{args.port}", file=sys.stderr)
        httpd.serve_forever()
        return 0

    with open(args.input, encoding="utf-8") as fh:
        raw = json.load(fh)
    data = data_from_dict(raw.get("data", raw))
    result = evaluate(data, parse_weights(raw))
    print(json.dumps(result_to_dict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
