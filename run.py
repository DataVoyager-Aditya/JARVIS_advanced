"""
JARVIS_advanced launcher. Starts the FastAPI backend (voice endpoints in Phase 1).

    python run.py            # serve on 127.0.0.1:8000
    python run.py --reload   # dev autoreload
"""

import argparse
import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(description="Run the JARVIS backend")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
