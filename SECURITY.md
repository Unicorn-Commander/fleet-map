# Security

## Reporting a vulnerability

Please do not report a vulnerability in a public GitHub issue, pull request, or discussion.

Open a private advisory on [github.com/Unicorn-Commander/fleet-map](https://github.com/Unicorn-Commander/fleet-map/security/advisories/new). Include what is affected, how to reproduce it, and the impact you see.

This repository does not publish a security contact address. Do not put a live inventory, a token, a password, or a customer name in the report.

## Data that should not be committed

`data.js`, `data/inv.*.txt`, `data/unifi.json`, and `data/headscale-nodes.json` are gitignored. A collection writes addresses, ports, MACs, and client names into them.

`reduce_public.py` is the check before a data file is published. It exits 2 and writes nothing if a scan of its own output still matches an address, a MAC, a redact pattern, or a hostname outside `privacy.ownDomains`.

The UniFi collector and the Ops-Center push take credentials from the environment (`UNIFI_PASS`, `OPSCENTER_FLEET_TOKEN`). `tools/keycloak-lockout/kc_lockout.py` reads credentials from environment variables named in its target file. Do not commit those values.

## Scope

This policy covers the code in this repository, including the viewer, the collectors, and the publish scripts.
