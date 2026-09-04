# fleet-map

Where things run: a live map of the Magic Unicorn fleet (hosts, containers, routes, GPU lanes,
LAN, agents). `collect.sh` sshes every host in `data/hosts3.txt` (read-only), `build.py` merges
the inventories with `topology.json` into `data.js`, `index.html` renders it (D3 + 3D).

`data.js` and the inventories are gitignored: they hold IPs, ports, MACs and client names.

## Where it runs

| copy | where | data | privacy |
|---|---|---|---|
| local | `file:///Users/aaronstransky/Projects/fleet-map/index.html` | `data.js` from `./collect.sh` | toggle (🔒 button, `?privacy=1`) |
| office panel | uc1-tv FLEET tile (panel-ops) | bigboy `panel-fleet-push.timer` every 5 min → panel feed | reduced, privacy applied |
| Ops-Center | magicunicorn.dev Fleet Map page | bigboy `deploy/opscenter-push.sh` (ExecStartPost) | full data behind SSO |
| **public** | **https://magicunicorn.tech/fleet/** (unlinked, noindex) | bigboy `deploy/public-push.sh` (ExecStartPost) → `reduce_public.py` → rsync → commander `/home/muut/fleet-public/data.js` | **forced on, server-side redacted** |

## Public copy (Build In Public receipts page)

- `reduce_public.py data.js out.js` — strips whole fields (IPs, tailnet/public addresses, listening
  ports, container ports, UniFi network layer, LAN clients, physical locations, file-provider
  upstreams, guest agents, agent notes), keeps only `privacy.ownDomains` hostnames, applies
  `privacy.replace` to host ids everywhere, scrubs every remaining string (IP/MAC → `[ip]`/`[mac]`,
  other hostnames → `[hidden]`, `privacy.redact` → `[client]`, `:port` and long hex ids removed),
  then **re-scans its own output and refuses to write if anything is left** (exit 2).
- `deploy/build-public-viewer.py <outdir>` — patches `index.html` into the public viewer (privacy
  forced on, button hidden, `localStorage`/`?privacy=` ignored, `<meta name="robots" content="noindex">`,
  data loaded from `data/data.js` with a 5-minute cache-buster) and copies d3, the 3D bundle and the
  crew avatars. Output is committed in the site repo `MagicUnicorn/magicunicorn.tech` under
  `marketing/fleet/` (Dockerfile bakes `marketing/` into the nginx image). Re-run + commit there after
  a viewer change.
- `deploy/public-push.sh` — runs on bigboy as a second `ExecStartPost` of `panel-fleet-push.service`
  (`deploy/public-push.service.d.conf`, installed by `deploy/public-push-install.sh`). No tokens:
  rsync over bigboy muut's ssh key to commander muut. Site side: the Infrastructure compose service
  `magicunicorn-tech` bind-mounts `/home/muut/fleet-public:/usr/share/nginx/html/fleet/data:ro`.
- Adding a client name: edit `privacy.redact` / `privacy.replace` in `topology.json`, commit, push;
  bigboy pulls before every run. Anything on the redact list that survives the reduce stops the push.
