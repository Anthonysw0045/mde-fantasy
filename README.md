# MDE 판타지 현황판

Yahoo 판타지 농구 공개 리그의 이번 주 매치업·카테고리 점수·순위·거래를 보여주는 정적 페이지.

- `build.py` — Yahoo 공개 리그 페이지 수집 → `docs/data.json`
- `docs/index.html` — 화면 (GitHub Pages, `/docs` 폴더)
- `.github/workflows/update.yml` — 5분마다 수집 → GitHub Pages 직접 배포 (예상 우세는 4시간마다 재계산, 풀 출전 가정)

리그 추가: `build.py` 의 `LEAGUES` 에 `{"id": "리그번호", "name": "표시 이름"}` 추가 (리그가 공개 상태여야 함).
