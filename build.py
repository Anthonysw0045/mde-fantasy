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
    {"id": "245347", "name": "MDE Highscore", "type": "points", "scoring": "best1",  # High Score: 주전 6명 × 주간 최고 1경기
     "slots": ["G", "G", "FC", "FC", "FC", "Util"]},
]
CATS = ["FG%", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]
LOWER_BETTER = {"TO"}
BASE = "https://basketball.fantasysports.yahoo.com/nba/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
KST = dt.timezone(dt.timedelta(hours=9))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")
PROJ_CACHE = os.path.join(os.path.dirname(OUT), "proj_cache.json")
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


# ── 이번 주 실제 카테고리 누적 ───────────────────────────────────────────
# 매치업 상세 페이지는 비로그인 시 빈 화면만 내려옴(2026-10-22 확인) → 팀 페이지의 일별 기록
# 'Starting Lineup Totals' 행(그날 주전 합계, Yahoo 매치업 집계와 같은 기준)을 주차 날짜만큼 합산
DAY_COLS = ["FGM/A*", "FG%", "FTM/A*", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]


def day_totals(lid, tid, date):
    """(주차, 그날 주전 합계 {FGM/A* ...}) — 경기 없으면 0"""
    s = get(f"{BASE}{lid}/{tid}?date={date}&stat1=S&stat2=D")
    wk = re.search(r"Week (\d+) vs", s)
    tot = dict.fromkeys(["fgm", "fga", "ftm", "fta", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"], 0.0)
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", s):
        c = cells(tr)
        if len(c) < 2 or c[1] != "Starting Lineup Totals":
            continue
        # 자유투 0/0 이면 FT% 칸이 빈 칸 → 빈 칸을 건너뛰지 말고 FGM/A 위치부터 고정 순서로 읽는다
        i = next(k for k, x in enumerate(c) if re.fullmatch(r"\d+/\d+|-/-", x))
        v = dict(zip(DAY_COLS, c[i:i + len(DAY_COLS)]))
        for k, b in [("fg", "FGM/A*"), ("ft", "FTM/A*")]:
            m = re.fullmatch(r"(\d+)/(\d+)", v.get(b, ""))
            if m:
                tot[k + "m"], tot[k + "a"] = float(m.group(1)), float(m.group(2))
        for k in ["3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]:
            tot[k] = f(v.get(k))
        break
    return (int(wk.group(1)) if wk else None), tot


def week_stats(cfg, week, team_ids):
    """팀별 이번 주 누적 {tid: {FGM/A*, FTM/A*, FG% ... TO}}. 이틀 이상 지난 날은 확정으로 보고 캐시"""
    lid = cfg["id"]
    today = dt.datetime.now(ET).date()
    cache = read_cache()
    dc = cache.setdefault("day", {}).setdefault(lid, {})
    # 주차 시작일: 첫 팀으로 하루씩 거슬러 올라가며 주차가 바뀌는 지점 (주차별로 한 번만)
    sk = f"w{week}-start"
    if sk not in dc:
        start = today
        for k in range(1, 8):
            d = today - dt.timedelta(days=k)
            wk, _ = day_totals(lid, team_ids[0], d.isoformat())
            if wk != week:
                break
            start = d
        dc[sk] = start.isoformat()
    start = dt.date.fromisoformat(dc[sk])
    dates = [start + dt.timedelta(days=k) for k in range((today - start).days + 1)]
    jobs = [(tid, d) for tid in team_ids for d in dates if f"{tid}|{d}" not in dc]

    def run(j):
        time.sleep(0.3)  # Yahoo 999 제한 회피용 속도 조절
        return j, day_totals(lid, j[0], j[1].isoformat())[1]
    with ThreadPoolExecutor(3) as ex:
        got = dict(ex.map(run, jobs))
    out = {}
    for tid in team_ids:
        T = dict.fromkeys(["fgm", "fga", "ftm", "fta", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"], 0.0)
        for d in dates:
            v = dc.get(f"{tid}|{d}") or got[(tid, d)]
            if (today - d).days >= 2:          # 지난 날(통계 정정 여유 하루) → 확정 저장
                dc[f"{tid}|{d}"] = v
            for k in T:
                T[k] += v[k]
        out[tid] = {"FGM/A*": f"{T['fgm']:.0f}/{T['fga']:.0f}", "FTM/A*": f"{T['ftm']:.0f}/{T['fta']:.0f}",
                    "FG%": f"{T['fgm'] / T['fga']:.3f}".lstrip("0") if T["fga"] else "-",
                    "FT%": f"{T['ftm'] / T['fta']:.3f}".lstrip("0") if T["fta"] else "-",
                    **{k: f"{T[k]:.0f}" for k in ["3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]}}
    write_cache(cache)
    return out


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


def proj_start():
    """남은 예상 시작일. ET 19시(첫 경기 무렵) 이후면 오늘 경기는 진행/종료로 보고 내일부터
    — 오늘 기록은 매치업의 실제 누적에 들어가므로 이중 계산 방지"""
    now = dt.datetime.now(ET)
    return now.date() if now.hour < 19 else now.date() + dt.timedelta(days=1)


def project_league(cfg, week, team_ids):
    """남은 기간 팀별 예상 합계 {tid: {...}}"""
    lid, typ = cfg["id"], cfg.get("type", "cat")
    start = proj_start()
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
    if cfg.get("scoring") == "best1":
        return project_best1(cfg, week, team_ids, res, tot)
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


# ── High Score(주전 6명 × 주간 최고 1경기) 예상 ──────────────────────────
# 선수별: 그 주 경기 수 n, 경기당 예상 m(Yahoo 일일 예상 평균), 기복 cv(지난 시즌 경기별 편차/평균)
# 주간 최고 1경기 기대값 ≈ m × (1 + cv × Z[n])  (Z = 표준정규 n개 중 최대값의 기대값)
# 주전 6명(G,G,FC,FC,FC,Util)은 이 기대값이 큰 순으로 선택
Z_MAX = {0: None, 1: 0.0, 2: 0.564, 3: 0.846, 4: 1.029, 5: 1.163, 6: 1.267}
try:
    PLAYER_CV = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_cv.json"), encoding="utf-8"))
except Exception:
    PLAYER_CV = {}
CV_DEFAULT = PLAYER_CV.get("_median", 0.33)


def player_name(cell):
    nm = re.split(r" (?:Player Note|No new player Notes|New Player Note)", cell)[0]
    return re.sub(r" (Q|O|P|D|GTD|INJ|OFS|DTD)$", "", nm).strip()


def project_best1(cfg, week, team_ids, res, tot):
    per = {tid: {} for tid in team_ids}           # tid -> 선수 -> {pos, games:[예상 FP]}
    for (tid, d), (wk, players) in res:
        if week and wk != week:
            continue
        for p in players:
            r = p["row"]
            if p["slot"] in ("IL", "IL+") or not re.search(r"(?:vs|@) [A-Z]{2,4}\b", r.get("Players", "")):
                continue
            if r.get("PTS", "-") in ("-", ""):
                continue
            nm = player_name(r.get("Players", ""))
            per[tid].setdefault(nm, {"pos": p["pos"], "g": []})["g"].append(f(r.get("Fan Pts")))
    for tid in team_ids:
        cands = []
        for nm, v in per[tid].items():
            n = min(len(v["g"]), 6)
            m = sum(v["g"]) / len(v["g"])
            cv = PLAYER_CV.get(nm, CV_DEFAULT)
            cands.append({"name": nm, "pos": v["pos"], "n": n, "v": m * (1 + cv * Z_MAX[n])})
        st = pick_starters(cands, cfg["slots"], key=lambda p: p["v"])
        tot[tid]["fp"] = round(sum(p["v"] for p in st), 1)
        tot[tid]["games"] = sum(p["n"] for p in st)
        tot[tid]["starters"] = [{"name": p["name"], "n": p["n"], "v": round(p["v"], 1)} for p in sorted(st, key=lambda p: -p["v"])]
    tot["_model"] = "best1"
    return tot


PAGES_CACHE_URL = "https://anthonysw0045.github.io/mde-fantasy/proj_cache.json"
ROSTER_MAX_AGE_S = 3600     # 로스터는 1시간마다 (팀 12개 페이지 → Yahoo 요청 절약)


def load_rosters(cfg, team_ids, tx_sig=""):
    """tx_sig = 최근 거래 표시값 — 새 거래(영입·방출)가 생기면 1시간을 기다리지 않고 바로 다시 수집"""
    lid = cfg["id"]
    cache = read_cache()
    rc = cache.setdefault("rosters", {}).get(lid)
    if rc and time.time() - rc["ts"] < ROSTER_MAX_AGE_S and rc.get("tx_sig") == tx_sig:
        return rc["teams"], rc["asof"]
    today = dt.datetime.now(ET).date().isoformat()
    teams = {}
    for tid in team_ids:
        _, players = team_day(lid, tid, today)
        rows = []
        for p in players:
            cell = p["row"].get("Players", "")
            m = re.search(r"\b([A-Z]{2,4}) - [A-Z,]+", cell)
            flag = re.search(r" (Q|O|P|D|GTD|INJ|OFS|DTD) (?:Player Note|No new player Notes|New Player Note)", cell)
            rows.append({"slot": p["slot"], "name": player_name(cell), "nba": m.group(1) if m else "",
                         "pos": ",".join(p["pos"]), "flag": flag.group(1) if flag else ""})
        teams[tid] = rows
        time.sleep(0.5)
    asof = dt.datetime.now(KST).strftime("%m-%d %H:%M")
    cache = read_cache()
    cache.setdefault("rosters", {})[lid] = {"ts": time.time(), "asof": asof, "teams": teams, "tx_sig": tx_sig}
    write_cache(cache)
    return teams, asof


def read_cache():
    """주간 예상·적중 기록 저장소. Actions 캐시가 유실되면 배포된 페이지의 사본에서 복구"""
    try:
        return json.load(open(PROJ_CACHE, encoding="utf-8"))
    except Exception:
        pass
    try:
        return json.loads(get(PAGES_CACHE_URL, tries=1))
    except Exception:
        return {}


def write_cache(cache):
    json.dump(cache, open(PROJ_CACHE, "w", encoding="utf-8"), ensure_ascii=False)


def load_proj(cfg, week, team_ids):
    """주간 예상 = 해당 주차를 처음 본 시점(주차 시작 전)에 한 번 작성하고 끝까지 고정.
    (형님 지시 2026-10-19: '맨 처음 주간 예상'과 '주차 결과'를 비교하기 위함 → 재계산 금지)"""
    cache = read_cache()
    key = f'{cfg["id"]}-w{week}'
    c = cache.get(key)
    # High Score 리그는 '최고 1경기' 모델로 만든 예상만 인정 (2026-10-19 규칙 정정 전 합산 예상은 폐기)
    if c and (cfg.get("scoring") != "best1" or c["tot"].get("_model") == "best1"):
        return c["tot"], c["asof"]
    tot = project_league(cfg, week, team_ids)
    asof = dt.datetime.now(KST).strftime("%m-%d %H:%M")
    cache[key] = {"ts": time.time(), "asof": asof, "tot": tot}
    write_cache(cache)
    return tot, asof


def proj_pair(cfg, tot, aid, bid):
    """두 팀의 주간 예상 비교 → (예상 a, 예상 b, 상세)"""
    ta, tb = tot[str(aid)], tot[str(bid)]
    if cfg.get("type") == "points":
        return round(ta["fp"], 1), round(tb["fp"], 1), {}
    pa, pb = combine_cat(None, ta), combine_cat(None, tb)
    pc = cat_result(pa, pb)
    return (sum(v == "a" for v in pc.values()), sum(v == "b" for v in pc.values()),
            {"pa": pa, "pb": pb, "pc": pc,
             "close": [c for c in CATS if pc[c] in ("a", "b") and close_call(c, pa[c], pb[c])]})


def record_history(cfg, cur_week):
    """끝난 주차마다 '처음 주간 예상' vs '실제 결과' 기록 (한 번만 기록, 이후 고정)"""
    lid = cfg["id"]
    cache = read_cache()
    hist = cache.setdefault("hist", {}).setdefault(lid, {})
    changed = False
    for key in sorted(k for k in cache if re.fullmatch(lid + r"-w\d+", k)):
        w = int(key.split("-w")[1])
        if w >= cur_week or str(w) in hist:
            continue
        try:
            wk, status, mus = parse_matchups(get(f"{BASE}{lid}?matchup_week={w}&module=matchups&lhst=matchups"), lid)
        except Exception as e:
            print("결과 수집 실패", lid, w, e)
            continue
        rows = []
        for mu in mus:
            a, b = mu["teams"]
            pa, pb, _ = proj_pair(cfg, cache[key]["tot"], a["id"], b["id"])
            ra, rb = f(a["score"]), f(b["score"])
            pw = "a" if pa > pb else "b" if pb > pa else "tie"
            rw = "a" if ra > rb else "b" if rb > ra else "tie"
            rows.append({"a": a["name"], "b": b["name"], "proj": [pa, pb], "result": [ra, rb], "hit": pw == rw})
        hist[str(w)] = {"asof": cache[key]["asof"], "rows": rows}
        changed = True
    if changed:
        write_cache(cache)
    return hist


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
    # 주간 예상은 실제 기록과 섞지 않는다 (실제 점수는 매치업 stats/score 로 별도 표시)
    for mu in data["matchups"]:
        a, b = mu["teams"]
        x, y, det = proj_pair(cfg, tot, a["id"], b["id"])
        a["prem_games"], b["prem_games"] = tot[a["id"]]["games"], tot[b["id"]]["games"]
        if cfg.get("type") == "points":
            a["pproj"], b["pproj"] = x, y
            a["pstarters"], b["pstarters"] = tot[a["id"]].get("starters"), tot[b["id"]].get("starters")
            continue
        a["pstats"], b["pstats"] = det["pa"], det["pb"]
        mu["pcats"], mu["pclose"] = det["pc"], det["close"]
        a["pcw"], b["pcw"] = x, y


def league(cfg):
    lid = cfg["id"]
    home = get(BASE + lid)
    week, status, mus = parse_matchups(home, lid)
    ws = {}
    if cfg.get("type") != "points":
        try:
            ws = week_stats(cfg, week, sorted({t["id"] for mu in mus for t in mu["teams"]}, key=int))
        except Exception as e:
            print("주간 누적 실패", lid, e)
    for mu in mus:
        a, b = mu["teams"]
        if a["id"] in ws and b["id"] in ws:
            det = [ws[a["id"]], ws[b["id"]]]
            a["stats"], b["stats"] = ({c: d.get(c, "-") for c in ["FGM/A*", "FTM/A*"] + CATS} for d in det)
            mu["cats"] = cat_result(det[0], det[1])
            w = sum(v == "a" for v in mu["cats"].values()); l = sum(v == "b" for v in mu["cats"].values())
            t = sum(v == "tie" for v in mu["cats"].values())
            a["cw"], b["cw"], mu["ties"] = w, l, t
    tx = parse_transactions(home)
    out = {"id": lid, "name": cfg["name"], "type": cfg.get("type", "cat"), "url": BASE + lid,
           "week": week, "status": status,
           "matchups": mus, "standings": parse_standings(home, lid), "transactions": tx}
    add_projection(cfg, out)  # 고정된 주간 예상 (끝난 주차도 그대로 표시)
    try:
        out["rosters"], out["rosters_asof"] = load_rosters(cfg, [s["id"] for s in out["standings"]],
                                                           json.dumps(tx[:1], ensure_ascii=False))
    except Exception as e:
        print("로스터 실패", lid, e)
    try:
        out["history"] = record_history(cfg, week)
    except Exception as e:
        print("적중 기록 실패", lid, e)
    out["warn"] = health(cfg, out)
    return out


# ── 자체 점검 (2026-10-22 재발 방지) ─────────────────────────────────────
# Yahoo 페이지 구조가 바뀌면 수집은 '성공'처럼 끝나도 특정 칸만 조용히 비거나 멈출 수 있음
# (예: 매치업 상세가 비로그인 빈 화면으로 바뀌어 실제 기록이 '경기 시작 전'에 멈춤).
# 매 수집마다 항목별로 점검 → 화면 상단 경고 + Actions 실행 실패(빨간 X)로 드러나게 한다.
def health(cfg, out):
    w = []
    mus, n = out.get("matchups") or [], 2 * len(out.get("matchups") or [])
    if not mus:
        w.append("매치업을 읽지 못함")
    if len(out.get("standings") or []) != n:
        w.append(f"순위 {len(out.get('standings') or [])}팀 (예상 {n}팀)")
    if cfg.get("type") != "points":
        for mu in mus:
            a, b = mu["teams"]
            ys = f(a["score"]) + f(b["score"])          # Yahoo 표시 카테고리 승수
            if ys > 0 and not a.get("stats"):
                w.append(f"실제 기록 미수집: {a['name']} vs {b['name']}")
            elif a.get("stats") and abs(a.get("cw", 0) - f(a["score"])) + abs(b.get("cw", 0) - f(b["score"])) >= 4:
                w.append(f"실제 기록이 Yahoo 점수와 다름: {a['name']} {a.get('cw')}-{b.get('cw')} (Yahoo {a['score']}-{b['score']})")
    if not out.get("proj_asof"):
        w.append("주간 예상 없음")
    R = out.get("rosters")
    if not R:
        w.append("로스터 미수집")
    else:
        short = [s["name"] for s in out.get("standings", []) if len(R.get(s["id"], [])) < 10]
        if short:
            w.append("로스터 인원 이상: " + ", ".join(short))
    return w


def previous():
    """직전 배포본 — 리그 전체 수집 실패 시 빈 화면 대신 마지막 정상값을 보여주기 위함"""
    try:
        d = json.loads(get(PAGES_CACHE_URL.replace("proj_cache.json", "data.json"), tries=2))
        return {l["id"]: l for l in d["leagues"] if not l.get("error")}, d.get("updated", "")
    except Exception:
        return {}, ""


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
            prev, when = previous()
            if cfg["id"] in prev:
                old = prev[cfg["id"]]
                old["warn"] = [f"이번 수집 실패 → {old.get('stale') or when} 기준 이전 값 표시 ({e})"]
                old["stale"] = old.get("stale") or when
                data["leagues"].append(old)
            else:
                data["leagues"].append({"id": cfg["id"], "name": cfg["name"], "error": str(e),
                                        "warn": [f"수집 실패: {e}"]})
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved", OUT)
    for l in data["leagues"]:
        for m in l.get("warn") or []:
            print("::warning::" + l["name"] + " — " + m)


if __name__ == "__main__":
    main()
