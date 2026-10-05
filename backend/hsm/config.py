"""Settings, read once from the environment (or a .env file for development).

Variable names follow the task template's .env.example. Both processes (web
API and collector) use this module, so they always agree on paths.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> None:
    """Tiny .env reader (avoids python-dotenv). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in "\"'" and value[:1] and value.count(value[0]) >= 2:
            value = value[1:value.index(value[0], 1)]  # quoted: keep as-is
        else:
            value = value.split(" #", 1)[0].strip()  # unquoted: drop inline comment
        os.environ.setdefault(key.strip(), value)


def _path(value: str) -> str:
    """Relative paths in .env are relative to the repository root, so it does
    not matter which directory a process was started from."""
    p = Path(value)
    return str(p if p.is_absolute() else (REPO_ROOT / p).resolve())


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Images an admin may create containers from: UI label -> LXD image source.
# An allowlist (not free text) so nobody can pull an arbitrary remote image.
IMAGES: dict[str, dict[str, str]] = {
    "ubuntu/24.04": {"server": "https://cloud-images.ubuntu.com/releases", "protocol": "simplestreams", "alias": "24.04"},
    "ubuntu/22.04": {"server": "https://cloud-images.ubuntu.com/releases", "protocol": "simplestreams", "alias": "22.04"},
    "debian/12": {"server": "https://images.lxd.canonical.com", "protocol": "simplestreams", "alias": "debian/12"},
    "alpine/3.20": {"server": "https://images.lxd.canonical.com", "protocol": "simplestreams", "alias": "alpine/3.20"},
}


@dataclass(frozen=True)
class Settings:
    session_secret: str
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    bootstrap_admin_email: str = ""
    cookie_secure: bool = True
    public_origin: str = "http://localhost:8000"
    sqlite_path: str = str(REPO_ROOT / "data" / "app.db")
    tinyflux_dir: str = str(REPO_ROOT / "data" / "metrics")
    lxd_endpoint: str = "/var/snap/lxd/common/lxd/unix.socket"
    lxd_verify_cert: bool = True
    dashboard_dir: str = str(REPO_ROOT / "dashboard" / "dist")
    poll_interval: float = 10.0
    raw_retention_hours: int = 6
    rollup_retention_days: int = 7
    retention_days: int = 30
    session_hours: float = 8.0
    session_idle_minutes: int = 60
    terminal_idle_seconds: int = 900
    terminal_max_per_user: int = 2
    terminal_max_total: int = 8
    exec_timeout_seconds: int = 30
    min_memory_mib: int = 128
    min_disk_gib: int = 2
    images: dict[str, dict[str, str]] = field(default_factory=lambda: dict(IMAGES))

    @property
    def latest_path(self) -> str:
        """Where the collector publishes its newest snapshot for the web API."""
        return str(Path(self.tinyflux_dir) / "latest.json")

    @property
    def retention(self) -> dict[str, int]:
        """Seconds kept per TSDB tier (raw / 5m / 1h)."""
        from .tsdb import retention_seconds
        return retention_seconds(self.raw_retention_hours, self.rollup_retention_days, self.retention_days)

    @property
    def lxd_socket(self) -> str | None:
        e = self.lxd_endpoint
        return e[len("unix://"):] if e.startswith("unix://") else (e if e.startswith("/") else None)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        load_dotenv(env_file or REPO_ROOT / ".env")
        env = os.environ.get
        secret = env("SESSION_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("SESSION_SECRET must be at least 32 random characters (see .env.example)")
        redirect = env("GOOGLE_OAUTH_REDIRECT_URI", cls.google_redirect_uri)
        # The browser origin is derived from the redirect URI unless overridden,
        # so there is exactly one place to configure the public URL.
        origin = env("PUBLIC_ORIGIN") or redirect.split("/auth/")[0]
        return cls(
            session_secret=secret,
            google_client_id=env("GOOGLE_OAUTH_CLIENT_ID", ""),
            google_client_secret=env("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            google_redirect_uri=redirect,
            bootstrap_admin_email=env("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower(),
            cookie_secure=_bool(env("COOKIE_SECURE", "true")),
            public_origin=origin.rstrip("/"),
            sqlite_path=_path(env("SQLITE_DB_PATH", cls.sqlite_path)),
            tinyflux_dir=_path(env("TINYFLUX_DB_PATH", cls.tinyflux_dir)),
            lxd_endpoint=env("LXD_ENDPOINT", cls.lxd_endpoint),
            lxd_verify_cert=_bool(env("LXD_VERIFY_CERT", "true")),
            dashboard_dir=_path(env("DASHBOARD_DIST", cls.dashboard_dir)),
            poll_interval=max(2.0, float(env("COLLECTOR_POLL_INTERVAL_SECONDS", "10"))),
            raw_retention_hours=int(env("RAW_RETENTION_HOURS", "6")),
            rollup_retention_days=int(env("ROLLUP_RETENTION_DAYS", "7")),
            retention_days=int(env("METRICS_RETENTION_DAYS", "30")),
            session_hours=float(env("SESSION_HOURS", "8")),
            session_idle_minutes=int(env("SESSION_IDLE_MINUTES", "60")),
            terminal_idle_seconds=int(env("TERMINAL_IDLE_SECONDS", "900")),
            terminal_max_per_user=int(env("TERMINAL_MAX_PER_USER", "2")),
            terminal_max_total=int(env("TERMINAL_MAX_TOTAL", "8")),
            exec_timeout_seconds=int(env("EXEC_TIMEOUT_SECONDS", "30")),
        )
