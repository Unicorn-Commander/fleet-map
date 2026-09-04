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

out = {"generatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
       "sites": topo["sites"], "externals": topo["externals"], "links": topo["links"],
       "tailnetMembers": topo["tailnetMembers"], "layers": topo["layers"], "linkLayer": topo["linkLayer"], "overlays": topo["overlays"], "privacy": topo.get("privacy", {}), "lanMembers": topo["lanMembers"],
       "hosts": hosts, "containers": containers, "routes": routes}
open(os.path.join(HERE, "data.js"), "w").write("window.FLEET = " + json.dumps(out, indent=None) + ";\n")
print(f"data.js: {len(hosts)} hosts, {len(containers)} containers, {len(routes)} routes, "
      f"{sum(len(h['hostnames']) for h in hosts.values())} public hostnames")
