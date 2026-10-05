# Interview notes: likely questions and short answers

**1. Why a single FastAPI process with SQLite instead of something "scalable"?**
The tool runs on the machine it monitors, so every MB and CPU cycle it uses is
taken from the containers. One async worker handles dozens of SSE and WebSocket
clients easily. SQLite in WAL mode is enough for a handful of users and an
audit log, and needs no extra daemon. I measured 0.28% of one core and
about 42 MiB (cgroup) idle, and 0.45% with 10 live viewers. The trade-off is
that rate limits, terminal counters and the metrics cache live in memory and
reset on restart. I documented that.

**2. How do you guarantee "one LXD call per interval" no matter how many viewers?**
A single `MetricsHub` task polls `GET /1.0/instances?recursion=2`, which embeds
the state of *every* instance in one response. Subscribers get the same
snapshot through 1-slot queues, and the newest snapshot replaces an unread one.
The task is created when the first SSE client subscribes and cancelled when the
last one leaves. I verified this with `lxc monitor`: 3 clients for 13 s produced
4 calls, and 0 calls after they disconnected. A unit test shows 50 subscribers
cause about 3 calls over 3 ticks, not 150.

**3. Why the `BOOTSTRAP_ADMIN_EMAIL` env var rather than "first login is admin"?**
"First login wins" is a race: anyone who reaches the URL before the owner gets
root-equivalent control. The env var makes the root of trust the server's own
config, which only the owner can edit. It's deterministic, works the same on
restart, and recovering from a lost admin is just editing one line. That
account is re-asserted as admin on each login and can't be demoted in the UI.
Everyone else starts with no assignments.

**4. How is authorization enforced? What stops a missed check?**
Every route declares `require_user`, `require_admin` or
`require_container_access(name)` as a FastAPI dependency. Role and assignments
are read from the DB on every request, and nothing comes from the client. Tests
introspect `app.routes`: every non-public route must return 401 without a
session, and every route depending on `require_admin` must return 403 for a
normal user. A new route that forgets its dependency fails CI. Unassigned and
non-existent containers return the same 403, so a user can't probe names.

**5. Why server-side sessions if the cookie is already signed?**
A purely signed cookie can't be revoked. Logout or deleting a user wouldn't
take effect until expiry. My cookie holds an HMAC-signed random token, and the
DB stores only its SHA-256 with `expires_at`. That gives immediate revocation,
and a leaked DB file contains no usable sessions. Long-lived streams re-check
the session every tick, so an SSE stream or open terminal closes within seconds
of logout, expiry, demotion or unassignment.

**6. How does CSRF protection work, including for WebSockets?**
Every non-GET request needs an `X-CSRF-Token` matching the per-session
synchronizer token from `/api/me`, plus an `Origin` equal to `PUBLIC_ORIGIN`
when the browser sends one. This is enforced inside `require_user`, so no
mutating route can forget it. `SameSite=Lax` is a second layer. WebSockets
can't carry custom headers, so the terminal handshake is rejected unless
`Origin` matches exactly. That is the standard defence against cross-site
WebSocket hijacking. Login CSRF is covered by the OAuth `state` and nonce.

**7. Walk me through the terminal path and its safeguards.**
xterm.js sends binary frames to `/api/containers/{name}/terminal`. On connect
the server:
1. checks Origin, the session, assignment and that the container is running;
2. applies the rate limit (10/min/user, 20/min/IP) and concurrency caps (2 per
   user, 8 total);
3. calls LXD `exec` with a fixed argv (`/bin/bash -l`), `interactive` and
   `wait-for-websocket`, and bridges to LXD's data and control websockets over
   the unix socket.

Text frames are accepted only as `{"type":"resize"}`. A watchdog closes the
session on idle timeout (15 min), session expiry or revocation, with distinct
close codes the UI explains. On close we SIGHUP the shell, and I verified no
bash process is left behind. Open and close are audited with duration.

**8. How do you handle input validation and injection risks?**
Pydantic models use `extra="forbid"`.
* Container names match `^[a-z0-9-]{1,63}$` in paths and bodies, plus LXD's
  hostname rule on create.
* Images must be in a server-side allowlist.
* CPU and memory are bounded by the schema and host limits.
* There's no shell anywhere: LXD REST with JSON bodies and argv lists, and URL
  segments are additionally `quote()`d.
* All SQL is parameterized.

In the UI, all server data goes through `textContent`, never `innerHTML`. The
CSP is `script-src 'self'`, and I checked that the Astro build produces no
inline scripts.

**9. What is the biggest residual risk?**
The `lxd` group is root-equivalent. Anyone who can talk to the socket can start
a privileged container that mounts `/`. So an RCE in this app, or a stolen
admin session, is effectively host compromise, even though the service runs as
a non-root user with a heavy systemd sandbox (NoNewPrivileges,
ProtectSystem=strict, no capabilities, syscall filter, MemoryMax, CPUQuota).

Mitigations:
* Expose only a narrow LXD surface: two limit keys, forced
  `security.privileged=false`/`nesting=false`, and allowlisted images.
* Keep sessions short, HttpOnly, and revocable.
* Audit everything.
* Recommend TLS plus VPN or an IP allowlist in front.

Container users get root *inside* an unprivileged container. That's intended,
but worth stating.

**10. What would you change for a bigger deployment?**
* Move rate limits and the metrics fan-out to a shared store, or keep a single
  "metrics leader", if it ever needed multiple workers.
* Run container creation as a background job with progress over SSE instead of
  a long-held request.
* Use LXD's `/1.0/events` stream instead of polling for status changes.
* Use per-project LXD restricted certificates so the app isn't root-equivalent.
* Use zfs or btrfs pools for real disk accounting.
* Add OIDC group or domain restrictions, plus WebAuthn step-up for destructive
  admin actions.
