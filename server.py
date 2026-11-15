#!/usr/bin/env python3
"""Rocket League Hub - lokales Dashboard fuer RLCS-Ergebnisse, News, Rankings und Patch Notes.

Start:  python3 server.py [--open] [--port 8790]
Benoetigt nur die Python-Standardbibliothek.
"""
import gzip
import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8790
ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
USER_AGENT = "RocketLeagueHub/1.0 (local personal dashboard)"
STEAM_APPID = 252950


# ---------------------------------------------------------------- HTTP ----
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def iso_to_ts(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


# ----------------------------------------------------------- Liquipedia ----
def parse_liquipedia_matches(page_html):
    matches = []
    for seg in page_html.split('<div class="match-info">')[1:]:
        ts = re.search(r'data-timestamp="(\d+)"', seg)
        if not ts:
            continue
        head_end = seg.find("</span></span>")
        finished = 'data-finished="finished"' in seg[: head_end if head_end > 0 else 600]
        parts = re.split(r'<div class="match-info-header-opponent([^"]*)">', seg)
        # parts: [pre, classesL, bodyL, classesR, bodyR...]
        if len(parts) < 5:
            continue

        def team(classes, body):
            body = body.split('<div class="match-info-tournament')[0]
            body_team = body.split('<div class="match-info-header-scoreholder')[0]
            full = re.search(r'title="([^"]+)"', body_team)
            short = re.search(r'<span class="name"[^>]*>(.*?)</span>', body_team, re.S)
            short_txt = strip_tags(short.group(1)) if short else ""
            full_txt = html.unescape(full.group(1)) if full else short_txt
            full_txt = re.sub(r" \(page does not exist\)$", "", full_txt)
            return {
                "name": full_txt or short_txt or "TBD",
                "short": short_txt or full_txt or "TBD",
                "winner": "match-info-header-winner" in classes,
            }

        left = team(parts[1], parts[2])
        right = team(parts[3], parts[4])
        scores = re.findall(r'match-info-header-scoreholder-score(?: [^"]*)?">([^<]*)<', seg)
        lower = re.search(r'match-info-header-scoreholder-lower">(.*?)</span>', seg, re.S)
        tname = re.search(r'match-info-tournament-name">(.*?)</span></a>', seg, re.S)
        tlink = re.search(r'match-info-tournament-name"><a href="([^"]+)"', seg)
        tour = strip_tags(tname.group(1)) if tname else ""
        link = "https://liquipedia.net" + html.unescape(tlink.group(1)) if tlink else ""
        is_rlcs = tour.upper().startswith("RLCS") or "Championship_Series" in link
        matches.append({
            "ts": int(ts.group(1)),
            "finished": finished,
            "left": left,
            "right": right,
            "scoreL": scores[0].strip() if len(scores) >= 2 else None,
            "scoreR": scores[1].strip() if len(scores) >= 2 else None,
            "format": strip_tags(lower.group(1)).strip("()") if lower else "",
            "tournament": tour,
            "url": link,
            "rlcs": is_rlcs,
        })
    return matches


def fetch_matches():
    url = ("https://liquipedia.net/rocketleague/api.php?action=parse"
           "&page=Liquipedia:Matches&format=json&prop=text")
    data = json.loads(http_get(url))
    all_m = parse_liquipedia_matches(data["parse"]["text"]["*"])
    now = time.time()
    seen, uniq = set(), []
    for m in all_m:
        key = (m["ts"], m["left"]["name"], m["right"]["name"], m["tournament"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(m)
    results = sorted([m for m in uniq if m["finished"]], key=lambda m: -m["ts"])
    open_m = sorted([m for m in uniq if not m["finished"]], key=lambda m: m["ts"])
    live = [m for m in open_m if m["ts"] <= now and now - m["ts"] < 6 * 3600]
    upcoming = [m for m in open_m if m["ts"] > now]
    return {"live": live, "upcoming": upcoming, "results": results}


# ---------------------------------------------------------------- BLAST ----
def decode_turbo_stream(page_html):
    """Dekodiert die im HTML eingebetteten React-Router-Daten (turbo-stream) inkl. Nachlieferungen."""
    chunks = re.findall(r'streamController\.enqueue\((".*?")\);?\s*</script>', page_html, flags=re.S)
    if not chunks:
        raise ValueError("keine eingebetteten Daten gefunden")
    flat, promises = [], {}
    for chunk in chunks:
        for line in json.loads(chunk).split("\n"):
            if not line.strip():
                continue
            if not flat:
                flat = json.loads(line)
                continue
            m = re.match(r"P(\d+):(.*)$", line, re.S)
            if not m:
                continue
            try:
                arr = json.loads(m.group(2))
            except ValueError:
                continue
            if isinstance(arr, list):
                promises[int(m.group(1))] = len(flat)
                flat.extend(arr)
    memo = {}

    def hyd(i):
        if not isinstance(i, int) or i < 0 or i >= len(flat):
            return None
        if i in memo:
            return memo[i]
        v = flat[i]
        if isinstance(v, dict):
            o = {}
            memo[i] = o
            for k, x in v.items():
                o[flat[int(k[1:])]] = hyd(x)
            return o
        if isinstance(v, list):
            if v and isinstance(v[0], str):  # typisierter Wert: ["D", ms] Datum, ["P", id] Nachlieferung
                memo[i] = None
                if v[0] == "D" and len(v) > 1:
                    memo[i] = v[1]
                elif v[0] == "P" and len(v) > 1:
                    memo[i] = hyd(promises.get(v[1], -1))
                return memo[i]
            o = []
            memo[i] = o
            for x in v:
                o.append(hyd(x))
            return o
        memo[i] = v
        return v

    return hyd(0).get("loaderData", {})


def loader(ld, needle):
    for k, v in ld.items():
        if needle in k:
            return v
    raise KeyError(needle)


def blast_match(m):
    a, b = m.get("teamA") or {}, m.get("teamB") or {}
    return {
        "name": m.get("name"), "format": m.get("type"),
        "ts": iso_to_ts(m.get("scheduledAt")),
        "teamA": a.get("name") or "TBD", "teamAId": a.get("id"),
        "teamB": b.get("name") or "TBD", "teamBId": b.get("id"),
        "scoreA": m.get("teamAScore"), "scoreB": m.get("teamBScore"),
    }


def pretty_tournament(tid):
    words = []
    for w in tid.split("-"):
        words.append(w.upper() if w in ("rlcs", "eu", "na", "sam", "mena", "oce", "apac", "ssa", "lcq")
                     else w.capitalize())
    return " ".join(words)


def fetch_blast_home():
    page = http_get("https://blast.tv/rl")
    ld = decode_turbo_stream(page)
    tl = loader(ld, "routes/rl").get("tournamentTimelineData") or {}
    out = {}
    for group in ("live", "upcoming", "past"):
        items = []
        for t in tl.get(group) or []:
            items.append({
                "id": t.get("id"), "name": t.get("name"),
                "start": iso_to_ts(t.get("startDate")), "end": iso_to_ts(t.get("endDate")),
                "prize": t.get("prizePool"), "teams": t.get("numberOfTeams"),
                "url": "https://blast.tv/rl/tournaments/%s" % t.get("id"),
                "participants": [x.get("id") for x in (t.get("participants") or []) if x.get("id")],
                "keyMatches": [blast_match(x) for x in (t.get("keyMatches") or [])],
                "todaysMatches": [blast_match(x) for x in (t.get("todaysMatches") or [])],
            })
        out[group] = items
    # Links zur Spielerstatistik (welche Turniere gerade relevant sind)
    links = []
    for href in re.findall(r'href="(/rl/stats/players/rating\?[^"]+)"', page):
        href = html.unescape(href)
        if href not in links:
            links.append(href)
    return {"tournaments": out, "statLinks": links}


def fetch_blast_ranking():
    ld = decode_turbo_stream(http_get("https://blast.tv/rl/ranking"))
    pr = loader(ld, "rl.ranking").get("powerRankings") or {}
    teams = []
    for i, t in enumerate(pr.get("teams") or []):
        if i >= 300:
            break
        teams.append({
            "rank": i + 1, "id": t.get("teamId"), "name": t.get("teamName"),
            "rating": t.get("rating"), "region": t.get("region"),
            "delta": t.get("positionDelta"),
        })
    index = {}
    for i, t in enumerate(pr.get("teams") or []):
        info = {"id": t.get("teamId"), "short": t.get("shortName"), "name": t.get("teamName"),
                "rank": i + 1, "rating": t.get("rating"), "region": t.get("region")}
        for key in name_keys(t.get("teamName")):
            index.setdefault(key, info)
    periods = []
    for per in loader(ld, "rl.ranking").get("ratingPeriods") or []:
        if isinstance(per.get("startDate"), (int, float)) and isinstance(per.get("endDate"), (int, float)):
            periods.append({"name": per.get("name") or "", "start": int(per["startDate"] / 1000),
                            "end": int(per["endDate"] / 1000)})
    return {"teams": teams, "index": index, "periods": periods}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def name_keys(name):
    """Schreibweisen eines Teamnamens fuer den Abgleich Liquipedia <-> BLAST."""
    full = norm(name)
    keys = [full]
    short = re.sub(r"(esports?|gaming|team|club)", "", full)
    if short and short != full and len(short) >= 3:
        keys.append(short)
    return [k for k in keys if k]


def fetch_blast_stats(path, label):
    ld = decode_turbo_stream(http_get("https://blast.tv" + path))
    d = loader(ld, "rl.stats")
    players = {p.get("id"): p for p in (d.get("players") or [])}
    teams = {t.get("id"): t for t in (d.get("teams") or [])}
    rows = []
    for s in d.get("stats") or []:
        p = players.get(s.get("playerId")) or {}
        team = teams.get(s.get("teamId")) or p.get("team") or {}
        rows.append({
            "id": s.get("playerId"), "name": s.get("playerName"),
            "realName": " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x),
            "country": p.get("nationality"),
            "team": team.get("name"), "teamId": s.get("teamId"),
            "rating": s.get("rating"), "games": s.get("gamesPlayed"),
            "gameWins": s.get("gameWins"),
            "goals": s.get("goalsPerGame"), "assists": s.get("assistsPerGame"),
            "saves": s.get("savesPerGame"), "shots": s.get("shotsPerGame"),
            "shootPct": s.get("shootingPercentage"), "score": s.get("scorePerGame"),
            "demos": s.get("demosInflictedPerGame"),
        })
    rows.sort(key=lambda r: -(r["rating"] or 0))
    return {"label": label, "url": "https://blast.tv" + path, "players": rows[:100]}


def fetch_players():
    home = cached("blast_home")
    links = (home or {}).get("statLinks") or []
    if not links:
        links = ["/rl/stats/players/rating?tournamentId=rlcs-world-championship-2026&order=desc"]
    sets = []
    for href in links[:2]:
        parsed = urllib.parse.urlparse(href)
        q = urllib.parse.parse_qs(parsed.query)
        ids = (q.get("tournamentId") or [""])[0].split(",")
        if len(ids) == 1:
            label = pretty_tournament(ids[0])
        else:
            label = "Aktuelle Saison (%d Turniere)" % len(ids)
            q.pop("regions", None)  # weltweit statt nur einzelner Regionen
        q["order"] = ["desc"]
        path = parsed.path + "?" + urllib.parse.urlencode({k: v[0] for k, v in q.items()})
        try:
            sets.append(fetch_blast_stats(path, label))
        except Exception as e:  # ein Satz darf fehlen
            sets.append({"label": label, "url": "https://blast.tv" + path, "players": [], "error": str(e)})
    if not any(s["players"] for s in sets):
        raise RuntimeError("keine Spielerstatistik erhalten")
    sets.sort(key=lambda s: 0 if s["label"].startswith("Aktuelle") else 1)
    return {"sets": sets}


def fetch_blast_news():
    ld = decode_turbo_stream(http_get("https://blast.tv/rl/news"))
    mods = loader(ld, "rl.news").get("newsModuleDataMap") or {}
    seen, items = set(), []
    for mod in mods.values():
        arts = list(mod.get("spotlighted") or [])
        for lst in (mod.get("byTag") or {}).values():
            arts.extend(lst or [])
        for a in arts:
            route = a.get("route")
            if not route or route in seen:
                continue
            seen.add(route)
            img = None
            ref = ((a.get("backgroundImage") or {}).get("asset") or {}).get("_ref") or ""
            m = re.match(r"image-([0-9a-f]+)-(\d+x\d+)-(\w+)$", ref)
            if m:
                img = "https://sanity-cdn.blast.tv/images/6znhzi10/production/%s-%s.%s?w=640&auto=format" % m.groups()
            items.append({
                "source": "BLAST.tv", "kind": "esports", "title": (a.get("articleTitle") or "").strip(),
                "url": "https://blast.tv" + route, "ts": iso_to_ts(a.get("releaseDate")),
                "image": img, "summary": "",
            })
    return items


# ---------------------------------------------------------- Team-Details ----
_lp_lock = threading.Lock()
_lp_last = [0.0]


def lp_wikitext(title):
    """Wikitext einer Liquipedia-Seite (erlaubt: 1 Abruf / 2 s -> hier gedrosselt)."""
    url = ("https://liquipedia.net/rocketleague/api.php?action=query&prop=revisions&rvprop=content"
           "&rvslots=main&redirects=1&format=json&titles=" + urllib.parse.quote(title))
    with _lp_lock:
        wait = 2.5 - (time.time() - _lp_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            raw = http_get(url)
        finally:
            _lp_last[0] = time.time()
    pages = (json.loads(raw).get("query") or {}).get("pages") or {}
    for pg in pages.values():
        if "revisions" in pg:
            return pg.get("title"), pg["revisions"][0]["slots"]["main"]["*"]
    return None, None


def wiki_clean(v):
    v = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", v or "", flags=re.S)
    v = re.sub(r"\{\{[^{}]*\}\}", "", v)
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", v)
    v = v.replace("'''", "").replace("''", "")
    return strip_tags(v).strip()


def wiki_people(value):
    people = []
    for part in re.split(r"<br\s*/?>", value or "", flags=re.I):
        if not part.strip():
            continue
        flag = re.search(r"\{\{[Ff]lag\|([A-Za-z]{2})\b", part)
        txt = wiki_clean(part)
        if not txt:
            continue
        nick = re.search(r'"([^"]+)"', txt)
        real = re.sub(r'\s*"[^"]+"\s*', " ", txt).strip() if nick else ""
        people.append({"name": nick.group(1) if nick else txt, "realName": real,
                       "country": flag.group(1).upper() if flag else None})
    return people


def parse_lp_team(wt):
    out = {"managers": [], "coaches": [], "staff": [], "location": None, "created": None, "website": None}
    start = wt.find("{{Infobox team")
    if start >= 0:
        box = {}
        for line in wt[start:].split("\n")[1:]:
            if line.startswith("}}"):
                break
            m = re.match(r"\|\s*([a-z0-9_]+)\s*=(.*)", line)
            if m:
                box[m.group(1)] = m.group(2).strip()
        out["managers"] = wiki_people(box.get("manager"))
        out["coaches"] = wiki_people(box.get("coaches") or box.get("coach"))
        out["location"] = wiki_clean(box.get("location")) or None
        out["created"] = (box.get("created") or "")[:10] or None
        out["website"] = box.get("website") or None
    for row in re.findall(r"\{\{SquadAutoRow\|([^\n]*)", wt):
        role = re.search(r"\|\s*role\s*=\s*([^|}<]*)", "|" + row)
        if not role or not role.group(1).strip():
            continue
        if re.search(r"\|\s*leavedate\s*=\s*\d", row):
            continue
        get = lambda k: (re.search(r"\|\s*%s\s*=\s*([^|}<]*)" % k, "|" + row) or [None, ""])[1].strip()
        person = {"name": get("id"), "realName": get("name"), "country": get("flag").upper() or None,
                  "role": role.group(1).strip()}
        if not person["name"]:
            continue
        if re.search(r"manager", person["role"], re.I):
            out["managers"].append(person)
        elif re.search(r"coach", person["role"], re.I):
            out["coaches"].append(person)
        else:
            out["staff"].append(person)
    return out


def blast_team(team_id, short=None):
    path = "/rl/team/%s" % team_id[:8] + ("/%s" % urllib.parse.quote(short) if short else "")
    ld = decode_turbo_stream(http_get("https://blast.tv" + path))
    d = loader(ld, "rl.team")
    team = d.get("team") or {}
    people = []
    for p in d.get("teamPlayers") or []:
        age = None
        if isinstance(p.get("dateOfBirth"), (int, float)):
            age = int((time.time() - p["dateOfBirth"] / 1000) / (365.25 * 86400))
        people.append({"name": p.get("nickname"), "country": p.get("nationality"), "age": age,
                       "realName": " ".join(x.strip() for x in (p.get("firstName"), p.get("lastName"))
                                            if x and re.search(r"\w", x)),
                       "role": p.get("role") or "player", "id": p.get("id")})
    results = []
    for t in d.get("teamTournaments") or []:
        r = t.get("teamResult") or {}
        a, b = r.get("prizePositionStart"), r.get("prizePositionEnd")
        place = None if a is None else ("%d." % a if not b or a == b else "%d.–%d." % (a, b))
        results.append({"name": t.get("name"), "ts": iso_to_ts(t.get("endDate") or t.get("startDate")),
                        "place": place, "prize": r.get("prize") if re.search(r"\d", r.get("prize") or "") else None})
    results.sort(key=lambda x: -(x["ts"] or 0))
    transfers = []
    for x in d.get("teamTransferHistoryPromise") or []:
        pl, cur, prev = x.get("player") or {}, x.get("currentTeam") or {}, x.get("previousTeam") or {}
        if cur.get("id") == team.get("id"):
            kind, other = "in", prev
        elif prev.get("id") == team.get("id"):
            kind, other = "out", cur
        else:
            continue
        transfers.append({"player": pl.get("nickname"), "country": pl.get("nationality"), "type": kind,
                          "team": other.get("name"), "teamId": other.get("id"),
                          "role": (cur if kind == "in" else prev).get("role"),
                          "ts": iso_to_ts(x.get("currentTeamFromDate"))})
    transfers.sort(key=lambda t: -(t["ts"] or 0))
    links = team.get("socialLinks") or {}
    return {"id": team.get("id"), "name": team.get("name"), "country": team.get("nationality"),
            "people": people, "results": results[:6], "url": "https://blast.tv" + path,
            "transfers": transfers[:10],
            "website": (links.get("website") or {}).get("url"), "twitter": (links.get("twitter") or {}).get("url")}


TEAM_FILE = os.path.join(ROOT, "teams_cache.json")
STATE_FILE = os.path.join(ROOT, "season_state.json")
TEAM_VERSION = 2
FULL_ROSTER = 3            # Rocket League wird 3 gegen 3 gespielt
WEEK = 7 * 86400
_teams = {}
_teams_lock = threading.Lock()
_team_fetch_locks = {}


def load_teams():
    try:
        with open(TEAM_FILE, encoding="utf-8") as f:
            _teams.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_teams():
    with _teams_lock:
        try:
            tmp = TEAM_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_teams, f, ensure_ascii=False)
            os.replace(tmp, TEAM_FILE)
        except OSError:
            pass


def offseason():
    """Off-Season (Transferfenster) = Ende der Weltmeisterschaft bis zum ersten Turnier der neuen Saison."""
    now = time.time()
    periods = (cached("ranking") or {}).get("periods") or []
    home = (cached("blast_home") or {}).get("tournaments") or {}
    tours = [t for group in home.values() for t in group]
    ends = [x["end"] for x in periods + tours
            if "world championship" in (x.get("name") or "").lower() and x.get("end") and x["end"] <= now]
    if not ends:
        return None
    start = max(ends)
    starts = [x["start"] for x in periods + tours if x.get("start") and x["start"] > start]
    end = min(starts) if starts else None
    try:  # einmal erkanntes Saisonstart-Datum merken (die Quelle zeigt spaeter nur noch neuere Turniere)
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    saved = state.get(str(start))
    if saved and (end is None or saved < end):
        end = saved
    if end and end != saved:
        state[str(start)] = end
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except OSError:
            pass
    return {"start": start, "end": end, "active": end is None or now < end}


def team_stale(entry, off, now):
    """Regeln: unvollstaendige Kader woechentlich, vollstaendige einmal je Off-Season (+ Abschluss zum Saisonstart)."""
    if entry.get("v") != TEAM_VERSION:
        return True
    fetched = entry.get("fetched", 0)
    if len(entry.get("players") or []) < FULL_ROSTER:
        return now - fetched > WEEK
    if off:
        if fetched < off["start"]:
            return True
        if off["end"] and now >= off["end"] and fetched < off["end"]:
            return True
    return False


def registrations(team_id):
    """Fuer welche laufenden/kommenden RLCS-Turniere ist das Team gemeldet?"""
    home = (cached("blast_home") or {}).get("tournaments") or {}
    reg, checked, unknown = [], [], []
    for group in ("live", "upcoming"):
        for t in home.get(group) or []:
            parts = t.get("participants") or []
            if not parts:
                unknown.append(t.get("name"))
                continue
            checked.append(t.get("name"))
            if team_id and team_id in parts:
                reg.append({"name": t.get("name"), "url": t.get("url"), "live": group == "live"})
    return {"registered": reg, "checked": checked, "unknown": unknown}


def with_status(entry):
    """Ergaenzt die gespeicherten Teamdaten um den tagesaktuellen Pruefstatus."""
    out = dict(entry)
    off = offseason()
    n = len(entry.get("players") or [])
    weekly = n < FULL_ROSTER
    out["check"] = {
        "playerCount": n, "fullRoster": FULL_ROSTER, "complete": not weekly,
        "rule": "weekly" if weekly else "offseason",
        "nextCheck": int(entry.get("fetched", 0) + WEEK) if weekly else None,
        "offseason": off,
    }
    out["registration"] = registrations(entry.get("id"))
    return out


def resolve_team(name, team_id=None):
    rk = cached("ranking")
    if not rk or "index" not in rk or "periods" not in rk:
        _cache.pop("ranking", None)
        refresh("ranking")
        rk = cached("ranking") or {}
    index = rk.get("index") or {}
    if team_id:
        hit = next((v for v in index.values() if v["id"] == team_id), None)
        if hit:
            return hit
    for k in name_keys(name):
        if k in index:
            return index[k]
    return None


def fetch_team(name, team_id, lp_title, info):
    out = {"v": TEAM_VERSION, "name": name, "id": team_id or (info or {}).get("id"), "fetched": time.time(),
           "query": {"name": name, "id": team_id, "lp": lp_title},
           "rank": (info or {}).get("rank"), "rating": (info or {}).get("rating"),
           "region": (info or {}).get("region"), "country": None,
           "players": [], "substitutes": [], "coaches": [], "managers": [], "staff": [],
           "results": [], "transfers": [], "links": {}, "location": None, "created": None, "notes": []}
    errors = 0
    if out["id"]:
        try:
            b = blast_team(out["id"], (info or {}).get("short"))
            out["name"] = b["name"] or name
            out["country"] = b["country"]
            out["results"] = b["results"]
            out["transfers"] = b["transfers"]
            out["links"]["BLAST.tv"] = b["url"]
            if b["website"]:
                out["links"]["Website"] = b["website"]
            if b["twitter"]:
                out["links"]["X / Twitter"] = b["twitter"]
            for p in b["people"]:
                group = {"coach": "coaches", "substitute": "substitutes"}.get(p["role"], "players")
                out[group].append(p)
        except Exception as e:
            errors += 1
            out["notes"].append("BLAST.tv nicht erreichbar (%s)." % type(e).__name__)
    else:
        out["notes"].append("Dieses Team ist bei BLAST.tv nicht gelistet – daher kein Kader verfügbar.")
    try:
        title, wt = None, None
        cands = []
        for c in (lp_title, out["name"], name):
            if c and c not in cands:
                cands.append(c)
        for cand in cands:
            title, wt = lp_wikitext(cand)
            if wt:
                break
        if wt:
            lp = parse_lp_team(wt)
            out["links"]["Liquipedia"] = "https://liquipedia.net/rocketleague/" + urllib.parse.quote(title.replace(" ", "_"))
            out["location"], out["created"] = lp["location"], lp["created"]
            if lp["website"] and "Website" not in out["links"] and lp["website"].startswith("http"):
                out["links"]["Website"] = lp["website"]
            have = {norm(c["name"]) for c in out["coaches"]}
            out["coaches"] += [c for c in lp["coaches"] if norm(c["name"]) not in have]
            seen = set()
            for m in lp["managers"]:
                if norm(m["name"]) not in seen:
                    seen.add(norm(m["name"]))
                    out["managers"].append(m)
            out["staff"] = lp["staff"][:8]
        else:
            out["notes"].append("Keine Liquipedia-Seite gefunden – Manager und Organisation daher unbekannt.")
    except Exception as e:
        errors += 1
        out["notes"].append("Liquipedia nicht erreichbar (%s)." % type(e).__name__)
    return out, errors


def team_details(name, team_id=None, lp_title=None, force=False):
    info = resolve_team(name, team_id)
    key = (info or {}).get("id") or team_id or norm(name)
    with _teams_lock:
        lock = _team_fetch_locks.setdefault(key, threading.Lock())
    with lock:  # dasselbe Team nie doppelt gleichzeitig abrufen
        hit = _teams.get(key)
        now = time.time()
        if hit and hit.get("v") == TEAM_VERSION:
            if force and now - hit.get("fetched", 0) < 60:
                return with_status(hit)
            if not force and not team_stale(hit, offseason(), now):
                return with_status(hit)
        out, errors = fetch_team(name, team_id, lp_title, info)
        if errors and hit and hit.get("v") == TEAM_VERSION:
            hit = dict(hit, notes=(hit.get("notes") or []) + ["Aktualisierung fehlgeschlagen – gezeigt wird der letzte bekannte Stand."])
            return with_status(hit)
        if not errors:  # nur vollstaendige Antworten merken
            _teams[key] = out
            save_teams()
        return with_status(out)


def maintenance():
    """Prueft im Hintergrund alle bekannten Teams nach den Regeln in team_stale()."""
    time.sleep(90)
    while True:
        try:
            off, now = offseason(), time.time()
            due = [e for e in list(_teams.values()) if team_stale(e, off, now)][:20]
            for e in due:
                q = e.get("query") or {}
                team_details(q.get("name") or e.get("name") or "", q.get("id") or e.get("id"), q.get("lp"), force=True)
                time.sleep(5)
            if due:
                sys.stderr.write("[teams] %d Team(s) im Hintergrund aktualisiert\n" % len(due))
        except Exception as ex:
            sys.stderr.write("[teams] Hintergrundpruefung fehlgeschlagen: %s\n" % ex)
        time.sleep(6 * 3600)


# ---------------------------------------------------------------- Steam ----
def clean_steam_text(s):
    s = s or ""
    s = re.sub(r"\{STEAM_CLAN_IMAGE\}\S+", "", s)
    s = re.sub(r"\[img\].*?\[/img\]", "", s, flags=re.S | re.I)
    s = re.sub(r"\[previewyoutube[^\]]*\]\[/previewyoutube\]", "", s, flags=re.I)
    s = re.sub(r"\[url=[^\]]*\](.*?)\[/url\]", r"\1", s, flags=re.S | re.I)
    s = re.sub(r"\[h\d\](.*?)\[/h\d\]", lambda m: "\n\n" + m.group(1).strip().upper() + "\n", s, flags=re.S | re.I)
    s = re.sub(r"\[\*\]", "\n• ", s)
    s = re.sub(r"\[p(?: [^\]]*)?\]", "", s, flags=re.I)
    s = re.sub(r"\[/p\]|\[/\*\]|\[/?(?:list|olist|table|tr)[^\]]*\]|\[hr\]", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"\[/?[a-zA-Z*][^\]\n]{0,60}\]", "", s)
    s = strip_tags(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def fetch_steam_news():
    url = ("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
           "?appid=%d&count=40&maxlength=0" % STEAM_APPID)
    data = json.loads(http_get(url))
    official, press = [], []
    for n in data["appnews"]["newsitems"]:
        tags = n.get("tags") or []
        title = n.get("title", "")
        text = clean_steam_text(n.get("contents"))
        is_official = n.get("feedname") == "steam_community_announcements"
        is_patch = "patchnotes" in tags and "patch notes" in title.lower() or \
                   bool(re.search(r"patch notes|hotfix", title, re.I))
        item = {
            "source": "Psyonix" if is_official else (n.get("feedlabel") or "Presse"),
            "kind": "patch" if is_patch else ("dev" if is_official else "press"),
            "title": title, "url": n.get("url"), "ts": n.get("date"),
            "summary": text[:280] + ("…" if len(text) > 280 else ""),
            "image": None,
        }
        if is_official:
            item["body"] = text[:9000] + ("\n…" if len(text) > 9000 else "")
            official.append(item)
        else:
            press.append(item)
    season = None
    for it in official:
        m = re.search(r"Season (\d+)", it["title"])
        if m and it["kind"] != "patch":
            season = {"number": int(m.group(1)), "title": it["title"], "url": it["url"], "ts": it["ts"]}
            break
    return {"official": official, "press": press, "season": season}


def fetch_steam_players():
    url = ("https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
           "?appid=%d" % STEAM_APPID)
    return {"count": json.loads(http_get(url))["response"].get("player_count")}


# ------------------------------------------------------------------ RSS ----
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"


def fetch_reddit():
    # Beide Subreddits in EINEM Abruf: Reddit erlaubt ohne Anmeldung nur ca. 1 Abruf pro Minute.
    root = ET.fromstring(http_get("https://www.reddit.com/r/RocketLeagueEsports+RocketLeague/.rss?limit=50"))
    items = []
    for e in root.findall(ATOM + "entry"):
        link = e.find(ATOM + "link")
        thumb = e.find(MEDIA + "thumbnail")
        author = e.find(ATOM + "author/" + ATOM + "name")
        when = e.findtext(ATOM + "published") or e.findtext(ATOM + "updated")
        cat = e.find(ATOM + "category")
        sub = cat.get("term") if cat is not None else "RocketLeague"
        items.append({
            "source": "r/" + sub, "kind": "community",
            "title": (e.findtext(ATOM + "title") or "").strip(),
            "url": link.get("href") if link is not None else "",
            "ts": iso_to_ts(when),
            "author": author.text if author is not None else "",
            "image": html.unescape(thumb.get("url")) if thumb is not None else None,
            "summary": "",
        })
    return items


def fetch_shift():
    root = ET.fromstring(http_get("https://shiftrle.gg/feed/"))
    items = []
    for it in root.iter("item"):
        try:
            ts = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
        except Exception:
            ts = None
        desc = strip_tags(it.findtext("description") or "")
        desc = re.sub(r"\s*\[…\]$|\s*\[\.\.\.\]$", "…", desc)
        items.append({
            "source": "SHIFT", "kind": "esports", "title": (it.findtext("title") or "").strip(),
            "url": it.findtext("link"), "ts": ts, "image": None,
            "summary": desc[:280] + ("…" if len(desc) > 280 else ""),
            "tags": [c.text for c in it.findall("category") if c.text],
        })
    return items


# ---------------------------------------------------------------- Cache ----
# name -> (Abruf-Funktion, Gueltigkeit in Sekunden, Mindestabstand bei "Aktualisieren")
SOURCES = {
    "matches": (fetch_matches, 600, 120),          # Liquipedia: max. 1 Abruf / 30 s erlaubt
    "blast_home": (fetch_blast_home, 600, 120),
    "ranking": (fetch_blast_ranking, 3600, 600),
    "players": (fetch_players, 3600, 600),
    "blast_news": (fetch_blast_news, 1800, 300),
    "steam_news": (fetch_steam_news, 1800, 120),
    "steam_players": (fetch_steam_players, 300, 60),
    "reddit": (fetch_reddit, 900, 300),
    "shift": (fetch_shift, 3600, 600),
}
SOURCE_LABELS = {
    "matches": "Liquipedia (Spiele)", "blast_home": "BLAST.tv (Turniere)",
    "ranking": "BLAST.tv (Team-Ranking)", "players": "BLAST.tv (Spielerstatistik)",
    "blast_news": "BLAST.tv (News)", "steam_news": "Steam (Psyonix-News)",
    "steam_players": "Steam (Spielerzahl)", "reddit": "Reddit (Community)", "shift": "SHIFT (News)",
}
_cache = {}  # name -> {"data", "fetched", "error", "tried"}
CACHE_FILE = os.path.join(ROOT, "cache.json")
_file_lock = threading.Lock()


def load_cache():
    """Letzten Datenstand von der Platte laden: schneller Start, weniger Abrufe bei den Quellen."""
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            for name, e in json.load(f).items():
                if name in SOURCES and e.get("data") is not None:
                    _cache[name] = {"data": e["data"], "fetched": e.get("fetched", 0), "error": None, "tried": 0}
    except (OSError, ValueError):
        pass


def save_cache():
    with _file_lock:
        try:
            snap = {n: {"data": e["data"], "fetched": e["fetched"]} for n, e in _cache.items() if e.get("data") is not None}
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, CACHE_FILE)
        except OSError:
            pass
_locks = {name: threading.Lock() for name in SOURCES}


def cached(name):
    return (_cache.get(name) or {}).get("data")


def refresh(name, force=False):
    fn, ttl, min_gap = SOURCES[name]
    with _locks[name]:
        entry = _cache.setdefault(name, {"data": None, "fetched": 0, "error": None, "tried": 0})
        now = time.time()
        age = now - entry["fetched"]
        if entry["data"] is not None and age < (min_gap if force else ttl):
            return
        if entry["error"] and now - entry["tried"] < 180:
            return  # nach Fehler (z.B. Drosselung) nicht sofort wieder anfragen
        entry["tried"] = now
        try:
            entry["data"] = fn()
            entry["fetched"] = time.time()
            entry["error"] = None
            save_cache()
        except Exception as e:
            entry["error"] = "%s: %s" % (type(e).__name__, e)
            sys.stderr.write("[%s] Fehler: %s\n" % (name, entry["error"]))


def build_payload(force=False):
    refresh("blast_home", force)  # liefert die Links fuer die Spielerstatistik
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: refresh(n, force), [n for n in SOURCES if n != "blast_home"]))

    steam = cached("steam_news") or {"official": [], "press": [], "season": None}
    news = []
    for key in ("blast_news", "shift"):
        news.extend(cached(key) or [])
    news.extend(steam["press"])
    news.extend([dict(x, body=None) for x in steam["official"]])
    news = [n for n in news if n.get("title")]
    news.sort(key=lambda n: -(n.get("ts") or 0))

    community = list(cached("reddit") or [])
    community.sort(key=lambda n: -(n.get("ts") or 0))

    status = []
    for name in SOURCES:
        e = _cache.get(name) or {}
        status.append({"id": name, "label": SOURCE_LABELS[name], "ok": e.get("error") is None and e.get("data") is not None,
                       "fetched": int(e.get("fetched") or 0), "error": e.get("error")})
    return {
        "generated": int(time.time()),
        "matches": cached("matches") or {"live": [], "upcoming": [], "results": []},
        "tournaments": (cached("blast_home") or {}).get("tournaments") or {"live": [], "upcoming": [], "past": []},
        "ranking": (cached("ranking") or {}).get("teams") or [],
        "playerSets": (cached("players") or {}).get("sets") or [],
        "news": news[:120],
        "official": steam["official"],
        "season": steam["season"],
        "community": community[:60],
        "steamPlayers": (cached("steam_players") or {}).get("count"),
        "status": status,
    }


# --------------------------------------------------------------- Server ----
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon"}


def lan_urls(port):
    """Adressen, unter denen der Hub im selben WLAN erreichbar ist (fuers Handy)."""
    urls = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.0.2.1", 9))  # sendet nichts, ermittelt nur die eigene WLAN-Adresse
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith("127."):
            urls.append("http://%s:%d" % (ip, port))
    except OSError:
        pass
    try:
        host = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3).stdout.strip()
        if re.match(r"^[A-Za-z0-9-]+$", host or ""):
            urls.append("http://%s.local:%d" % (host, port))
    except (OSError, subprocess.SubprocessError):
        pass
    return urls


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/data":
            force = "force=1" in (parsed.query or "")
            try:
                body = json.dumps(build_payload(force), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        if parsed.path == "/api/info":
            body = json.dumps({"urls": lan_urls(self.server.server_address[1])}).encode()
            self._send(200, body, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/team":
            q = urllib.parse.parse_qs(parsed.query or "")
            name = (q.get("name") or [""])[0][:120]
            tid = (q.get("id") or [""])[0]
            if not re.match(r"^[0-9a-f-]{8,36}$", tid):
                tid = None
            if not name and not tid:
                self._send(400, b'{"error":"name fehlt"}', "application/json")
                return
            try:
                body = json.dumps(team_details(name, tid, (q.get("lp") or [""])[0][:160] or None,
                                                    force=(q.get("force") or [""])[0] == "1"), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        rel = "index.html" if parsed.path in ("/", "") else parsed.path.lstrip("/")
        full = os.path.realpath(os.path.join(STATIC, rel))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            self._send(404, b"Nicht gefunden", "text/plain; charset=utf-8")
            return
        with open(full, "rb") as f:
            self._send(200, f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

    def log_message(self, fmt, *args):
        pass


def main():
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    # Ohne --local ist der Hub auch fuer Geraete im selben WLAN erreichbar (Handy).
    host = "127.0.0.1" if "--local" in sys.argv else "0.0.0.0"
    server = ThreadingHTTPServer((host, port), Handler)
    url = "http://localhost:%d" % port
    print("Rocket League Hub laeuft auf %s  (Beenden mit Ctrl+C)" % url, flush=True)
    if host != "127.0.0.1":
        for u in lan_urls(port):
            print("  Fuers Handy im selben WLAN: %s" % u, flush=True)
    load_cache()
    load_teams()
    threading.Thread(target=build_payload, daemon=True).start()  # Cache vorwaermen
    threading.Thread(target=maintenance, daemon=True).start()
    if "--open" in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
