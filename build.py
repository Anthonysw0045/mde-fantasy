"""MDE 판타지 리그 실시간 대시보드 — 데이터 수집기

Yahoo 공개 리그 페이지(로그인 불필요)에서 매치업·카테고리 점수·순위·거래를 모아
docs/data.json 으로 저장한다. docs/index.html 이 이 JSON을 읽어 화면을 그린다.

리그 추가: LEAGUES 에 한 줄 추가 (리그가 '공개'여야 함 — 비공개는 Error #152)
실행: py -I dashboard/build.py   (GitHub Actions 에서는 python dashboard/build.py)
"""
import datetime as dt, html, json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

LEAGUES = [
    {"id": "90403", "name": "MDE Human League", "type": "cat",        # 9-cat H2H
     "slots": ["PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util"]},
    {"id": "245347", "name": "MDE Highscore", "type": "points",       # 포인트(High Score)
     "slots": ["G", "G", "FC", "FC", "FC", "Util"]},
]
CATS = ["FG%", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]
LOWER_BETTER = {"TO"}
BASE = "https://basketball.fantasysports.yahoo.com/nba/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
KST = dt.timezone(dt.timedelta(hours=9))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")
PROJ_CACHE = os.path.join(os.path.dirname(OUT), "proj_cache.json")
PROJ_MAX_AGE_H = 4          # 예상치는 무거움(팀×날짜 페이지) → 4시간마다만 재계산
ET = ZoneInfo("America/New_York")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                if "login.yahoo.com" in r.geturl():
                    raise PermissionError("비공개 리그 (로그인 필요)")
                return r.read().decode("utf-8", "ignore")
        except PermissionError:
            raise
        except Exception as e:
            if i == tries - 1:
                raise
            # HTTP 999 = Yahoo 요청 과다 제한 → 길게 쉬었다 재시도
            time.sleep(20 * (i + 1) if "999" in str(e) else 3)


def text(fragment):
    t = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def cells(row):
    return [text(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]


def parse_matchups(s, lid):
    m = re.search(r"matchup_week=(\d+)&module=matchups&lhst=matchups' selected", s)
    week = int(m.group(1)) if m else None
    m = re.search(r'Ta-end Fz-xxs"><span[^>]*>([^<]*)<', s)
    status = html.unescape(m.group(1)).strip() if m else ""
    out = []
    for li in re.findall(r"(?is)<li class='Linkable Listitem[^']*'[^>]*data-target='[^']*mid1=\d+&mid2=\d+'>.*?(?=<li class='Linkable Listitem|</ul>)", s):
        ids = re.search(r"mid1=(\d+)&mid2=(\d+)", li).groups()
        names = re.findall(r'class="F-link" href="[^"]*/nba/%s/(\d+)">(.*?)</a>' % lid, li)
        logos = re.findall(r'<img class="Ta-c Avatar-med Grid-u" src="([^"]+)"', li)
        recs = re.findall(r"<div class='Fz-xxs F-shade'>([^<]*)</div>", li)
        scores = re.findall(r"<div class='Fz-lg[^']*'>([^<]*)</div>", li)
        projs = re.findall(r"<div class='F-shade\s*'>([^<]*)</div>", li)
        teams = []
        for k, tid in enumerate(ids):
            nm = dict(names).get(tid, tid)
            teams.append({"id": tid, "name": html.unescape(nm).strip(),
                          "logo": logos[k] if k < len(logos) else "",
                          "record": recs[k].strip() if k < len(recs) else "",
                          "score": scores[k].strip() if k < len(scores) else "",
                          "proj": projs[k].strip() if k < len(projs) else ""})
        out.append({"teams": teams})
    return week, status, out


def parse_standings(s, lid):
    m = re.search(r'(?is)<table[^>]*id="standingstable".*?</table>', s)
    if not m:
        return []
    head = cells(re.search(r"(?is)<thead>(.*?)</thead>", m.group(0)).group(1))
    key = {"Rank": "rank", "Team": "name", "W-L-T": "wlt", "Pct": "pct", "GB": "gb", "Pts": "pts",
           "Last Week": "last", "Waiver": "waiver", "Moves": "moves"}
    rows = []
    for tr in re.findall(r"(?is)<tr class=\"Linkable[^\"]*\"[^>]*>(.*?)</tr>", m.group(0)):
        tid = re.search(r"/nba/%s/(\d+)" % lid, tr).group(1)
        logo = re.search(r'<img[^>]*src="([^"]+)"', tr)
        row = {"id": tid, "logo": logo.group(1) if logo else ""}
        for h, v in zip(head, cells(tr)):
            if h in key:
                row[key[h]] = v
        rows.append(row)
    return rows


def parse_matchup_detail(s):
    for t in re.findall(r"(?is)<table.*?</table>", s):
        if "FGM/A" not in t:
            continue
        trs = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", t)
        head = cells(trs[0])
        res = []
        for tr in trs[1:3]:
            c = cells(tr)
            res.append({h: v for h, v in zip(head, c) if h})
        return res
    return []


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cat_result(a, b):
    """카테고리별 승자: 'a' / 'b' / 'tie' / None(미시작)"""
    out = {}
    for c in CATS:
        x, y = num(a.get(c)), num(b.get(c))
        if x is None or y is None:
            out[c] = None
        elif x == y:
            out[c] = "tie"
        elif (x < y) == (c in LOWER_BETTER):
            out[c] = "a"
        else:
            out[c] = "b"
    return out


def parse_transactions(s):
    """리그 홈 'Recent Transactions' 모듈 (시각은 Yahoo 표기 그대로 — 미국 시간)"""
    m = re.search(r'(?is)<table class="Table[^"]*Tst-transaction-table">(.*?)</table>', s)
    if not m:
        return []
    items = []
    for tr in re.findall(r"(?is)<tr>(.*?)</tr>", m.group(1)):
        kinds = re.findall(r'<span class="F-icon[^"]*" title="([^"]+)"', tr)
        moves = []
        for blk in re.findall(r'(?is)<div class="Pbot-xs">(.*?)</h6>', tr):
            pl = re.search(r'(?is)<a href="https://sports.yahoo.com/nba/players/\d+"[^>]*>(.*?)</a>', blk)
            pos = re.search(r'(?is)<span class="F-position[^"]*">(.*?)</span>', blk)
            how = re.search(r'(?is)<h6[^>]*>(.*)$', blk)
            moves.append({"player": text(pl.group(1)) if pl else "", "pos": text(pos.group(1)) if pos else "",
                          "how": text(how.group(1)) if how else ""})
        team = re.findall(r'(?is)class="Tst-team-name"[^>]*>(.*?)</a>', tr)
        when = re.search(r'(?is)F-timestamp[^>]*>(.*?)</span>', tr)
        for k, mv in enumerate(moves):
            mv["kind"] = {"Added Player": "add", "Dropped Player": "drop", "Traded Player": "trade"}.get(
                kinds[k] if k < len(kinds) else "", "etc")
        items.append({"moves": moves, "teams": [text(t) for t in team], "time": text(when.group(1)) if when else ""})
    return items[:20]


# ── 예상 우세 (풀 출전 가정) ─────────────────────────────────────────────
# 남은 날짜마다 각 팀 페이지의 Yahoo 일일 예상치(Projected Stats: Today)를 받아
# 경기 있는 선수를 포지션 칸 수만큼 전원 선발했다고 보고 합산한다. (IL 제외)
ELIG = {"G": {"PG", "SG", "G"}, "F": {"SF", "PF", "F"}, "FC": {"SF", "PF", "F", "C", "FC"}}


def can_play(slot, pos):
    if slot == "Util":
        return True
    return bool(set(pos) & ELIG.get(slot, {slot}))


def team_day(lid, tid, date):
    s = get(f"{BASE}{lid}/{tid}?date={date}&stat1=P&stat2=P")
    wk = re.search(r"Week (\d+) vs", s)
    players = []
    for t in re.findall(r"(?is)<table.*?</table>", s):
        head = None
        for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", t):
            c = cells(tr)
            if "Players" in c and "Pos" in c:
                head = c
                continue
            if not head or "ysf-player-name" not in tr:
                continue
            # 데이터 행은 Action 칸이 아이콘 2칸 → 'Action' 이후 열은 한 칸씩 밀림
            ai = head.index("Action") if "Action" in head else 2
            row = {}
            for i, h in enumerate(head):
                j = i if i <= ai else i + 1
                row[h] = c[j] if j < len(c) else ""
            pm = re.search(r"\b[A-Z]{2,4} - ([A-Z,]+)", c[1])
            players.append({"slot": c[0], "pos": pm.group(1).split(",") if pm else [], "row": row})
    return (int(wk.group(1)) if wk else None), players


def pick_starters(players, slots, key):
    ps = sorted(players, key=key, reverse=True)
    match = {}

    def tr(i, seen):
        for j, sl in enumerate(slots):
            if j in seen or not can_play(sl, ps[i]["pos"]):
                continue
            seen.add(j)
            if j not in match or tr(match[j], seen):
                match[j] = i
                return True
        return False
    for i in range(len(ps)):
        tr(i, set())
    return [ps[i] for i in match.values()]


def f(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0.0


def project_league(cfg, week, team_ids):
    """남은 기간 팀별 예상 합계 {tid: {...}}"""
    lid, typ = cfg["id"], cfg.get("type", "cat")
    now = dt.datetime.now(ET)
    # ET 19시 이후면 오늘 경기는 진행/종료로 보고 내일부터 (실제 누적은 매치업 점수에 반영됨)
    start = now.date() if now.hour < 19 else now.date() + dt.timedelta(days=1)
    # 이번 주에 속한 날짜만: 첫 팀으로 날짜를 넘겨가며 주차가 바뀌는 지점 확인 (요청 수 절약)
    first, dates, res = team_ids[0], [], []
    for k in range(8):
        d = start + dt.timedelta(days=k)
        wk, players = team_day(lid, first, d.isoformat())
        if week and wk != week:
            break
        dates.append(d)
        res.append(((first, d), (wk, players)))
    jobs = [(tid, d) for tid in team_ids[1:] for d in dates]

    def run(j):
        time.sleep(0.3)  # Yahoo 999 제한 회피용 속도 조절
        return j, team_day(lid, j[0], j[1].isoformat())
    with ThreadPoolExecutor(3) as ex:
        res += list(ex.map(run, jobs))
    tot = {tid: {"games": 0, "fgm": 0, "fga": 0, "ftm": 0, "fta": 0, "3PTM": 0, "PTS": 0, "REB": 0, "AST": 0,
                 "ST": 0, "BLK": 0, "TO": 0, "fp": 0.0} for tid in team_ids}
    for (tid, d), (wk, players) in res:
        if week and wk != week:
            continue
        act = []
        for p in players:
            r = p["row"]
            if p["slot"] in ("IL", "IL+"):
                continue
            # 그날 경기 있는 선수만 (선수칸에 'vs XXX'/'@ XXX' 표시). 경기 없는 날 Yahoo는 시즌 예상치를 보여줌 → 제외
            if not re.search(r"(?:vs|@) [A-Z]{2,4}\b", r.get("Players", "")):
                continue
            if typ == "points":
                if r.get("PTS", "-") in ("-", ""):
                    continue
                p["v"] = f(r.get("Fan Pts"))
            else:
                if r.get("FGM/A*", "-/-") in ("-/-", ""):
                    continue
                p["v"] = f(r.get("PTS"))
            act.append(p)
        T = tot[tid]
        for p in pick_starters(act, cfg["slots"], key=lambda p: p["v"]):
            r = p["row"]
            T["games"] += 1
            if typ == "points":
                T["fp"] += p["v"]
            else:
                fg = r.get("FGM/A*", "0/0").split("/")
                ft = r.get("FTM/A*", "0/0").split("/")
                T["fgm"] += f(fg[0]); T["fga"] += f(fg[1]); T["ftm"] += f(ft[0]); T["fta"] += f(ft[1])
                for c in ["3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]:
                    T[c] += f(r.get(c))
    return tot


def load_proj(cfg, week, team_ids):
    try:
        cache = json.load(open(PROJ_CACHE, encoding="utf-8"))
    except Exception:
        cache = {}
    key = f'{cfg["id"]}-w{week}'
    c = cache.get(key)
    if c and time.time() - c["ts"] < PROJ_MAX_AGE_H * 3600:
        return c["tot"], c["asof"]
    tot = project_league(cfg, week, team_ids)
    asof = dt.datetime.now(KST).strftime("%m-%d %H:%M")
    cache = {k: v for k, v in cache.items() if not k.startswith(cfg["id"] + "-w")}
    cache[key] = {"ts": time.time(), "asof": asof, "tot": tot}
    json.dump(cache, open(PROJ_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return tot, asof


def combine_cat(stats, rem):
    """실제 누적 + 남은 예상 → 최종 예상 카테고리"""
    def pair(v):
        try:
            a, b = str(v).split("/")
            return f(a), f(b)
        except ValueError:
            return 0.0, 0.0
    stats = stats or {}
    fgm, fga = pair(stats.get("FGM/A*", "0/0"))
    ftm, fta = pair(stats.get("FTM/A*", "0/0"))
    fgm += rem["fgm"]; fga += rem["fga"]; ftm += rem["ftm"]; fta += rem["fta"]
    out = {"FG%": round(fgm / fga, 3) if fga else 0, "FT%": round(ftm / fta, 3) if fta else 0}
    for c in ["3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]:
        out[c] = round(f(stats.get(c, 0)) + rem[c], 1)
    return out


def close_call(c, x, y):
    if c in ("FG%", "FT%"):
        return abs(x - y) < 0.01
    return abs(x - y) <= max(1.5, 0.05 * max(abs(x), abs(y)))


def add_projection(cfg, data):
    ids = sorted({t["id"] for mu in data["matchups"] for t in mu["teams"]}, key=int)
    try:
        tot, asof = load_proj(cfg, data["week"], ids)
    except Exception as e:
        print("예상치 실패", cfg["id"], e)
        return
    tot = {str(k): v for k, v in tot.items()}
    data["proj_asof"] = asof
    for mu in data["matchups"]:
        a, b = mu["teams"]
        if cfg.get("type") == "points":
            for t in (a, b):
                t["pproj"] = round(f(t.get("score")) + tot[t["id"]]["fp"], 1)
                t["prem_games"] = tot[t["id"]]["games"]
            continue
        pa, pb = combine_cat(a.get("stats"), tot[a["id"]]), combine_cat(b.get("stats"), tot[b["id"]])
        a["pstats"], b["pstats"] = pa, pb
        a["prem_games"], b["prem_games"] = tot[a["id"]]["games"], tot[b["id"]]["games"]
        pc = cat_result(pa, pb)
        mu["pcats"] = pc
        mu["pclose"] = [c for c in CATS if pc[c] in ("a", "b") and close_call(c, pa[c], pb[c])]
        a["pcw"] = sum(v == "a" for v in pc.values())
        b["pcw"] = sum(v == "b" for v in pc.values())


def league(cfg):
    lid = cfg["id"]
    home = get(BASE + lid)
    week, status, mus = parse_matchups(home, lid)
    for mu in mus:
        a, b = mu["teams"]
        if cfg.get("type") == "points":
            continue
        try:
            det = parse_matchup_detail(get(f"{BASE}{lid}/matchup?week={week}&mid1={a['id']}&mid2={b['id']}"))
        except Exception as e:
            print("matchup 실패", lid, a["id"], b["id"], e)
            det = []
        if len(det) == 2:
            a["stats"], b["stats"] = ({c: d.get(c, "-") for c in ["FGM/A*", "FTM/A*"] + CATS} for d in det)
            mu["cats"] = cat_result(det[0], det[1])
            w = sum(v == "a" for v in mu["cats"].values()); l = sum(v == "b" for v in mu["cats"].values())
            t = sum(v == "tie" for v in mu["cats"].values())
            a["cw"], b["cw"], mu["ties"] = w, l, t
    tx = parse_transactions(home)
    out = {"id": lid, "name": cfg["name"], "type": cfg.get("type", "cat"), "url": BASE + lid,
           "week": week, "status": status,
           "matchups": mus, "standings": parse_standings(home, lid), "transactions": tx}
    if status != "Final":
        add_projection(cfg, out)
    return out


def main():
    data = {"updated": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"), "leagues": []}
    try:  # 화면 버전 → 옛 화면이 캐시된 브라우저가 스스로 새로고침하도록
        data["page_ver"] = re.search(r'PAGE_VER="([^"]+)"', open(os.path.join(os.path.dirname(OUT), "index.html"), encoding="utf-8").read()).group(1)
    except Exception:
        pass
    for cfg in LEAGUES:
        try:
            data["leagues"].append(league(cfg))
            print("OK", cfg["id"])
        except Exception as e:
            print("리그 실패", cfg["id"], e)
            data["leagues"].append({"id": cfg["id"], "name": cfg["name"], "error": str(e)})
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved", OUT)


if __name__ == "__main__":
    main()
