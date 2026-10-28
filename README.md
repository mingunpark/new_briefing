# new_briefing

Daily News Briefing(예약 작업)용 데이터 수집 저장소.

- `collector/collect.py` — 공개 데이터 수집 (시장 지표, 미국채 금리, TGA, HN, GitHub, Hugging Face, Reddit)
- `.github/workflows/collect.yml` — 매일 06:30 KST 실행, 결과를 커밋
- `data/latest.json` — 브리핑 작업이 읽는 최신 데이터
- `data/archive/YYYY-MM-DD.json` — 일자별 보관본 (60일)
- `data/state/github_stars.json` — GitHub star 증가량 계산용 스냅샷 (30일)

브리핑 작업은 아래 주소로 읽는다.

```
https://raw.githubusercontent.com/mingunpark/new_briefing/main/data/latest.json
```

소스별 성공/실패는 `status`, 실패 목록은 `failed_sources` 에 기록된다.
수동 실행: Actions 탭 → collect-briefing-data → Run workflow
