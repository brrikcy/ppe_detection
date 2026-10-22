"""Start the PPE Inspector web app on http://127.0.0.1:8000 and open it in a browser.

    python run.py [--port 8000] [--no-browser]
"""

import argparse
import logging
import threading
import webbrowser

import uvicorn


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    print(f"PPE Inspector running at {url}  (Ctrl+C to stop)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("app.main:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
