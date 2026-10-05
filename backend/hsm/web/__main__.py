"""`python -m hsm.web` (from backend/): one uvicorn process, one worker."""
import os

import uvicorn

from ..config import REPO_ROOT, load_dotenv

if __name__ == "__main__":
    load_dotenv(REPO_ROOT / ".env")  # so BACKEND_HOST/PORT can live in .env
    uvicorn.run(
        "hsm.web.app:create_app",
        factory=True,
        host=os.environ.get("BACKEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("BACKEND_PORT", "8000")),
        workers=1,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_level="info",
    )
