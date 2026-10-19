"""MDE 판타지 리그 실시간 대시보드 — 데이터 수집기

Yahoo 공개 리그 페이지(로그인 불필요)에서 매치업·카테고리 점수·순위·거래를 모아
docs/data.json 으로 저장한다. docs/index.html 이 이 JSON을 읽어 화면을 그린다.

리그 추가: LEAGUES 에 한 줄 추가 (리그가 '공개'여야 함 — 비공개는 Error #152)
실행: py -I dashboard/build.py   (GitHub Actions 에서는 python dashboard/build.py)
"""
import datetime as dt, html, json, os, re, sys, time, urllib.request

LEAGUES = [
    {"id": "90403", "name": "MDE Human League", "type": "cat"},      # 9-cat H2H
    {"id": "245347", "name": "MDE Highscore", "type": "points"},     # 포인트(High Score)
]
CATS = ["FG%", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]
LOWER_BETTER = {"TO"}
BASE = "https://basketball.fantasysports.yahoo.com/nba/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
KST = dt.timezone(dt.timedelta(hours=9))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                if "login.yahoo.com" in r.geturl():
                    raise PermissionError("비공개 리그 (로그인 필요)")
                return r.read().decode("utf-8", "ignore")
        except PermissionError:
            raise
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3)


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
    return {"id": lid, "name": cfg["name"], "type": cfg.get("type", "cat"), "url": BASE + lid,
            "week": week, "status": status,
            "matchups": mus, "standings": parse_standings(home, lid), "transactions": tx}


def main():
    data = {"updated": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"), "leagues": []}
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
