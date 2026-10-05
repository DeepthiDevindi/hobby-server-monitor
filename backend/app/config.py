from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader so python-dotenv is not a runtime dependency.

    Real environment variables (e.g. systemd EnvironmentFile) always win.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Allowlisted images: UI key -> LXD image source. Nothing else can be created.
IMAGES: dict[str, dict[str, str]] = {
    "ubuntu/24.04": {
        "server": "https://cloud-images.ubuntu.com/releases",
        "protocol": "simplestreams",
        "alias": "24.04",
    },
    "ubuntu/22.04": {
        "server": "https://cloud-images.ubuntu.com/releases",
        "protocol": "simplestreams",
        "alias": "22.04",
    },
    "debian/12": {
        "server": "https://images.lxd.canonical.com",
        "protocol": "simplestreams",
        "alias": "debian/12",
    },
}


@dataclass(frozen=True)
class Settings:
    secret_key: str
    google_client_id: str = ""
    google_client_secret: str = ""
    bootstrap_admin_email: str = ""
    cookie_secure: bool = True
    public_origin: str = "http://localhost:8000"
    database_path: str = str(ROOT / "data" / "monitor.db")
    lxd_socket: str = "/var/snap/lxd/common/lxd/unix.socket"
    frontend_dir: str = str(ROOT / "frontend" / "dist")
    session_hours: float = 8.0
    metrics_interval: float = 4.0
    terminal_idle_seconds: int = 900
    terminal_max_per_user: int = 2
    terminal_max_total: int = 8
    max_cpus: int = os.cpu_count() or 1
    max_memory_mib: int = 4096
    images: dict[str, dict[str, str]] = field(default_factory=lambda: dict(IMAGES))

    @classmethod
    def from_env(cls, env_file: Path = ROOT / ".env") -> "Settings":
        _load_dotenv(env_file)
        env = os.environ.get
        secret = env("SECRET_KEY", "")
        if len(secret) < 32:
            raise RuntimeError("SECRET_KEY must be set to at least 32 random characters (see .env.example)")
        return cls(
            secret_key=secret,
            google_client_id=env("GOOGLE_CLIENT_ID", ""),
            google_client_secret=env("GOOGLE_CLIENT_SECRET", ""),
            bootstrap_admin_email=env("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower(),
            cookie_secure=_bool(env("COOKIE_SECURE", "true")),
            public_origin=env("PUBLIC_ORIGIN", cls.public_origin).rstrip("/"),
            database_path=env("DATABASE_PATH", cls.database_path),
            lxd_socket=env("LXD_SOCKET", cls.lxd_socket),
            frontend_dir=env("FRONTEND_DIR", cls.frontend_dir),
            session_hours=float(env("SESSION_HOURS", "8")),
            metrics_interval=min(5.0, max(3.0, float(env("METRICS_INTERVAL", "4")))),
            terminal_idle_seconds=int(env("TERMINAL_IDLE_SECONDS", "900")),
            terminal_max_per_user=int(env("TERMINAL_MAX_PER_USER", "2")),
            terminal_max_total=int(env("TERMINAL_MAX_TOTAL", "8")),
            max_memory_mib=int(env("MAX_MEMORY_MIB", "4096")),
        )
