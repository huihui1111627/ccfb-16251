from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from .benchmark import run_benchmark_run
from .dashboard import LazyDashboardState, create_lazy_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m psdb",
        description="参数敏感计划缓存：解析、直方图代价优化器、执行器、分档缓存与可视化",
    )
    sub = parser.add_subparsers(dest="command")

    benchmark = sub.add_parser("benchmark", help="运行幂律商户负载对比基准")
    benchmark.add_argument("--calls", type=int, default=720)
    benchmark.add_argument("--cache-entries", type=int, default=5)
    benchmark.add_argument("--seed", type=int, default=16251)
    benchmark.add_argument("--out", type=str, default="benchmark-report.json")

    web = sub.add_parser("web", help="运行基准并启动可视化页面")
    web.add_argument("--calls", type=int, default=720)
    web.add_argument("--cache-entries", type=int, default=5)
    web.add_argument("--seed", type=int, default=16251)
    web.add_argument("--host", type=str, default="127.0.0.1")
    web.add_argument("--port", type=int, default=8766)

    demo = sub.add_parser("demo", help="web 的别名")
    demo.add_argument("--calls", type=int, default=720)
    demo.add_argument("--cache-entries", type=int, default=5)
    demo.add_argument("--seed", type=int, default=16251)
    demo.add_argument("--host", type=str, default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8766)
    return parser


def run_benchmark_command(args: argparse.Namespace) -> int:
    print(f"[1/2] 生成幂律商户/订单数据并执行 {args.calls} 次混合调用...", flush=True)
    result = run_benchmark_run(
        calls=args.calls,
        max_cache_entries=args.cache_entries,
        seed=args.seed,
    )
    output = Path(args.out)
    output.write_text(
        json.dumps(result.report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    comp = result.report["comparison"]
    print(f"[2/2] 报告已写入 {output}", flush=True)
    print(json.dumps(comp, ensure_ascii=False, indent=2), flush=True)
    return 0


def run_web_command(args: argparse.Namespace) -> int:
    lazy_state = LazyDashboardState()
    server = create_lazy_server(lazy_state, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"Dashboard starting at {url}", flush=True)
    print(f"正在后台构造数据库并运行 {args.calls} 次请求的对照基准...", flush=True)

    def benchmark_worker() -> None:
        try:
            result = run_benchmark_run(
                calls=args.calls,
                max_cache_entries=args.cache_entries,
                seed=args.seed,
            )
            lazy_state.bind(result)
            print("基准完成，页面数据已就绪。", flush=True)
        except Exception as exc:  # pragma: no cover - startup failure path
            lazy_state.status = "error"
            lazy_state.error = f"{type(exc).__name__}: {exc}"
            print(lazy_state.error, file=sys.stderr, flush=True)

    thread = threading.Thread(target=benchmark_worker, daemon=True)
    thread.start()

    print("按 Ctrl+C 停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...", flush=True)
    finally:
        server.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "web", "demo"}:
        if args.command is None:
            # Default web/demo options.
            args = parser.parse_args(["web"])
        return run_web_command(args)
    if args.command == "benchmark":
        return run_benchmark_command(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
