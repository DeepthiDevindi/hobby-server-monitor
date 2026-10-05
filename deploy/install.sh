#!/usr/bin/env bash
# Install Hobby Server Monitor as two systemd services on this machine.
# Run from the repository root as root:   sudo deploy/install.sh
# Idempotent: re-running upgrades code, dependencies and the DB schema.
set -euo pipefail

PREFIX=/opt/hobby-server-monitor
ETC=/etc/hobby-server-monitor
STATE=/var/lib/hobby-server-monitor

[[ $EUID -eq 0 ]] || { echo "run as root (sudo $0)"; exit 1; }
[[ -f backend/requirements.txt && -d dashboard ]] || { echo "run from the repository root"; exit 1; }
[[ -f dashboard/dist/index.html ]] || { echo "build the dashboard first: (cd dashboard && npm ci && npm run build)"; exit 1; }

# 1. service user; the lxd group is root-equivalent (see README "Security notes")
id hsm &>/dev/null || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin hsm
usermod -aG lxd hsm

# 2. code (read-only for the service) and a private venv
install -d -m 0755 "$PREFIX"
cp -r backend "$PREFIX/"
install -d -m 0755 "$PREFIX/dashboard"
cp -r dashboard/dist "$PREFIX/dashboard/"
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/backend/requirements.txt"
chown -R root:root "$PREFIX"

# 3. config: secrets readable by root and the service group only
install -d -m 0750 -o root -g hsm "$ETC"
if [[ ! -f "$ETC/env" ]]; then
  install -m 0640 -o root -g hsm .env.example "$ETC/env"
  echo "!! edit $ETC/env (Google OAuth, SESSION_SECRET, BOOTSTRAP_ADMIN_EMAIL, COOKIE_SECURE=true) then re-run"
  exit 0
fi

# 4. state + schema, as the service user so file ownership is right
install -d -m 0700 -o hsm -g hsm "$STATE"
# (paths are set after sourcing so they win, exactly as in the unit files)
runuser -u hsm -- bash -c "set -a; . '$ETC/env'; SQLITE_DB_PATH='$STATE/app.db'; TINYFLUX_DB_PATH='$STATE/metrics'; set +a
  cd '$PREFIX/backend' && exec '$PREFIX/.venv/bin/python' -m hsm.init_db"

# 5. services: start on boot, restart on failure
install -m 0644 deploy/hsm-collector.service deploy/hsm-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hsm-collector.service hsm-web.service
systemctl --no-pager --lines=0 status hsm-collector.service hsm-web.service
