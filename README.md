# Hobby Server Monitor

A small browser dashboard and control panel for LXD containers, designed to
run **on the same old Linux box it monitors**. It runs as one Python process,
uses SQLite for state, serves a static Astro UI, and needs no agents inside
containers.

* **Admins** can create, start, stop, restart, resize and delete containers,
  manage users and their container assignments, read the audit log, and open a
  terminal in any container.
* **Container users** can see live metrics and open a terminal, but **only for
  containers an admin has assigned to them**.

---

## Prerequisites

| What | Version | Notes |
|---|---|---|
| Linux with LXD | LXD 5.x (tested 5.21.8, `dir` storage) | snap install; socket `/var/snap/lxd/common/lxd/unix.socket` |
| Python | 3.10+ (tested 3.12) | runtime |
| Node.js | 18.20+ / 20.3+ / 22 | **build time only** (Astro 5). Not needed on the server at runtime |
| A Google Cloud project | – | for OAuth 2.0 / OpenID Connect |

## LXD setup

```bash
sudo snap install lxd
sudo lxd init --auto                 # default dir pool + lxdbr0 bridge
sudo usermod -aG lxd "$USER"         # dev only, see the lxd-group warning below
newgrp lxd
lxc launch ubuntu:24.04 test1        # something to look at
curl -s --unix-socket /var/snap/lxd/common/lxd/unix.socket lxd/1.0 | head -c 200   # socket works?
```

## Google OAuth setup

1. Open Google Cloud Console, go to **APIs & Services → OAuth consent screen**
   and configure it. "External" plus test users is fine for a hobby box.
   Scopes: `openid`, `email`, `profile`.
2. Go to **Credentials → Create credentials → OAuth client ID → Web application**.
3. Add the authorized redirect URI **`http://localhost:8000/auth/callback`**. In
   production use `https://your.host/auth/callback` and set `PUBLIC_ORIGIN` to
   match.
4. Copy the client ID and secret into `.env`.

## Configure

```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # -> SECRET_KEY
$EDITOR .env    # GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SECRET_KEY, BOOTSTRAP_ADMIN_EMAIL, COOKIE_SECURE=false
```

`.env` is in `.gitignore`. Secrets are read only from the environment. The app
refuses to start if `SECRET_KEY` is shorter than 32 characters.

## Run (development)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
(cd frontend && npm ci && npm run build)    # -> frontend/dist (static files)
.venv/bin/python backend/main.py             # http://localhost:8000, single worker
.venv/bin/python -m pytest -q                # 79 tests (4 are live LXD tests)
```

Sign in with the Google account named in `BOOTSTRAP_ADMIN_EMAIL`. That account
is the admin. Anyone else who signs in appears on the Admin page with **no
access** until you assign containers to them.

## Run (production, systemd)

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin hsm
sudo usermod -aG lxd hsm                        # read the warning below first
sudo mkdir -p /opt/hobby-server-monitor /etc/hobby-server-monitor
sudo rsync -a --exclude .venv --exclude node_modules --exclude .env ./ /opt/hobby-server-monitor/
sudo python3 -m venv /opt/hobby-server-monitor/.venv
sudo /opt/hobby-server-monitor/.venv/bin/pip install -r /opt/hobby-server-monitor/requirements.txt
# frontend/dist must be built beforehand (on any machine with Node) and copied along
sudo install -m 0640 -o root -g hsm .env /etc/hobby-server-monitor/env   # set COOKIE_SECURE=true, PUBLIC_ORIGIN=https://...
sudo cp deploy/hobby-server-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hobby-server-monitor
```

The service listens on `127.0.0.1:8000`. Put a TLS reverse proxy in front of it
(Caddy or nginx) and forward WebSocket upgrades for `/api/containers/*/terminal`.
The unit includes `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`PrivateTmp/Devices`, an empty capability set, a syscall filter,
`MemoryDenyWriteExecute`, `MemoryMax=192M`, `MemoryHigh=128M`, `CPUQuota=25%`
and `TasksMax=64`.

---

## Bootstrap admin: the choice and the reasoning

The first admin is whoever has the verified Google email in
**`BOOTSTRAP_ADMIN_EMAIL`**. That account is made admin on every login and
cannot be demoted or deleted through the UI.

Why I chose this over the alternatives:

* **"First user to log in becomes admin"** has a race. Between the moment the
  service is reachable and the moment the owner logs in, any stranger who finds
  the URL becomes admin of a tool that is effectively root on the host.
* **A one-time setup token printed to the log** works, but it adds state and a
  step that is easy to get wrong.
* **The env var is deterministic.** The root of trust is the server's config,
  which only the machine's owner can edit. It works the same on every restart,
  never depends on login order, and recovering from a lost admin is just
  editing one line.
  It also relies on Google's `email_verified` claim, which we require for
  everyone.

Everyone else starts as role `user` with **zero assignments**. They can sign in
but see nothing until an admin grants access. An admin can also pre-create a
user by email so that access is ready before their first login.

---

## Architecture

```
  Browser (Astro static pages + xterm.js)
     │  HTTPS (reverse proxy)            cookies: hsm_session (HttpOnly, SameSite=Lax, signed)
     ▼
 ┌──────────────────────── uvicorn, 1 process, 1 worker ─────────────────────────┐
 │ SecurityHeaders (pure ASGI) → SessionMiddleware (OAuth state only, 10 min)    │
 │                                                                               │
 │  /auth/login, /auth/callback ── authlib ──► Google OIDC (ID token + JWKS)     │
 │  /api/*   Depends(require_user | require_admin | require_container_access)    │
 │     ├─ api.py       container CRUD, users, assignments, audit                 │
 │     ├─ metrics.py   SSE  ◄── MetricsHub (one poller task, only while ≥1 sub)  │
 │     └─ terminal.py  WS   ◄─► ExecSession (2 websockets: stdin/out + control)  │
 │  /        StaticFiles(frontend/dist)                                          │
 │                                                                               │
 │  database.py  SQLite (WAL): users, sessions(hash), assignments, audit_log     │
 │  lxd.py       httpx AsyncClient(uds=…)   websockets.unix_connect(…)           │
 └───────────────────────────────┬───────────────────────────────────────────────┘
                                 │ unix socket (REST + websockets)
                                 ▼
                         LXD daemon ──► containers (no agents inside)
```

Data flow for metrics. Each tick of the single poller makes one
`GET /1.0/instances?recursion=2`, which returns every instance *with its state*
(CPU ns, memory, disk, NICs). The poller computes rates, stores the snapshot,
and pushes it into each subscriber's 1-slot queue. Each SSE generator re-checks
that viewer's session and assignments and sends only the containers that viewer
is allowed to see.

Data flow for the terminal. The browser's xterm sends binary frames to our
WebSocket. We call `POST /1.0/instances/{name}/exec` (`interactive`,
`wait-for-websocket`, fixed argv `["/bin/bash","-l"]`), connect to the
operation's data and control websockets over the unix socket, and pump bytes in
both directions. Text frames from the browser carry only `{"type":"resize"}`.

### Layout

```
backend/app/   config.py  database.py  lxd.py  security.py  dependencies.py
               auth.py  api.py  metrics.py  terminal.py  schemas.py  main.py
frontend/src/  layouts/Layout.astro  pages/{index,login,admin,terminal}.astro  scripts/*.ts  styles.css
tests/         test_auth.py  test_authorization.py  test_validation.py  test_metrics.py
               test_terminal.py  test_lxd_live.py  conftest.py (FakeLXD)
deploy/        hobby-server-monitor.service
```

---

## Resource-efficiency decisions

* **One process, one worker, plain asyncio.** No Celery, Redis, Postgres or
  Docker. Plain `uvicorn` is used without `[standard]`, so there is no uvloop,
  httptools or watchfiles.
* **Six direct runtime dependencies** (`requirements.txt`). The `.env` loader is
  ten lines of code instead of a python-dotenv dependency.
* **No background work when nobody is watching.** The metrics poller task is
  created when the first SSE client subscribes and cancelled when the last one
  leaves. With zero clients, LXD is never polled. This was verified with
  `lxc monitor` (below).
* **One LXD call per interval regardless of the number of viewers.**
  `recursion=2` returns state for all instances at once, so there is no
  per-container fan-out. A new subscriber receives the cached snapshot
  immediately if it is fresher than one interval.
* **Slow consumers can't build up memory.** Each subscriber queue holds one
  item and the oldest snapshot is dropped.
* **The UI is static files.** Astro builds to HTML/JS/CSS that FastAPI serves
  directly. The dashboard JS is about 5 KB; xterm (about 290 KB) loads only on
  the terminal page. No framework runtime.
* **SQLite** with one shared connection in WAL mode. There's no ORM. Access
  happens only on the event-loop thread, because all handlers are `async`.
* **LXD gives disk usage from its own API**, so we never run `du` and never
  install agents inside containers.

## Security decisions and threat notes

**AuthN**
* Google OpenID Connect via authlib. The ID token is verified (JWKS signature,
  `iss`, `aud` = our client ID, `exp`, and a nonce bound to the browser).
  `email_verified` must be `true`. The redirect URI comes from `PUBLIC_ORIGIN`
  and is never derived from the `Host` header.
* **Server-side sessions.** The cookie holds an itsdangerous-signed random
  token. The DB stores only its SHA-256 hash, with an expiry (`SESSION_HOURS`,
  default 8). Because of that, logout and user deletion revoke a session
  immediately, and a stolen DB file contains no usable sessions.
  There is deliberately no signed timestamp in the cookie. itsdangerous's
  `TimestampSigner` rejects "future" signatures, and on WSL2 the wall clock
  stepped back by up to 1.4 s twice in 30 s, which randomly logged users out.
  NTP steps can do the same on real hosts. Expiry is enforced by the DB row
  instead, and idle and rate-limit timers use the monotonic clock. The cookie is
  `HttpOnly; SameSite=Lax; Max-Age=…`, plus `Secure` when `COOKIE_SECURE=true`.
* The login and callback routes are rate limited (10 per minute per IP).

**AuthZ**
* Every route has a server-side dependency: `require_user`, `require_admin` or
  `require_container_access(name)`. The role and assignments are read from
  SQLite on every request. Nothing from the client (role, user id, container
  list) is trusted.
* The test suite introspects the router so a route can't slip through without a
  dependency. Every non-public route must return 401 without a session, and
  every route that depends on `require_admin` must return 403 to a user.
* Unassigned and non-existent containers both return the same 403, so a user
  can't probe which names exist.
* Assignment grants view, metrics and terminal only. Lifecycle actions are
  admin-only.
* Revocation also applies to long-lived connections. SSE streams and open
  terminals re-check session and assignment on every tick (every 4 s and 5 s
  respectively) and close on logout, expiry, demotion or unassignment.

**CSRF**: every non-GET request needs `X-CSRF-Token` to match the per-session
synchronizer token (from `/api/me`). If an `Origin` header is present it must
equal `PUBLIC_ORIGIN`. This is enforced centrally inside `require_user`, so no
state-changing route can forget it. `SameSite=Lax` is a second layer.

**WebSocket terminal**
* The handshake is refused unless `Origin == PUBLIC_ORIGIN`, which prevents
  cross-site WebSocket hijacking.
* The cookie session and container access are re-checked on connect, and the
  container must be running.
* Rate limits: 10 opens per minute per user and 20 per minute per IP.
  Concurrency caps: 2 per user and 8 in total.
* Idle timeout (15 min without keystrokes). The session closes on expiry or
  revocation. Input frames are capped at 64 KiB.
* The shell gets SIGHUP when the browser leaves, so no orphaned bash processes
  are left in the container (verified).
* Terminal open and close are audited, including duration and reason.

**Input handling**
* Pydantic models with `extra="forbid"`.
* Container names must match `^[a-z0-9-]{1,63}$` in both paths and bodies.
  Create also enforces LXD's hostname rules (must start with a letter, can't
  end with `-`).
* The image must be in a server-side allowlist (`config.IMAGES`).
* CPU and memory are bounded by both the schema and the host settings.
* New containers get `security.privileged=false` and `security.nesting=false`.
* There is no shell anywhere: LXD REST and argv lists only. All SQL is
  parameterized.

**Headers** are added by a pure ASGI middleware on every response, static files
included:
* `Content-Security-Policy`: `default-src 'self'; script-src 'self'`, no
  inline scripts (the build is checked), `frame-ancestors 'none'`,
  `object-src 'none'`, `base-uri 'none'`.
* `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin`, COOP, Permissions-Policy, and
  `Cache-Control: no-store` on the API.
* HSTS when cookies are Secure.

`style-src` allows `'unsafe-inline'` because xterm.js injects a `<style>`
element for its theme. Inline *scripts* remain forbidden, which is where the XSS
risk lies. The UI writes all server data with `textContent`, never with
`innerHTML`.

**Audit log**: logins (including failures), logout, container
create/start/stop/restart/update/delete, user create/role change/delete,
assign/unassign, and terminal open/close, each with actor, target, detail and
IP.

### ⚠ The `lxd` group is effectively root

Anyone who can talk to the LXD socket can create a privileged container that
mounts the host's `/`, so membership in the `lxd` group is equivalent to root on
the host. This service runs as an unprivileged `hsm` user, but it needs that
group. Consequences:

* An RCE in this app, or a stolen **admin** session, should be treated as a
  host compromise. The systemd sandbox raises the cost of exploitation, but it
  does not change what LXD will do on request.
* That is why the app itself never exposes raw LXD config. Admins can set only
  a fixed set of keys (`limits.cpu`, `limits.memory`). Privileged and nested
  containers are forced off, and images come from an allowlist.
* Keep it behind TLS. Consider putting it behind a VPN or Tailscale, or adding
  an IP allowlist at the proxy. Keep the admin set tiny.
* Container users get a **root shell inside their container** (unprivileged
  container, so root there is not root on the host). Assign containers
  accordingly.

**Other threats considered**
* Session theft is mitigated by HttpOnly, short expiry and server-side
  revocation.
* Login CSRF is mitigated by the OAuth `state` and nonce.
* Host-header and open-redirect attacks are prevented by the fixed redirect URI
  and fixed post-login redirect to `/`.
* Resource exhaustion is mitigated by the rate limiter (whose memory is
  bounded), terminal caps, 1-slot queues and systemd `MemoryMax`/`CPUQuota`.

---

## Measured resource usage (real numbers)

Measured on 2026-10-05 on WSL2 Ubuntu 24.04 (12 vCPU laptop, Python 3.12.3,
LXD 5.21.8, two running containers). The app ran under systemd as a transient
**user** service with the same resource limits as the unit file:

```bash
systemd-run --user --unit=hsm-measure -p MemoryHigh=128M -p MemoryMax=192M -p CPUQuota=25% \
  -p TasksMax=64 -p NoNewPrivileges=yes .venv/bin/uvicorn app.main:get_app --factory \
  --app-dir backend --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

CPU = delta of the cgroup's `CPUUsageNSec` over the window. RSS comes from
`ps -o rss`; MemoryCurrent and tasks come from `systemctl show` and
`systemd-cgtop`.

| Scenario (window) | CPU (% of one core) | RSS (ps) | cgroup MemoryCurrent | Threads |
|---|---|---|---|---|
| Idle, 0 clients (60 s) | **0.28 %** | **64.8 MiB** | **41.8 MiB** | 6 |
| 1 SSE client (60 s) | 0.34 % | 64.8 MiB | 42.0 MiB | 6 |
| 10 SSE clients (60 s) | 0.45 % | 65.1 MiB | 42.2 MiB | 6 |
| Idle again after clients left (30 s) | 0.23 % | 65.1 MiB | 42.2 MiB | 6 |

`systemd-cgtop`: `hsm-measure.service  6 tasks  0.3 %  42.2M`.

**LXD load** (counted from `lxc monitor --pretty --type=logging --loglevel=debug`):
* 3 concurrent SSE clients for 13 s produced **4** `GET /1.0/instances?recursion=2`
  calls, i.e. one per 4 s interval, not 3×.
* The 10 s after they disconnected produced **0** calls.

Notes:
* The idle CPU floor of about 0.25% is mostly uvicorn's own 0.1 s housekeeping
  tick, not our code.
* RSS is larger than MemoryCurrent mostly because of shared-library pages that
  the cgroup doesn't charge to the service.
* I couldn't install the *system* unit on the test machine because there was no
  sudo. Numbers on bare-metal Ubuntu should be similar.

---

## Known limitations

* **Single worker by design.** Rate limits, terminal counters and the metrics
  cache live in process memory, so they reset on restart and don't work across
  multiple workers.
* **Disk usage shows "n/a" on `dir` storage pools**, because LXD reports 0
  without quota support. On zfs or btrfs pools it shows real numbers.
* **CPU %** is per core (100% = one core busy), computed from the delta between
  two samples. The first sample after a client connects shows "…".
* If a container is deleted outside this tool (`lxc delete`) and a new one is
  later created with the same name *outside this tool*, old assignments for
  that name still apply. Creating through the UI clears them.
* Container create blocks the HTTP request until LXD finishes (up to about a
  minute on the first image pull). There's no job queue.
* The terminal runs `/bin/bash -l` as root inside the container. Images without
  bash (e.g. Alpine) aren't in the allowlist for that reason.
* Behind a reverse proxy, the audit and rate-limit IP is the proxy's address
  unless you enable uvicorn `--proxy-headers` with `--forwarded-allow-ips` set
  to the proxy.
* Starlette's `SessionMiddleware` (used only to hold OAuth state and nonce for
  10 minutes) still uses a timestamped signature. A backward clock step in the
  middle of a login can fail that login, and retrying fixes it.
* A real Google login wasn't exercised end to end on the dev machine because no
  OAuth credentials were configured. The callback logic is covered by tests
  that stub the token exchange.
