"""启动规划服务：python -m grid_resilience --host 127.0.0.1 --port 8080"""

import argparse

from .service import ResilienceService, create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="电网韧性项目排序服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = create_server(ResilienceService(), host=args.host, port=args.port)
    print(f"电网韧性规划服务运行于 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
