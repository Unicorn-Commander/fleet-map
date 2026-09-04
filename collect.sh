#!/bin/bash
# Re-collect live inventory from every host into data/ then rebuild data.js.
# Needs the Mac's ssh keys. Runs read-only commands only.
cd "$(dirname "$0")"; mkdir -p data
while read h a; do [ -z "$h" ] && continue
  ( timeout 150 ssh -o BatchMode=yes -o ConnectTimeout=12 $a 'bash -s' < data/inv.sh > data/inv.$h.txt 2>/dev/null ) &
done < data/hosts3.txt
# centerdeep: muut needs a sudo password (fleet muut pw, Vaultwarden REMOVED). Set CENTERDEEP_SUDO_PW to include it.
if [ -n "$CENTERDEEP_SUDO_PW" ]; then
  ( scp -q data/inv.sh muut@100.87.46.87:/tmp/inv.sh && timeout 150 ssh -o BatchMode=yes muut@100.87.46.87 "echo '$CENTERDEEP_SUDO_PW' | sudo -S -p '' bash /tmp/inv.sh" > data/inv.centerdeep.txt 2>/dev/null ) &
fi
( ssh -o BatchMode=yes -o ConnectTimeout=10 root@100.79.41.137 'docker exec unicorn-headscale headscale nodes list -o json 2>/dev/null' > data/headscale-nodes.json ) &
( ssh -o BatchMode=yes -o ConnectTimeout=10 root@100.79.41.137 'for d in /home/muut/Production/UC-Cloud/extensions/traefik-commander/dynamic /home/muut/Infrastructure/traefik/dynamic; do for f in $d/*.yml $d/*.yaml; do [ -f "$f" ] && echo "R=$(basename $f)|$(grep -hoE "Host\(\`[^\`]+\`\)" $f | tr "\n" " ")|$(grep -hoE "url: *\"?https?://[^\" ]+" $f | sed "s/url: *\"\?//" | tr "\n" " ")"; done; done' > data/.commander-routes.txt ) &
wait
[ -s data/.commander-routes.txt ] && cat data/.commander-routes.txt >> data/inv.commander.txt
python3 build.py
