# Interview prep: likely questions, short answers, and where to point

Use this to rehearse. Every answer names the file to open while explaining.

**1. Walk me through a request to `/api/containers/test1/history` from a container user.**
1. `AuthMiddleware.process_resource` (`backend/hsm/web/policy.py`) looks up
   the resource's declared policy, `CONTAINER`.
2. `resolve_user` checks the cookie's HMAC, then the session row (absolute and
   idle expiry), then the user row.
3. It validates that `test1` matches the name regex, then calls
   `db.can_access`. That query joins `containers` (name → uuid) with
   `assignments` or owner. If there's no row, the answer is 403, whether or
   not the container exists.
4. Only then does `History.on_get` (`hsm/web/containers.py`) run. It picks a
   TSDB tier, reads only the overlapping segment files, and buckets to ≤ 360
   points.

**2. Why a separate collector process instead of a thread in the API?**
- The brief requires collection independent of the UI. As its own unit
  (`deploy/hsm-collector.service`) it keeps recording while the web API is
  restarted, upgraded or crashed.
- It's the **sole writer** of TinyFlux, so there are no multi-writer races.
- Its sandbox can be much tighter: no network at all, lower CPU and IO
  priority.
- Cost: one extra small Python process (see the REPORT measurements).
- Rejected: a thread in the API (couples history to the web process
  lifetime), and an RPC socket between the processes (more protocol surface;
  an atomically replaced file is enough).

**3. How does N open tabs not cost N× the work?**
- Tabs never reach the collector, which makes one LXD call per 10 s regardless.
- In the web process, `LiveHub` (`hsm/web/live.py`) has one watcher task that
  `stat`s `latest.json` once a second. It exists only while there's at least
  one subscriber.
- Each snapshot is read once and queued to every tab, filtered per user.
- Tests: `test_many_tabs_one_reader_and_nothing_when_idle` and
  `test_one_lxd_call_per_tick`.

**4. Why TinyFlux segments and tiers? Isn't that over-engineering?**
I measured first:
- One file with a day of raw data for 5 containers took 0.34 s and 60 MB RSS
  per query, because TinyFlux parses the whole file. Hence hourly and daily
  segments.
- TinyFlux writes field names on every row (~228 B/row), so 30 days of 5-minute
  rows came to 45 MB per 10 containers. Hence tiers (raw 6 h, 5-minute 7 d,
  1-hour 30 d), giving about 13.6 MB.
- Retention deletes whole files: no rewrite, no reader and writer race.
- Code: `hsm/tsdb.py`. Test: `test_storage_is_bounded`.

**5. What does a quota measure, and why not actual usage?**
- It measures the sum of *allocated* limits (CPU cores, memory, disk) over the
  containers a user owns. Allocation is what the host promised and is stable;
  usage spikes would make a quota flap, and enforcing it would mean killing
  work.
- At the limit, new allocations are refused with the numbers, and running
  containers are untouched.
- Races are prevented by an `asyncio.Lock` plus counting in-flight create jobs
  (`_allocated_with_pending`).
- Code: `hsm/quota.py`, `hsm/web/containers.py`.

**6. How do you stop next month's endpoint from forgetting authorization?**
There are three layers:
- **Runtime:** a resource with no `policy` gets a 500, not a pass.
- **Startup:** `check_routes()` refuses to start the app.
- **Tests:** they walk `app.route_table` and assert 401 without a session for
  every protected responder, and 403 for a user on every admin responder.

CSRF lives in the same middleware, so a new POST is protected automatically.

**7. What happens on logout? On revoking a user?**
- **Logout** deletes the server-side session row, so the cookie is dead
  everywhere at once.
- **Revoking a user** deletes the user; sessions and grants cascade, and their
  containers become unowned but keep running.
- Open SSE streams re-check on every push, and terminals every 5 s, so both
  close within seconds.
- Why not JWT: it can't be revoked before expiry.

**8. What is the real security boundary of the terminal?**
- The container, not command filtering. Users get root *inside* an
  unprivileged container (user namespaces), with `security.privileged` and
  `security.nesting` forced false, AppArmor and seccomp from LXD, no devices,
  and process, memory and CPU limits.
- The text is one argv element to `/bin/sh -c` inside the container, wrapped
  in `timeout -s KILL 30`. Nothing reaches a host shell.
- Residual risks: a kernel or LXD breakout bug, and LAN access from the
  bridge.

**9. Your service runs as non-root. Is it unprivileged?**
- No. It's in the `lxd` group, and LXD socket access is root on the host.
- Mitigations:
  - an API that exposes only fixed config keys (privileged and nesting forced
    off, image allowlist);
  - systemd sandboxing;
  - a collector with no network;
  - an audit log;
  - admin is treated as root.
- The proper fix is a restricted LXD TLS client confined to a project. It's
  listed as follow-up #1.

**10. How are container renames and deletes handled?**
- Rows are keyed by LXD `volatile.uuid`. The collector reconciles every tick.
- **Rename:** the name is updated (in two steps, so a swap can't violate
  `UNIQUE`), and grants and history follow the uuid.
- **Out-of-band delete:** the row is removed, grants cascade, and it's
  audited.
- **Same name reused:** new uuid, so no inherited access.
- Fresh rows (< 30 s old) are protected from a racing tick.
- Tests: `test_rename_keeps_owner_and_grants` and
  `test_outside_delete_drops_grants_and_same_name_is_not_inherited`.

**11. What does the user see when LXD is down?**
- The collector keeps ticking, records `lxd up=0`, and publishes last-known
  values with `lxd_ok=false`.
- The dashboard dims the cards and shows a red banner with the time LXD was
  last seen.
- Actions return 503 "LXD is unreachable". pylxd has 3 s connect and 30 s read
  timeouts.
- If the collector itself stops, the banner says the data is stale and for
  how long.

**12. Why Falcon, and what surprised you about it?**
- It's the brief's preferred stack, it's lean, and its middleware gives one
  choke point for authorization.
- Surprises:
  - the test client runs the shutdown hook per request;
  - `App` has `__slots__`;
  - a WebSocket can't receive text *or* binary in one call, so I used a typed
    binary protocol;
  - errors raised before `accept` show the browser only 1006, so the
    middleware accepts and then closes with a 44xx code.

**13. What did you get wrong first?**
- v1 followed a paraphrase of the brief: it polled only while viewers were
  connected, with no TSDB or quotas.
- `TimestampSigner` plus WSL clock steps produced random logouts.
- A rollup average was biased by the first missing sample.
- systemd `EnvironmentFile` precedence and inline comments.
- Each is in REPORT "Issues", with the fix.

**14. How did you use AI, and how do you know the code is right?**
- Claude Code wrote most of the code from my direction.
- Correctness comes from:
  - 116 tests, including adversarial authN/authZ, forged-token and quota-race
    tests;
  - live tests against real LXD;
  - headless-browser checks;
  - reading every module.
- Bugs it introduced and that were caught are listed in REPORT "AI Tool
  Usage".
