"""Start the inference flowchart app (see inference_trace/main.py).

    python run_trace.py [--port 8100] [--no-browser]
"""

import argparse
import logging
import threading
import webbrowser

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(description="PPE inference flowchart")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("inference_trace.main:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
