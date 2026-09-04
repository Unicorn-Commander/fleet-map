#!/usr/bin/env bash
# Install the Ops-Center push step on BIGBOY next to the panel collector (run from the Mac).
# Additive rsync, no --delete. Idempotent. Does NOT create the token — see PRECONDITIONS.
#
# PRECONDITIONS (operator, once):
#   1. Pick a token:  openssl rand -hex 32   (store it in Vaultwarden REMOVED "Ops-Center fleet-map ingest (dogfood)")
#   2. Ops-Center side (dogfood = bigboy bind-mount, no recreate needed):
#        printf '%s\n' "<token>" | ssh muut@192.168.10.10 'umask 077; cat > /home/muut/UC-Cloud-production/services/ops-center/backend/data/fleet_map_ingest.token'
#      (prod/commander later: FLEET_MAP_INGEST_TOKEN in the ops-center compose env + recreate)
#   3. Collector side: append  OPSCENTER_FLEET_TOKEN=<token>  (and optionally OPSCENTER_URL=)
#      to /etc/panel-feeds/env on bigboy (root:muut 0640):
#        ssh muut@192.168.10.10 'sudo -n sh -c "printf \"OPSCENTER_FLEET_TOKEN=%s\\n\" \"$(cat /home/muut/UC-Cloud-production/services/ops-center/backend/data/fleet_map_ingest.token)\" >> /etc/panel-feeds/env"'
#   Then run this script. Nothing here prints the token.
set -euo pipefail
BB=muut@192.168.10.10
HERE="$(cd "$(dirname "$0")" && pwd)"

ssh "$BB" 'mkdir -p ~/fleet-map/deploy'
rsync -az "$HERE/opscenter-push.sh" "$HERE/opscenter-push.service.d.conf" "$BB:fleet-map/deploy/"

ssh "$BB" bash -s <<'REMOTE'
set -euo pipefail
chmod +x ~/fleet-map/deploy/opscenter-push.sh
test -r /etc/panel-feeds/env || { echo "/etc/panel-feeds/env missing or unreadable by muut" >&2; exit 1; }
grep -q '^OPSCENTER_FLEET_TOKEN=' /etc/panel-feeds/env || { echo "OPSCENTER_FLEET_TOKEN not in /etc/panel-feeds/env (see PRECONDITIONS)" >&2; exit 1; }
systemctl cat panel-fleet-push.service >/dev/null 2>&1 || { echo "panel-fleet-push.service not installed (panel-ops deploy/fleet-push/install.sh first)" >&2; exit 1; }
sudo -n mkdir -p /etc/systemd/system/panel-fleet-push.service.d
sudo -n cp ~/fleet-map/deploy/opscenter-push.service.d.conf /etc/systemd/system/panel-fleet-push.service.d/opscenter.conf
sudo -n systemctl daemon-reload
systemctl cat panel-fleet-push.service | grep -n "ExecStartPost"
# push the CURRENT data.js right away (no re-collect); the timer keeps it fresh from here on
set -a; . /etc/panel-feeds/env; set +a
~/fleet-map/deploy/opscenter-push.sh
REMOTE
