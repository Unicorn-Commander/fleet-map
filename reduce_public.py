#!/usr/bin/env python3
"""Produce the PUBLIC fleet-map data file (server-side redaction).

    python3 reduce_public.py data.js public/data.js

Reads the collector's data.js (window.FLEET = {...}) and writes a copy that is safe
to serve on the marketing site (magicunicorn.tech/fleet/). The public viewer runs
with privacy mode forced on, but the viewer is client-side: whatever is in the data
file is public the moment it is served. So this script is the real gate and it
strips WHOLE FIELDS rather than trusting regexes:

  hosts        drop ips / ts / public / listen / net / net_all / file_routes /
               physical / last_seen; hostnames filtered to ownDomains
  containers   drop ports; hostnames filtered to ownDomains
  routes       only ownDomains hostnames; file-provider targets reduced to the host id
  network      dropped entirely (UniFi devices, LAN clients, MACs, VLAN subnets)
  agents       guests dropped; piece notes / avatar notes / stable usernames dropped;
               piece labels lose parentheticals (keys, ports, room ids)
  externals    notes dropped
  sites        label -> short (city level)
  privacy      redact list / replace map NOT published (the list itself names clients)
  ids          topology.json privacy.replace applied to host ids everywhere

Then every remaining string (keys included) goes through a text scrub: replace map,
MAC/IPv4/IPv6 -> [mac]/[ip]/[ip6], hostnames outside ownDomains -> [hidden],
redact patterns -> [client], :port suffixes and long hex ids removed.

Finally the OUTPUT is re-scanned (IPs, MACs, private ranges, every redact pattern,
non-own hostnames). Any hit -> exit 2 and NO file is written: fail closed.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Registries / public infrastructure allowed to appear as hostnames (image names, labels).
PUBLIC_HOSTS = {
    "quay.io", "ghcr.io", "docker.io", "gcr.io", "codeberg.org", "code.forgejo.org",
    "registry.gitlab.com", "lscr.io", "mcr.microsoft.com", "public.ecr.aws", "nvcr.io",
    "github.com", "youtube.com", "cloudflare.com", "hostinger.com",
}
# TLDs we treat as "this token is a hostname" (keeps settings.json / foo.py / x.service alone).
TLDS = ("com", "net", "org", "io", "ai", "dev", "tech", "health", "services", "site", "art",
        "online", "studio", "music", "media", "app", "cloud", "xyz", "info", "us", "co", "me",
        "tv", "live", "uk", "de", "br", "sh", "gg", "ly", "to", "cc")

RX_HOSTNAME = re.compile(r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:%s))(?![\w-])" % "|".join(TLDS), re.I)
RX_MAC = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)
RX_IP4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")
RX_IP6 = re.compile(r"(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?:/\d{1,3})?|(?<![\w:])[0-9a-f]{0,4}::[0-9a-f:]+", re.I)
RX_PORT = re.compile(r"(?<!\d):\d{2,5}\b")
RX_HEX = re.compile(r"\b[0-9a-f]{7,}\b")
RX_PARENS = re.compile(r"\s*\([^()]*\)")

OWNER_REPLACE = [("Aaron's", "the founder's"), ("Aaron’s", "the founder’s"), ("Aaron", "the founder"), ("aaron", "founder")]


def load_fleet(path):
    s = Path(path).read_text(encoding="utf-8")
    return json.loads(s[s.index("{"): s.rindex("}") + 1])


def own_dom(h, own):
    h = (h or "").lower()
    return any(h == d or h.endswith("." + d) for d in own)


class Scrubber:
    def __init__(self, priv):
        self.own = [d.lower() for d in priv.get("ownDomains", [])]
        # word-bounded first; then left-bounded only (catches "caseylanesite-web") for patterns long enough
        # not to hit inside ordinary words (perry/raspberry, jordan/engine)
        self.redact = [re.compile(r"\b(?:%s)\b" % p, re.I) for p in priv.get("redact", [])] + \
                      [re.compile(r"(?<![a-z])(?:%s)" % p, re.I) for p in priv.get("redact", []) if len(re.sub(r"\[.*?\][*?]?|[()?|]", "", p)) >= 6]
        rep = dict(priv.get("replace", {}))
        # longest keys first so shafen-macstudio wins over shafen
        self.replace = sorted(rep.items(), key=lambda kv: -len(kv[0])) + OWNER_REPLACE

    def host_ok(self, h):
        h = h.lower()
        return own_dom(h, self.own) or h in PUBLIC_HOSTS

    def text(self, s):
        x = s
        for k, v in self.replace:
            x = x.replace(k, v)
        x = RX_MAC.sub("[mac]", x)
        x = RX_IP4.sub("[ip]", x)
        x = RX_IP6.sub("[ip6]", x)
        x = RX_HOSTNAME.sub(lambda m: m.group(1) if self.host_ok(m.group(1)) else "[hidden]", x)
        for rx in self.redact:
            x = rx.sub("[client]", x)
        x = RX_PORT.sub("", x)
        x = RX_HEX.sub("[id]", x)
        return re.sub(r"[ \t]{2,}", " ", x).strip()

    def walk(self, o, key=None):
        if isinstance(o, dict):
            return {self.text(k): self.walk(v, k) for k, v in o.items()}
        if isinstance(o, list):
            return [self.walk(v, key) for v in o]
        if isinstance(o, str):
            if key in ("generatedAt", "color", "avatar"):
                return o
            return self.text(o)
        return o


def rename_ids(o, idmap):
    """Apply the host-id map to every string equal to an id or prefixed '<id>/' (container ids)."""
    def one(s):
        if s in idmap:
            return idmap[s]
        if "/" in s:
            h, rest = s.split("/", 1)
            if h in idmap:
                return idmap[h] + "/" + rest
        return s
    if isinstance(o, dict):
        return {one(k): rename_ids(v, idmap) for k, v in o.items()}
    if isinstance(o, list):
        return [rename_ids(v, idmap) for v in o]
    if isinstance(o, str):
        return one(o)
    return o


def reduce_public(F):
    priv = F.get("privacy") or {}
    own = [d.lower() for d in priv.get("ownDomains", [])]
    rep = priv.get("replace", {})
    hosts = F.get("hosts", {})
    # host-id map: only ids that exist; avoid collisions by suffixing
    idmap, used = {}, set()
    for hid in hosts:
        if hid in rep:
            new = rep[hid]
            n = 2
            while new in used or (new in hosts and new != hid):
                new = "%s-%d" % (rep[hid], n)
                n += 1
            idmap[hid] = new
            used.add(new)

    out = {
        "generatedAt": F.get("generatedAt"),
        "public": True,
        "unmapped": [],
        "sites": {k: {"label": v.get("short") or v.get("label"), "short": v.get("short") or v.get("label"),
                      "color": v.get("color"), "geo": v.get("geo")} for k, v in F.get("sites", {}).items()},
        "externals": [{k: v for k, v in e.items() if k != "note"} for e in F.get("externals", [])],
        "links": F.get("links", []),
        "tailnetMembers": [h for h in F.get("tailnetMembers", []) if h in hosts],
        "lanMembers": [h for h in F.get("lanMembers", []) if h in hosts],
        "layers": F.get("layers", []),
        "linkLayer": F.get("linkLayer", {}),
        "overlays": F.get("overlays", []),
        "privacy": {"desc": priv.get("desc", ""), "ownDomains": priv.get("ownDomains", []),
                    "redact": [], "replace": {}},
        "hosts": {},
        "containers": [],
        "routes": [],
        "network": None,
        "agents": [],
        "agentKinds": F.get("agentKinds", []),
    }

    DROP_HOST = {"ips", "ts", "public", "listen", "net", "net_all", "file_routes", "physical", "last_seen"}
    for hid, h in hosts.items():
        nh = {k: v for k, v in h.items() if k not in DROP_HOST}
        nh["hostnames"] = [x for x in h.get("hostnames", []) if own_dom(x, own)]
        nh["physical"] = {}
        nh["file_routes"] = []
        out["hosts"][hid] = nh

    for c in F.get("containers", []):
        nc = dict(c)
        nc["ports"] = []
        nc["hostnames"] = [x for x in c.get("hostnames", []) if own_dom(x, own)]
        out["containers"].append(nc)

    for r in F.get("routes", []):
        if not own_dom(r.get("hostname", ""), own):
            continue
        nr = {"hostname": r["hostname"], "via": r.get("via"), "kind": r.get("kind"),
              "target_host": r.get("target_host")}
        if r.get("kind") == "file":
            nr["target"] = r.get("target_host") or "[upstream]"
        else:
            nr["target"] = r.get("target")
        out["routes"].append(nr)

    for a in F.get("agents", []):
        if a.get("tier") == "guest":
            continue
        na = {k: v for k, v in a.items() if k not in ("avatar_note", "stable_username", "pieces")}
        na["pieces"] = [{"kind": p.get("kind"), "label": RX_PARENS.sub("", p.get("label") or "").strip(),
                         "host": p.get("host"), "target": p.get("target"), "service": p.get("service"),
                         "status": p.get("status")} for p in a.get("pieces", [])]
        out["agents"].append(na)

    out = rename_ids(out, idmap)
    sc = Scrubber(priv)
    out = sc.walk(out)
    # a renamed host whose display name got redacted shows its new id instead of "[client]…"
    for hid, h in out["hosts"].items():
        if "[client]" in (h.get("display") or "") or "[hidden]" in (h.get("display") or ""):
            h["display"] = hid
    return out, sc


def verify(text, priv, sc):
    """Re-scan the serialized output. Returns a list of (what, count)."""
    probs = []
    for name, rx in (("ipv4", RX_IP4), ("mac", RX_MAC),
                     ("rfc1918/cgnat", re.compile(r"\b(192\.168\.|10\.\d{1,3}\.|100\.\d{1,3}\.|172\.(1[6-9]|2\d|3[01])\.)"))):
        n = len(rx.findall(text))
        if n:
            probs.append((name, n))
    for p in priv.get("redact", []):
        n = len(re.findall(r"(?<![a-z])(?:%s)" % p, text, re.I))
        if n:
            probs.append(("redact:" + p, n))
    for k in priv.get("replace", {}):
        n = len(re.findall(r"(?<![\w-])%s(?![\w-])" % re.escape(k), text, re.I))
        if n:
            probs.append(("replace-key:" + k, n))
    bad = {}
    for m in RX_HOSTNAME.finditer(text):
        h = m.group(1)
        if not sc.host_ok(h):
            bad[h.lower()] = bad.get(h.lower(), 0) + 1
    for h, n in sorted(bad.items()):
        probs.append(("hostname:" + h, n))
    return probs


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    F = load_fleet(src)
    out, sc = reduce_public(F)
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    probs = verify(text, F.get("privacy") or {}, sc)
    if probs:
        print("REFUSING to write %s — leaks found in the reduced output:" % dst, file=sys.stderr)
        for what, n in probs:
            print("  %-40s %d" % (what, n), file=sys.stderr)
        sys.exit(2)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(dst + ".tmp")
    tmp.write_text("window.FLEET=" + text + ";\n", encoding="utf-8")
    tmp.replace(dst)
    print("wrote %s: %d hosts, %d containers, %d routes, %d agents, %d bytes" % (
        dst, len(out["hosts"]), len(out["containers"]), len(out["routes"]), len(out["agents"]), len(text)))


if __name__ == "__main__":
    main()
