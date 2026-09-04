#!/usr/bin/env python3
"""Build data.js for the fleet map from data/inv.*.txt + data/headscale-nodes.json + topology.json."""
import json, re, glob, os, datetime, sys
HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "data")
topo = json.load(open(os.path.join(HERE, "topology.json")))

# ---- headscale ----
hs = {}
try:
    for n in json.load(open(os.path.join(D, "headscale-nodes.json"))):
        name = n.get("given_name") or n.get("name")
        hs[name] = {"ip": (n.get("ip_addresses") or [None])[0], "online": n.get("online"),
                    "tags": [t.replace("tag:", "") for t in (n.get("valid_tags") or n.get("forced_tags") or [])],
                    "last_seen": n.get("last_seen")}
except Exception as e:
    print("headscale json missing:", e, file=sys.stderr)

alias = {}
for hid, h in topo["hosts"].items():
    for a in h.get("aliases", []):
        alias[a] = hid
def canon(name):
    return alias.get(name, name)


# ---- classification: container / unit -> (category, layer) ----
CAT_RULES = [  # order matters; first match wins. (regex on "name image", category, layer)
 (r"postgres|postgresql|mysql|mariadb|clickhouse|timescale", "database", "data"),
 (r"redis|valkey|rabbitmq|nats|memcached", "cache/queue", "data"),
 (r"minio|qdrant|falkordb|milvus|weaviate|elasticsearch|meilisearch|searxng|garage", "storage/search", "data"),
 (r"oauth2-proxy|traefik|cloudflared|caddy|nginx-proxy|jmap-proxy", "proxy/ingress", "ingress"),
 (r"keycloak|authentik|vaultwarden", "identity/secrets", "identity"),
 (r"litellm|llama|lemonade|vllm|ollama|router|infinity|embed|rerank|qwen|gemma|vision|ocr|kokoro|tts|stt|whisper|speaches|chatterbox|vibevoice|speaker-svc|artwork|flux|medsam|mammo|cxr|dots-|gpu-agent|open-webui|openwebui|librechat|presenton|research-relay|corporal|nvidia|xdna|flm", "ai/inference", "ai"),
 (r"uptime-kuma|grafana|prometheus|exporter|langfuse|glitchtip|loki|alertmanager|uc-agent|fleet-health|node-exporter|watchdog", "observability", "observability"),
 (r"james|stalwart|postfix|dovecot|listmonk|postiz|mail|email-ops|outreach|email-verifier|ev2|retirement-leads|leads", "mail/outreach", "mail"),
 (r"majiks|icecast|liquidsoap|peertube|stream|radio|channel|obs|producer|sound|music|dj", "media", "media"),
 (r"forgejo|gitea|runner|buildkit|buildx|woodpecker|registry|drone", "dev/ci", "dev"),
 (r"docker|containerd|tailscale|headscale|systemd|sshd", "platform", "platform"),
 (r"landing|website|static|redirect|-web$|frontend|-ui$|kiosk|panel", "app frontend", "apps"),
 (r"worker|celery|beat|cron|backup|seed|init", "app worker/job", "apps"),
 (r"mcp", "app mcp", "apps"),
 (r"backend|api|engine|server|app$", "app backend", "apps"),
]
EXTRA = [(r"cadvisor|umami|analytics", "observability", "observability"), (r"machine-learning|-ml$|brigade-pdf|gotenberg|tika", "ai/inference", "ai"),
         (r"-site$|site$|-web$|-front$|-tech$|-com$|sams-services|pages|booking|landing", "app frontend", "apps"), (r"agents$|brigade|knowledge-ops|nextcloud|outline|paperless|centerdeep|center-deep|search", "app backend", "apps")]
def classify(text, name=None):
    t = text.lower()
    for rx, cat, layer in CAT_RULES:
        if re.search(rx, t): return cat, layer
    for rx, cat, layer in EXTRA:
        if re.search(rx, (name or text).lower()): return cat, layer
    return "app", "apps"


GPU_HOSTS = {"bigboy","midboy1","midboy2","legacy1","aaron-strix","genesisflow-z13","aaron-uc1"}
SAAS_HOSTS = {"srv981531","centerdeep","rackboy1","bigboy"}
def tags_for(c):
    n = (c["name"] + " " + c["image"]).lower(); hn = " ".join(c["hostnames"]).lower(); t = set()
    if c["host"] in SAAS_HOSTS and re.search(r"ops-center|uchub|keycloak|lago|litellm|entitle|billing|marketplace|unicorncommander|headscale|umami|stripe|monetiz|traefik|kong|api-gateway|brigade", n): t.add("saas-platform")
    if "unicorncommander.ai" in hn and c["host"] == "srv981531": t.add("saas-platform")
    if c["layer"] == "ai" and (c["host"] in GPU_HOSTS or "litellm" in n or "router" in n): t.add("saas-inference")
    if "unicorncommander.ai" in hn or re.search(r"-prod\b|_prod\b|prod-", n) or c["host"] in {"srv981531","centerdeep","kvm2-mail","kvm2-marketing","majiks-vps","legacy1"}: t.add("prod")
    if "magicunicorn.dev" in hn or re.search(r"dogfood|-df\b|stable-|rook|corporal", n) or (c["host"] == "bigboy" and "unicorncommander.ai" not in hn and "-prod" not in n): t.add("dogfood")
    return sorted(t)

hosts = {}
containers = []
routes = []   # {hostname, via(host id), target(container id or upstream), kind}
HOST_RE = re.compile(r"Host\(`([^`]+)`\)")

def parse_inv(path):
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    if not lines or not lines[0].startswith("HOST="):
        return
    hid = canon(lines[0][5:].strip())
    meta = {"id": hid, "containers": [], "stacks": {}, "hostnames": set(), "file_routes": []}
    for ln in lines[1:]:
        if ln.startswith("OS="):
            for kv in ln.split("|"):
                k, _, v = kv.partition("=")
                meta[k.lower()] = v
        elif ln.startswith("IPS="): meta["ips"] = ln[4:].split()
        elif ln.startswith("TS="): meta["ts"] = ln[3:].strip()
        elif ln.startswith("MACS="): meta["macs"] = [m.split("=")[-1].lower() for m in ln[5:].split() if "=" in m]
        elif ln.startswith("GPU="): meta["gpu"] = [g for g in ln[4:].split(";") if g.strip()]
        elif ln.startswith("LISTEN="): meta["listen"] = ln[7:].split()
        elif ln.startswith("UNITS="): meta["units"] = ln[6:].split()
        elif ln.startswith("C="):
            body = ln[2:]
            mm = re.match(r"^([^|]*)\|([^|]*)\|([^|]*)\|(\{.*\}|[^|]*)\|([^|]*)\|([^|]*)$", body, re.S)
            if not mm:
                continue
            name, image, project, labels, ports, health = mm.groups()
            try:
                labels = json.loads(labels) if labels.startswith("{") else {}
            except ValueError:
                labels = {}
            name = name.lstrip("/")
            project = project if project and project != "<no value>" else "(no compose project)"
            hn = []
            for k, v in labels.items():
                if k.startswith("traefik.http.routers.") and k.endswith(".rule"):
                    hn += HOST_RE.findall(v)
            hn = sorted(set(hn))
            cid = f"{hid}/{name}"
            cat, layer = classify(name + " " + image.split("@")[0], name)
            if "oauth2-proxy" in name: cat, layer = "proxy/ingress", "ingress"
            svc = labels.get("com.docker.compose.service") or name
            deps = [d.split(":")[0] for d in (labels.get("com.docker.compose.depends_on") or "").split(",") if d]
            c = {"id": cid, "host": hid, "name": name, "image": image, "stack": project, "service": svc, "depends": deps,
                 "ports": ports.split(), "health": health, "hostnames": hn, "cat": cat, "layer": layer}
            c["tags"] = tags_for(c)
            containers.append(c)
            meta["containers"].append(cid)
            meta["stacks"].setdefault(project, []).append(cid)
            for h in hn:
                meta["hostnames"].add(h)
                routes.append({"hostname": h, "via": hid, "target": cid, "kind": "label"})
        elif ln.startswith("R="):
            f, hn, ups = (ln[2:].split("|") + ["", ""])[:3]
            hn = sorted(set(HOST_RE.findall(hn)))
            ups = ups.split()
            meta["file_routes"].append({"file": f, "hostnames": hn, "upstreams": ups})
            for h in hn:
                meta["hostnames"].add(h)
                for u in ups:
                    routes.append({"hostname": h, "via": hid, "target": u, "kind": "file"})
    meta["hostnames"] = sorted(meta["hostnames"])
    hosts[hid] = meta

for p in sorted(glob.glob(os.path.join(D, "inv.*.txt"))):
    parse_inv(p)

# resolve compose depends_on -> container ids (same host + project)
svc_index = {(c["host"], c["stack"], c["service"]): c["id"] for c in containers}
for c in containers:
    c["depends_ids"] = [svc_index[(c["host"], c["stack"], d)] for d in c["depends"] if (c["host"], c["stack"], d) in svc_index]
# hosts that exist only in topology/headscale (mac, panel1, shafen boxes)
for hid in topo["hosts"]:
    hosts.setdefault(hid, {"id": hid, "containers": [], "stacks": {}, "hostnames": [], "file_routes": []})

# resolve upstream urls to hosts for file routes
ip2host = {}
for hid, m in hosts.items():
    for ip in m.get("ips", []):
        ip2host[ip.split("=")[-1].split("/")[0]] = hid
    if m.get("ts"): ip2host[m["ts"]] = hid
for name, n in hs.items():
    if n["ip"]: ip2host[n["ip"]] = canon(name)
name2host = {"rackboy1.unicorncommander.net": "rackboy1", "magicunicorn.unicorncommander.net": "bigboy",
             "midboy1.unicorncommander.net": "midboy1", "midboy2.unicorncommander.net": "midboy2",
             "legacy1.unicorncommander.net": "legacy1", "centerdeep.unicorncommander.net": "centerdeep"}
cname = {c["name"]: c["id"] for c in containers}
for r in routes:
    if r["kind"] != "file": continue
    m = re.match(r"https?://([^/:]+)(?::(\d+))?", r["target"])
    if not m: continue
    h, port = m.group(1), m.group(2)
    r["port"] = port
    if h in ip2host: r["target_host"] = ip2host[h]
    elif h in name2host: r["target_host"] = name2host[h]
    elif h in cname: r["target_host"] = cname[h].split("/")[0]; r["target"] = cname[h]
    else: r["target_host"] = None

contById_tmp = {c['id']: c for c in containers}
# merge topology + headscale into hosts
for hid, m in hosts.items():
    t = topo["hosts"].get(hid, {})
    m.update({"site": t.get("site", "mobile"), "role": t.get("role", ""), "warn": t.get("warn", ""),
              "public": t.get("public", ""), "display": (t.get("aliases") or [hid])[0] if hid not in ("bigboy",) else hid})
    if hid in ("srv981531",): m["display"] = "commander"
    if hid == "centerdeep": m["display"] = "centerdeep"
    n = hs.get(hid) or next((v for k, v in hs.items() if canon(k) == hid), None)
    if n:
        m["ts"] = m.get("ts") or n["ip"]; m["online"] = n["online"]; m["tags"] = n["tags"]; m["last_seen"] = n["last_seen"]
    else:
        m["online"] = None
    m["stacks"] = {k: v for k, v in m["stacks"].items()}
    m["models"] = t.get("models", []); m["hardware"] = t.get("hardware", {}); m["physical"] = t.get("physical", {})
    # viewer glyph overrides: glyph = symbol name (rack|tower|mini|pi|laptop|mac|vps|panel), icon = URL/path of a sticker image (PNG w/ alpha)
    if t.get("glyph"): m["glyph"] = t["glyph"]
    if t.get("icon"): m["icon"] = t["icon"]
    units = []
    for u in m.get("units", []):
        cat, layer = classify(u)
        if re.match(r"(docker|containerd|tailscaled|systemd)", u): cat, layer = "platform", "platform"
        ut = []
        if layer == "ai" and hid in GPU_HOSTS: ut.append("saas-inference")
        if re.search(r"majiks|icecast|james|stalwart", u): ut.append("prod")
        units.append({"name": u, "cat": cat, "layer": layer, "tags": ut})
    m["unit_items"] = units
    by = {}
    for cid in m["containers"]:
        c = contById_tmp[cid]; by.setdefault(c["layer"], {}).setdefault(c["cat"], []).append(cid)
    for u in units: by.setdefault(u["layer"], {}).setdefault(u["cat"], []).append("unit:" + u["name"])
    m["byLayer"] = by
    ht = set()
    for cid in m["containers"]: ht.update(contById_tmp[cid].get("tags", []))
    for u in units: ht.update(u.get("tags", []))
    if hid in GPU_HOSTS or hid in {"srv981531"}: ht.add("saas-inference")
    m["tags_overlay"] = sorted(ht)

# ---- network layer: UniFi controller truth (data/unifi.json from data/unifi.py) ----
# Adds out["network"] = {devices, clients, networks, wlans, links, generatedAt} and host.net / host.net_all.
# Matching client -> fleet host: MAC (inv MACS=), then IP (inv IPS= / topology), then name/hostname vs host id, aliases,
# display and topology hosts.<id>.netNames. Links: wired host->switch/AP port, wifi host->AP, uplink device->device.
network = None
def _ipset(m):
    return {ip.split("=")[-1].split("/")[0] for ip in m.get("ips", [])}
def _subnet_of(ip, nets):
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        for n in nets:
            if n.get("subnet") and a in ipaddress.ip_network(n["subnet"], strict=False): return n
    except ValueError:
        pass
    return None
def _fmt_speed(mbps):
    if not mbps: return "link down"
    return f"{mbps/1000:g} Gb" if mbps >= 1000 else f"{mbps} Mb"
try:
    uni = json.load(open(os.path.join(D, "unifi.json")))
    net_names = {}
    for hid, h in hosts.items():
        t = topo["hosts"].get(hid, {})
        for nm in [hid, h.get("display")] + list(t.get("aliases", [])) + list(t.get("netNames", [])):
            if nm: net_names[str(nm).lower()] = hid
    mac2host = {m: hid for hid, h in hosts.items() for m in h.get("macs", [])}
    lanip2host = {}
    for hid, h in hosts.items():
        for ip in _ipset(h): lanip2host[ip] = hid
    dev_by_mac = {d["mac"]: d for d in uni["devices"]}
    net_by_id = {n["id"]: n for n in uni["networks"]}
    net_by_name = {n["name"]: n for n in uni["networks"]}
    nlinks = []
    for d in uni["devices"]:
        d["host"] = None
        up = d.get("uplink")
        if up and up.get("mac") in dev_by_mac:
            nlinks.append({"s": d["id"], "t": up["mac"], "k": "uplink",
                           "l": f"{d['name']} uplink → {dev_by_mac[up['mac']]['name']} port {up.get('remote_port')} · {_fmt_speed(up.get('speed'))}" + (" (offline)" if not d["online"] else ""),
                           "meta": {"port": up.get("remote_port"), "speed": up.get("speed"), "online": d["online"]}})
    for c in uni["clients"]:
        hid = None; how = None
        if c["mac"] in mac2host: hid, how = mac2host[c["mac"]], "mac"
        if not hid:
            for ip in (c.get("ip"), c.get("fixed_ip"), c.get("last_ip")):
                if ip and ip in lanip2host: hid, how = lanip2host[ip], "ip"; break
        if not hid:
            for nm in (c.get("name"), c.get("hostname")):
                if not nm: continue
                k = str(nm).lower()
                for cand in (k, re.sub(r"[-_ ](wifi|wlan|wired|eth|lan)\d*$", "", k)):
                    if cand in net_names: hid, how = net_names[cand], "name"; break
                if hid: break
        c["host"] = hid; c["match"] = how
        dev = dev_by_mac.get(c.get("sw_mac") if c["wired"] else c.get("ap_mac"))
        c["device"] = dev["id"] if dev else None; c["device_name"] = dev["name"] if dev else None
        # network sanity: controller says X, the host's own interface subnet says Y (Flex Mini clients come back "Default" with no IP)
        if hid:
            own = sorted(ip for ip in _ipset(hosts[hid]) if not ip.startswith("100."))
            own_nets = {}
            for ip in own:
                sn = _subnet_of(ip, uni["networks"])
                if sn: own_nets.setdefault(sn["name"], ip)
            if own_nets and c.get("network") not in own_nets:
                c["host_network"] = ", ".join(sorted(own_nets)); c["host_ip"] = own_nets[sorted(own_nets)[0]]
            elif c.get("ip") and own and c["ip"] not in own:
                c["host_ip"] = next((ip for ip in own if _subnet_of(ip, uni["networks"]) and _subnet_of(ip, uni["networks"])["name"] == c.get("network")), own[0])
        if not dev: continue
        if c["wired"]:
            port = next((p for p in dev["ports"] if p["idx"] == c.get("sw_port")), None)
            lab = f"{dev['name']} port {c.get('sw_port')}" + (f" “{port['name']}”" if port and port.get("name") and not re.match(r"^(Port|SFP\+?) ?\d+$", port["name"]) else "") + f" · {_fmt_speed(c.get('speed'))}"
            if c.get("network"): lab += f" · {c['network']}" + (f" (VLAN {c['vlan']})" if c.get("vlan") else "")
            c["link"] = {"k": "lan", "l": lab}
        else:
            lab = f"{dev['name']} · {c.get('essid') or 'wifi'} · {c.get('signal')} dBm · ch {c.get('channel')} ({c.get('band') or ''}) · {round((c.get('tx_rate') or 0)/1000)} Mb"
            if c.get("network"): lab += f" · {c['network']}" + (f" (VLAN {c['vlan']})" if c.get("vlan") else "")
            c["link"] = {"k": "wifi", "l": lab}
        if hid:
            nlinks.append({"s": hid, "t": dev["id"], "k": c["link"]["k"], "l": c["link"]["l"], "client": c["mac"]})
    for hid, h in hosts.items():
        mine = [c for c in uni["clients"] if c["host"] == hid]
        if not mine: continue
        mine.sort(key=lambda c: (not c["wired"], -(c.get("signal") or -999)))  # wired first, then strongest wifi
        c = mine[0]
        h["net"] = {"device": c["device"], "device_name": c["device_name"], "wired": c["wired"],
                    "port": c.get("sw_port"), "speed": c.get("speed"), "ap": c.get("ap_mac"), "essid": c.get("essid"),
                    "signal": c.get("signal"), "rssi": c.get("rssi"), "channel": c.get("channel"), "band": c.get("band"),
                    "tx_rate": c.get("tx_rate"), "vlan": c.get("vlan"), "network": c.get("network"), "host_network": c.get("host_network"),
                    "ip": c.get("ip"), "host_ip": c.get("host_ip"), "mac": c["mac"], "uptime": c.get("uptime"), "match": c["match"], "links": len(mine)}
        h["net_all"] = [c["mac"] for c in mine]
    gw = next((d for d in uni["devices"] if d["type"] == "gateway"), None)
    if gw and gw.get("wan", {}).get("ip"):
        nlinks.append({"s": "internet", "t": gw["id"], "k": "edge", "l": f"WAN {gw['wan'].get('ifname') or ''} {gw['wan']['ip']} · {_fmt_speed(gw['wan'].get('speed'))}"})
    matched = sum(1 for c in uni["clients"] if c["host"])
    network = {"generatedAt": uni.get("generatedAt"), "controller": {k: v for k, v in uni.get("controller", {}).items() if k != "user"},
               "devices": uni["devices"], "clients": uni["clients"], "networks": uni["networks"], "wlans": uni.get("wlans", []), "links": nlinks}
    print(f"network: {len(uni['devices'])} devices, {len(uni['clients'])} clients ({matched} matched to {len({c['host'] for c in uni['clients'] if c['host']})} hosts), {len(nlinks)} links", file=sys.stderr)
except FileNotFoundError:
    print("data/unifi.json missing: network layer falls back to the static lanMembers mesh", file=sys.stderr)
except Exception as e:
    print("unifi.json merge failed:", repr(e), file=sys.stderr)

# drift: nodes the tailnet knows that topology.json does not (new box, renamed box, missing alias)
unmapped = sorted({name for name in hs if canon(name) not in topo["hosts"]})
if unmapped:
    print("UNMAPPED tailnet nodes (add to topology.json hosts/aliases):", ", ".join(unmapped), file=sys.stderr)
out = {"generatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "unmapped": unmapped,
       "sites": topo["sites"], "externals": topo["externals"], "links": topo["links"],
       "tailnetMembers": topo["tailnetMembers"], "layers": topo["layers"], "linkLayer": topo["linkLayer"], "overlays": topo["overlays"], "privacy": topo.get("privacy", {}), "lanMembers": topo["lanMembers"],
       "hosts": hosts, "containers": containers, "routes": routes, "network": network}
open(os.path.join(HERE, "data.js"), "w").write("window.FLEET = " + json.dumps(out, indent=None) + ";\n")
print(f"data.js: {len(hosts)} hosts, {len(containers)} containers, {len(routes)} routes, "
      f"{sum(len(h['hostnames']) for h in hosts.values())} public hostnames")
