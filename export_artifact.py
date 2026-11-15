#!/usr/bin/env python3
"""Erzeugt die Online-Versionen des Rocket League Hub (Seite + Daten-Schnappschuss data.json).

  python3 export_artifact.py              -> artifact/   (Claude-Artefakt: ohne HTML-Geruest, ohne fremde Bilder)
  python3 export_artifact.py --web _site  -> _site/      (eigene Web-Adresse, z.B. GitHub Pages: vollstaendige Seite)

Die Online-Versionen koennen selbst keine Daten abrufen. Dieses Skript holt deshalb den aktuellen Stand
(inkl. Team-Details) und legt ihn als Schnappschuss neben die Seite.
"""
import json
import os
import re
import sys
import time

import server

OUT = os.path.join(server.ROOT, "artifact")
MAX_TEAMS = 110


def build_page(web=False):
    src = open(os.path.join(server.STATIC, "index.html"), encoding="utf-8").read()
    if web:  # vollstaendige Seite, Bilder erlaubt
        return src.replace("<script>\n\"use strict\";",
                           "<script>window.RL_STATIC = true; window.RL_IMAGES = true;</script>\n<script>\n\"use strict\";", 1)
    head = re.search(r"<head>(.*?)</head>", src, re.S).group(1)
    body = re.search(r"<body>(.*?)</body>", src, re.S).group(1)
    title = re.search(r"<title>.*?</title>", head, re.S).group(0)
    style = re.search(r"<style>.*?</style>", head, re.S).group(0)
    return "\n".join([
        title,
        '<meta name="claude:viewport-fit-sides" content="cover">',
        style,
        "<script>window.RL_STATIC = true;</script>",
        body.strip(), ""])


def collect_teams(payload):
    wanted, seen = [], set()

    def add(name, team_id=None, lp=None):
        if not name or re.match(r"^(TBD|TBA)$", name, re.I):
            return
        info = server.resolve_team(name, team_id)
        key = (info or {}).get("id") or team_id or server.norm(name)
        if key and key not in seen:
            seen.add(key)
            wanted.append((name, team_id, lp))

    for t in payload["ranking"][:30]:
        add(t["name"], t["id"])
    index = (server.cached("ranking") or {}).get("index") or {}
    by_id = {v["id"]: v for v in index.values()}
    for group in ("live", "upcoming"):
        for t in payload["tournaments"].get(group) or []:
            for pid in t.get("participants") or []:
                if pid in by_id:
                    add(by_id[pid]["name"], pid)
            for m in (t.get("keyMatches") or []) + (t.get("todaysMatches") or []):
                add(m.get("teamA"), m.get("teamAId"))
                add(m.get("teamB"), m.get("teamBId"))
    for s in payload["playerSets"]:
        for p in s["players"][:40]:
            add(p.get("team"), p.get("teamId"))
    m = payload["matches"]
    for match in m["live"] + m["upcoming"] + m["results"]:
        if match["rlcs"]:
            add(match["left"]["name"], None, match["left"]["name"])
            add(match["right"]["name"], None, match["right"]["name"])
    wanted = wanted[:MAX_TEAMS]

    teams, names = {}, {}
    for i, (name, team_id, lp) in enumerate(wanted, 1):
        try:
            t = server.team_details(name, team_id, lp)
        except Exception as e:
            print("  ! %s: %s" % (name, e), flush=True)
            continue
        key = t.get("id") or server.norm(name)
        t.pop("query", None)
        teams[key] = t
        for n in (name, t.get("name"), lp):
            for k in server.name_keys(n or ""):
                names.setdefault(k, key)
        if i % 10 == 0 or i == len(wanted):
            print("  Teams: %d/%d" % (i, len(wanted)), flush=True)
    return teams, names


def main():
    web = "--web" in sys.argv
    out = os.path.abspath(sys.argv[sys.argv.index("--web") + 1]) if web else OUT
    os.makedirs(out, exist_ok=True)
    server.load_cache()
    server.load_teams()
    print("Hole aktuelle Daten ...", flush=True)
    payload = server.build_payload()
    bad = [s["label"] for s in payload["status"] if not s["ok"]]
    if not web:
        for n in payload["news"] + payload["community"]:
            n["image"] = None  # fremde Bilder kann das Claude-Artefakt nicht laden
    print("Hole Team-Details ...", flush=True)
    payload["teams"], payload["teamNames"] = collect_teams(payload)
    payload["generated"] = int(time.time())
    with open(os.path.join(out, "data.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    page = build_page(web)
    if web and "window.RL_STATIC = true" not in page:
        print("Fehler: Seite konnte nicht fuer die Web-Version vorbereitet werden.")
        return 1
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print("Fertig: %d Teams, data.json %d KB%s" % (len(payload["teams"]), os.path.getsize(os.path.join(out, "data.json")) // 1024,
                                                    (" | Quellen mit Fehler: " + ", ".join(bad)) if bad else ""))
    return 1 if len(bad) > 3 else 0


if __name__ == "__main__":
    sys.exit(main())
