"""Convenience entrypoint: `python backend/main.py` (single worker, by design)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:get_app",
        factory=True,
        app_dir=os.path.dirname(os.path.abspath(__file__)),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        workers=1,
        proxy_headers=False,
        server_header=False,
        log_level="info",
    )
