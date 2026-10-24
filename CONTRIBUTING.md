# Contributing

fleet-map is the fleet inventory and topology viewer in the Unicorn Commander suite. The [README](README.md) is the way to build `data.js` and open the page.

## Run the viewer

```bash
python3 build.py
./serve.sh
```

Open <http://127.0.0.1:8765/>. Python 3 from the standard library is enough. There is no package install and no container image.

## What to change

- `topology.json` — sites, hosts, links, layers, privacy rules
- `data/agents.json` — the roster the map draws
- `data/hosts3.txt` — the SSH list `collect.sh` reads
- `index.html` — the viewer
- `build.py` — how inventories and the topology become `data.js`

Leave `data.js`, `data/inv.*.txt`, `data/unifi.json`, and `data/headscale-nodes.json` untracked. They are listed in `.gitignore` and they hold addresses from a live collection.

Use stand-in names in examples (`node-a`, `gpu-1`). Do not add addresses, tokens, or customer names to a commit.

A change to `index.html` has to keep `deploy/build-public-viewer.py` working. That script exits when one of its patches matches anything other than once.

## Tests

The only automated tests cover `tools/keycloak-lockout/`, a Keycloak helper that ships beside the map:

```bash
python3 -m unittest discover -s tools/keycloak-lockout -t tools/keycloak-lockout
```

There is no lint target.

## Licence

By contributing, you agree that your contribution is licensed under the GNU Affero General Public License v3.0 or later, the same terms as [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md). Report vulnerabilities in private.
