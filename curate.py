#!/usr/bin/env python3
"""Build a curated, self-checking TV station list (M3U) and a matching programme guide (XMLTV).

Runs nightly in GitHub Actions (see .github/workflows/update.yml) and can be run by hand:
    python curate.py            writes playlist.m3u, guide.xml.gz, report.md, state.json

Only the standard library is used. Rules:
  * Sources are lists that only carry free, public streams (sources.json).
  * A stream is kept only if its host is on allowlist.txt (the broadcasters' own servers or the CDNs
    they pay for). Re-streams through unknown proxies are dropped, whatever the source says.
  * Every stream is probed. A stream that fails is kept for up to MAX_FAILS nights in a row (servers have
    bad nights), then dropped until it works again. Geo-blocked streams (marked by the source) are never
    dropped for failing, because GitHub's servers are in the US and cannot see them; they work in Germany.
  * One stream per channel; the best one (official host, highest resolution, https) wins.
"""
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Linux; Android 11; TV) AppleWebKit/537.36 ExoPlayer"
MAX_FAILS = 3
CTX = ssl.create_default_context()


def fetch(url, limit=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        data = r.read(limit) if limit else r.read()
        return r.status, r.geturl(), data


def norm(name):
    """'TRT 1 HD Ⓖ' -> 'trt1'; 'MDR Thüringen' -> 'mdrthuringen'. Used to match guide channels by name."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\b(hd|fhd|uhd|4k|tv|fernsehen|television|televizion|televizioni|channel)\b", " ", s)
    return re.sub(r"[^a-z0-9+]", "", s)


def host_ok(url, allow):
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(h == a or h.endswith("." + a) for a in allow)


def parse_m3u(text):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            name = line.rsplit(",", 1)[-1].strip()
            name = re.sub(r"\s*\((\d+p|Turkiye|Albania)\)|\s*\[Not 24/7\]|\s*\[Geo-blocked\]", "", name)
            raw_id = attrs.get("tvg-id", "")
            name = re.sub(r"[Ⓐ-ⓩ]", "", name)  # Free-TV markers such as Ⓖ (geo) and Ⓢ
            cur = {"name": name.strip(), "rawid": raw_id, "id": raw_id.split("@")[0],
                   "logo": attrs.get("tvg-logo", ""),
                   "geo": "Ⓖ" in line or "geo-blocked" in line.lower(),
                   "not247": "not 24/7" in line.lower()}
        elif line and not line.startswith("#") and cur:
            cur["url"] = line
            out.append(cur)
            cur = None
    return out


def probe(url):
    """(ok, height, note). Follows an HLS master to one variant so dead origins are caught."""
    try:
        st, final, body = fetch(url, 300_000, 12)
        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text[:300]:
            return st == 200 and not text.lstrip().startswith("<"), 0, "not-hls"
        hs = [int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", text)]
        if "#EXT-X-STREAM-INF" in text:
            var = [l for l in text.splitlines() if l and not l.startswith("#")]
            if var:
                st2, _, b2 = fetch(urllib.parse.urljoin(final, var[0]), 40_000, 12)
                if st2 != 200 or b"#EXT" not in b2:
                    return False, 0, "variant-fail"
        elif "#EXTINF" not in text:
            return False, 0, "empty-playlist"
        return True, max(hs) if hs else 0, ""
    except urllib.error.HTTPError as e:
        return False, 0, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - every failure is just "down tonight"
        return False, 0, type(e).__name__


def main():
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    allow = [l.strip().lower() for l in (ROOT / "allowlist.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    state_path = ROOT / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    # 1. Collect candidates, grouped by our own group names.
    cands, dropped_host = [], []
    for src in cfg["playlists"]:
        try:
            _, _, raw = fetch(src["url"], timeout=60)
        except Exception as e:  # noqa: BLE001
            print("source failed:", src["url"], e, file=sys.stderr)
            continue
        only = src.get("only")
        for it in parse_m3u(raw.decode("utf-8", "replace")):
            if only and not re.search(only, it["rawid"]):
                continue
            if it["not247"]:
                continue
            if any(re.search(p, it["name"], re.I) for p in cfg.get("exclude_names", [])):
                continue
            it["group"] = src["group"]
            it["prio"] = src.get("priority", 5)
            (cands if host_ok(it["url"], allow) else dropped_host).append(it)

    # 2. Probe every distinct URL once (parallel); the same stream in two lists counts once per night.
    urls = sorted({c["url"] for c in cands})
    with cf.ThreadPoolExecutor(32) as ex:
        res = dict(zip(urls, ex.map(probe, urls)))
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for u, (ok, h, note) in res.items():
        s = state.setdefault(u, {"fails": 0, "first_seen": now})
        s["fails"] = 0 if ok else s["fails"] + 1
        s["last_ok"] = now if ok else s.get("last_ok")
    for c in cands:
        ok, h, note = res[c["url"]]
        c.update(ok=ok, height=h, note=note, fails=state[c["url"]]["fails"])

    # 3. One stream per channel: keep working (or tolerated) streams, best first.
    def keep(c):
        # 403/451 from GitHub's US servers is almost always a geo block the source did not mark (Welt):
        # the stream works in Germany, so it is never dropped for that.
        if c.get("note") in ("HTTP 403", "HTTP 451"):
            c["geo"] = True
        return c["ok"] or c["geo"] or c["fails"] <= MAX_FAILS and c.get("note") != "HTTP 404"

    best = {}
    for c in sorted(cands, key=lambda c: (not c["ok"], c["prio"], -c["height"], not c["url"].startswith("https"))):
        key = c["id"] or norm(c["name"])
        if keep(c) and key not in best:
            best[key] = c
    chans = sorted(best.values(), key=lambda c: (cfg["group_order"].index(c["group"]), c["prio"], c["name"].lower()))

    # 4. Guide: take matching channels from the XMLTV sources, rename their ids to ours.
    # guide_aliases: {"regex on our channel name": "name in the guide"}; regional feeds share the umbrella guide.
    by_norm = {}
    for c in chans:
        names = [c["name"]] + [a for pat, a in cfg.get("guide_aliases", {}).items() if re.search(pat, c["name"], re.I)]
        for n in names:
            lst = by_norm.setdefault(norm(n), [])
            if c not in lst:
                lst.append(c)
    # A guide often has several entries with the same name (regional feeds, an HD copy, an empty stub), so
    # for every channel take the guide entry with the most programmes across all guide files.
    prog_re = re.compile(r'<programme [^>]*channel="([^"]+)"[^>]*>.*?</programme>', re.S)
    best_gid = {}  # our id -> (programme count, guide index, guide id)
    xmls = []
    for gi, gurl in enumerate(cfg["guides"]):
        try:
            _, _, raw = fetch(gurl, timeout=180)
            xml = gzip.decompress(raw).decode("utf-8", "replace") if gurl.endswith(".gz") else raw.decode()
        except Exception as e:  # noqa: BLE001
            print("guide failed:", gurl, e, file=sys.stderr)
            xml = ""
        xmls.append(xml)
        counts = {}
        for m in re.finditer(r'<programme [^>]*channel="([^"]+)"', xml):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        for gid, gname in re.findall(r'<channel id="([^"]+)">\s*<display-name[^>]*>([^<]+)<', xml):
            if not counts.get(gid):
                continue
            for c in by_norm.get(norm(gname), []):
                ours = c["id"] or norm(c["name"])
                if counts[gid] > best_gid.get(ours, (0,))[0]:
                    best_gid[ours] = (counts[gid], gi, gid)
    progs = {}
    for gi, xml in enumerate(xmls):
        want = {}
        for ours, (_, g, gid) in best_gid.items():
            if g == gi:
                want.setdefault(gid, []).append(ours)
        if not want:
            continue
        for m in prog_re.finditer(xml):
            for ours in want.get(m.group(1), []):
                progs.setdefault(ours, []).append(m.group(0).replace(f'channel="{m.group(1)}"', f'channel="{ours}"'))
    del xmls

    # 5. Write the playlist.
    repo = os.environ.get("GITHUB_REPOSITORY")
    base = f"https://raw.githubusercontent.com/{repo}/main" if repo else cfg["publish_base"].rstrip("/")
    lines = [f'#EXTM3U x-tvg-url="{base}/guide.xml.gz"']
    for n, c in enumerate(chans, 1):
        cid = c["id"] or norm(c["name"])
        name = re.sub(r"\s*[Ⓖ]\s*", "", c["name"]).strip()
        lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-chno="{n}" tvg-name="{name}" tvg-logo="{c["logo"]}" '
                     f'group-title="{c["group"]}",{name}')
        lines.append(c["url"])
    (ROOT / "playlist.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 6. Write the guide (only our channels, so it stays small for the TV).
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="tv-senderliste">']
    for c in chans:
        cid = c["id"] or norm(c["name"])
        name = re.sub(r"[<&>]", "", re.sub(r"\s*[Ⓖ]\s*", "", c["name"]))
        out.append(f'<channel id="{cid}"><display-name>{name}</display-name></channel>')
    for c in chans:
        out.extend(progs.get(c["id"] or norm(c["name"]), []))
    out.append("</tv>")
    with gzip.open(ROOT / "guide.xml.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\n".join(out))

    # 7. Report and state.
    rep = [f"# Senderliste – Prüfbericht {now}", "",
           f"{len(chans)} Sender in der Liste, {sum(1 for c in chans if progs.get(c['id'] or norm(c['name'])))} mit Programmführer. "
           f"{len(dropped_host)} Einträge verworfen, weil ihr Server nicht auf allowlist.txt steht.", "",
           "| Nr | Sender | Gruppe | Status | Auflösung | EPG |", "|---|---|---|---|---|---|"]
    for n, c in enumerate(chans, 1):
        st = "OK" if c["ok"] else ("geo (in DE ok)" if c["geo"] else f"Fehler {c['fails']}x: {c['note']}")
        epg = "ja" if progs.get(c["id"] or norm(c["name"])) else "–"
        rep.append(f"| {n} | {re.sub(chr(0x24bc), '', c['name']).strip()} | {c['group']} | {st} | {c['height'] or '?'}p | {epg} |")
    gone = [c for c in cands if not keep(c)]
    if gone:
        rep += ["", "## Vorübergehend entfernt", ""] + [f"- {c['name']} ({c['group']}): {c['fails']}x {c['note']}" for c in gone]
    (ROOT / "report.md").write_text("\n".join(rep) + "\n", encoding="utf-8")
    live = {c["url"] for c in cands}
    state = {u: s for u, s in state.items() if u in live}
    state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    print(f"{len(chans)} channels, {len(progs)} with guide, {len(dropped_host)} dropped by allowlist")


if __name__ == "__main__":
    main()
