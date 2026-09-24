#!/usr/bin/env python3
"""Build the PUBLIC copy of the fleet-map viewer (magicunicorn.tech/fleet/).

    python3 deploy/build-public-viewer.py <outdir> [--data data.js]

Patches index.html so that privacy mode is FORCED ON and cannot be toggled (button
hidden, localStorage and ?privacy= ignored), adds <meta name="robots" content="noindex">,
and loads the data from data/data.js with a 5-minute cache-buster (the collector on
bigboy refreshes that file through deploy/public-push.sh). Copies d3, the 3D bundle
and the crew avatars next to it. The public data itself is NOT written unless --data
is given (local testing) — on the site it lives on a bind mount, see deploy/public-push.sh.

Every patch must match exactly once; a viewer change that breaks a patch fails the build
instead of silently shipping a toggleable copy.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNER_OLD = "🔒 PRIVACY MODE — IPs, ports, routes and client identifiers hidden"
BANNER_NEW = "🔒 PUBLIC VIEW — live fleet, refreshed every 5 min · IPs, ports, routes and client identifiers hidden"

PATCHES = [
    # (description, regex, replacement)
    ("indexable title", re.escape("<title>Magic Unicorn Fleet Map</title>"),
     '<title>Live fleet map · Magic Unicorn</title><link rel="canonical" href="https://magicunicorn.tech/fleet/"><meta name="robots" content="index,follow">'),
    ("agents on unless opted out",
     re.escape("if(new URLSearchParams(location.search).get('layer')==='agents') setAgents(true);"),
     "if(new URLSearchParams(location.search).get('layer')!=='off') setAgents(true);"),
    ("hide privacy button", r'<div><span class="btn" id="bPriv"[^>]*>[^<]*</span></div>',
     '<div style="display:none"><span class="btn" id="bPriv"></span></div>'),
    ("data path + cache-buster", re.escape('<script src="data.js"></script>'),
     '<script>document.write(\'<script src="data/data.js?v=\'+Math.floor(Date.now()/300000)+\'"><\\/script>\')</script>'),
    ("force privacy on", re.escape("let privacy=false; try{privacy=localStorage.getItem('fleetmap-privacy')==='1';}catch(e){}"),
     "let privacy=true; /* public copy: privacy mode is forced on and not toggleable */"),
    ("no localStorage write", re.escape("try{localStorage.setItem('fleetmap-privacy',privacy?'1':'0');}catch(e){}"), ""),
    ("no toggle handler", r"document\.getElementById\('bPriv'\)\.onclick=\(\)=>\{privacy=!privacy;.*?refresh\(\);\};",
     "document.getElementById('bPriv').onclick=null;"),
    ("ignore ?privacy=", re.escape("if(NETPARAMS.has('privacy')){ privacy=NETPARAMS.get('privacy')==='1'; applyPriv(); }"),
     "privacy=true; applyPriv();"),
    ("banner text", re.escape(BANNER_OLD), BANNER_NEW),
]


def build(outdir, data=None):
    src = (ROOT / "index.html").read_text(encoding="utf-8")
    for desc, rx, rep in PATCHES:
        src, n = re.subn(rx, lambda m: rep, src, flags=re.S)
        if n != 1:
            sys.exit("patch '%s' matched %d times (expected 1) — index.html changed, fix build-public-viewer.py" % (desc, n))
    if "localStorage.getItem('fleetmap-privacy')" in src:
        sys.exit("privacy localStorage read survived the patch")
    out = Path(outdir)
    (out / "vendor").mkdir(parents=True, exist_ok=True)
    (out / "assets" / "agents").mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(src, encoding="utf-8")
    shutil.copy2(ROOT / "d3.v7.min.js", out / "d3.v7.min.js")
    shutil.copy2(ROOT / "vendor" / "fleet3d.bundle.js", out / "vendor" / "fleet3d.bundle.js")
    # crew avatars only: guest agents are dropped by reduce_public.py and their files carry names
    import json
    guests = {Path(a["avatar"]).name for a in json.loads((ROOT / "data" / "agents.json").read_text())["agents"]
              if a.get("tier") == "guest" and a.get("avatar")} if (ROOT / "data" / "agents.json").exists() else set()
    n = 0
    for f in sorted((ROOT / "assets" / "agents").glob("*.webp")):
        if f.name in guests:
            continue
        shutil.copy2(f, out / "assets" / "agents" / f.name)
        n += 1
    if data:
        import subprocess
        subprocess.check_call([sys.executable, str(ROOT / "reduce_public.py"), data, str(out / "data" / "data.js")])
    print("built public viewer in %s (%d avatars%s)" % (out, n, ", with data" if data else ""))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    data = sys.argv[sys.argv.index("--data") + 1] if "--data" in sys.argv else None
    build(sys.argv[1], data)
