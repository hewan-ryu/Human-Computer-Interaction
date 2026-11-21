"""
네이버 블로거 월별 샘플 크롤러 (계절·월별 트렌드 분석용)

블로거마다 2026년 1월~11월 글 목록을 모은 뒤,
  - 달마다 무작위로 8개씩 고르고 (그 달 글이 8개 미만이면 전부)
  - 고른 글이 모두 합쳐 30개 이하인 블로거는 제외하고
  - 조건을 만족한 블로거가 100명이 될 때까지 다음 블로거로 넘어가며
본문을 크롤링한다. (블로거 1명당 최대 8 x 11 = 88개)

사용법:
    python crawl_monthly_sample.py                       # 같은 폴더의 blog_ids.txt 사용
    python crawl_monthly_sample.py --id-file blog_ids.txt -o monthly_posts
    python crawl_monthly_sample.py --target 100 --per-month 8 --min-posts 31 --seed 42

출력 (-o 폴더, 기본 monthly_posts):
    {blog_id}.jsonl        블로거별 글 (한 줄 = 글 1개)
                           {"blog_id","log_no","title","published_at","url","sample_month","body"}
    crawl_summary.csv      블로거별 결과 (포함/제외/실패, 달별 글 수)

중간에 끊겨도 다시 실행하면 이미 끝난 블로거는 건너뛰고 이어서 진행한다.
"""

import argparse
import csv
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote_plus

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- 설정
KST = timezone(timedelta(hours=9))
START = datetime(2026, 1, 1, tzinfo=KST)
END = datetime(2026, 12, 1, tzinfo=KST)               # 12월 1일 0시 전까지 = 11월 말까지
MONTHS = [f"2026-{m:02d}" for m in range(1, 12)]      # 2026-01 ~ 2026-11

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://blog.naver.com/",
}
INVISIBLE = re.compile(r"[​‌‍﻿]")


def clean(text: str) -> str:
    return INVISIBLE.sub("", text or "").replace("\xa0", " ").strip()


# ---------------------------------------------------------------- 날짜 (기존 크롤러와 같은 방식)
def parse_date(raw: str, now: datetime) -> str:
    """'2026. 11. 3. 7:50', '2026. 11. 3.', 'n시간 전', '어제' 등 → ISO 8601 (+09:00)"""
    raw = clean(raw)
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{1,2}):(\d{2})", raw)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        return datetime(y, mo, d, h, mi, tzinfo=KST).isoformat()
    m = re.search(r"(\d+)\s*분\s*전", raw)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).replace(second=0, microsecond=0).isoformat()
    m = re.search(r"(\d+)\s*시간\s*전", raw)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).replace(second=0, microsecond=0).isoformat()
    if "방금" in raw:
        return now.replace(second=0, microsecond=0).isoformat()
    if "어제" in raw:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?", raw)
    if m:
        y, mo, d = map(int, m.groups())
        return datetime(y, mo, d, tzinfo=KST).isoformat()
    return ""


def to_dt(iso: str):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 요청 (실패 시 잠시 쉬었다가 재시도)
def get(session: requests.Session, url: str, params=None, retries: int = 3) -> requests.Response:
    for attempt in range(retries):
        try:
            res = session.get(url, params=params, headers=HEADERS, timeout=15)
            if res.status_code in (429, 500, 502, 503):
                raise requests.HTTPError(f"HTTP {res.status_code}")
            res.raise_for_status()
            return res
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


# ---------------------------------------------------------------- 1단계: 기간 안의 글 목록
def list_posts_in_period(blog_id: str, session: requests.Session, delay: float, max_pages: int = 300) -> list[dict]:
    """글 목록 페이지를 최신순으로 넘기며 2026-01-01 ~ 2026-11-30 글만 모은다."""
    now = datetime.now(KST)
    posts, seen = [], set()
    for page in range(1, max_pages + 1):
        res = get(session, "https://blog.naver.com/PostTitleListAsync.naver",
                  params={"blogId": blog_id, "currentPage": page, "countPerPage": 30})
        data = json.loads(res.text.replace("\\'", "'"))   # 응답에 JSON에 맞지 않는 이스케이프가 섞여 있음
        items = data.get("postList", [])
        new = [p for p in items if str(p.get("logNo")) not in seen]
        if not new:                                         # 마지막 페이지를 지나면 같은 글이 반복되거나 비어 있음
            break

        oldest_on_page = None
        for p in new:
            log_no = str(p["logNo"])
            seen.add(log_no)
            published = parse_date(unquote_plus(p.get("addDate", "")), now)
            dt = to_dt(published)
            if dt is None:
                continue
            oldest_on_page = dt if oldest_on_page is None else min(oldest_on_page, dt)
            if START <= dt < END:
                posts.append({"log_no": log_no,
                              "title": clean(unquote_plus(p.get("title", ""))),
                              "list_date": published,
                              "month": dt.strftime("%Y-%m")})

        if oldest_on_page is not None and oldest_on_page < START:   # 2026년 이전까지 내려왔으면 그만
            break
        time.sleep(delay)
    return posts


# ---------------------------------------------------------------- 2단계: 달마다 무작위로 고르기
def sample_by_month(posts: list[dict], per_month: int, rng: random.Random) -> dict[str, list[dict]]:
    by_month = {m: [] for m in MONTHS}
    for p in posts:
        if p["month"] in by_month:
            by_month[p["month"]].append(p)
    sampled = {}
    for m, items in by_month.items():
        items = sorted(items, key=lambda x: x["log_no"])    # 같은 seed면 항상 같은 결과가 나오게 정렬 후 추출
        sampled[m] = items if len(items) <= per_month else rng.sample(items, per_month)
    return sampled


# ---------------------------------------------------------------- 3단계: 본문 크롤링 (기존 크롤러의 parse_post와 같은 형식)
def fetch_post(blog_id: str, log_no: str, session: requests.Session) -> dict:
    res = get(session, f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}")
    soup = BeautifulSoup(res.text, "html.parser")
    now = datetime.now(KST)

    title_el = (soup.select_one(".se-title-text") or soup.select_one(".pcol1 .htitle")
                or soup.select_one(".se_title .se_textarea"))
    title = clean(title_el.get_text(" ")) if title_el else ""

    date_el = soup.select_one(".se_publishDate") or soup.select_one(".date")
    published_at = parse_date(date_el.get_text(), now) if date_el else ""

    body = [title, ""] if title else []
    container = soup.select_one(".se-main-container")
    if container:
        for p in container.select(".se-text-paragraph"):
            body.append(clean(p.get_text()))
    else:
        old = soup.select_one("#postViewArea") or soup.select_one(".se_component_wrap")
        if old:
            for line in old.get_text("\n").split("\n"):
                body.append(clean(line))
    while body and body[-1] == "":
        body.pop()

    return {"blog_id": blog_id, "log_no": log_no, "title": title, "published_at": published_at,
            "url": f"https://blog.naver.com/{blog_id}/{log_no}", "body": body}


# ---------------------------------------------------------------- 요약 파일
SUMMARY_COLS = ["blog_id", "status", "posts_in_period", "sampled", "saved", *MONTHS, "note"]


def load_summary(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig") as f:
        return {r["blog_id"]: r for r in csv.DictReader(f)}


def save_summary(path: Path, rows: dict[str, dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:     # utf-8-sig: 엑셀에서 한글 안 깨짐
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows.values())


# ---------------------------------------------------------------- 실행
def read_ids(id_file: str | None) -> list[str]:
    path = Path(id_file) if id_file else Path(__file__).with_name("blog_ids.txt")
    if not path.exists():
        raise SystemExit(f"블로거 ID 파일을 찾을 수 없습니다: {path}")
    ids = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue                                                 # 빈 줄 무시
        line = re.sub(r"^https?://(m\.)?blog\.naver\.com/", "", line).strip("/").split("/")[0]
        ids.append(line)
    return list(dict.fromkeys(ids))                                  # 중복 ID 제거, 순서 유지


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id-file", help="블로거 ID 목록 txt (기본: 같은 폴더의 blog_ids.txt)")
    ap.add_argument("-o", "--out", default="monthly_posts", help="저장 폴더")
    ap.add_argument("--target", type=int, default=100, help="모을 블로거 수 (기본 100)")
    ap.add_argument("--per-month", type=int, default=8, help="달마다 고를 글 수 (기본 8)")
    ap.add_argument("--min-posts", type=int, default=31,
                    help="블로거 포함 기준: 고른 글이 이 개수 이상이어야 함 (기본 31 = 30개 초과)")
    ap.add_argument("--seed", type=int, default=42, help="무작위 추출 seed (같으면 같은 글이 뽑힘)")
    ap.add_argument("--delay", type=float, default=1.5, help="요청 간격(초)")
    args = ap.parse_args()

    ids = read_ids(args.id_file)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / "crawl_summary.csv"
    summary = load_summary(summary_path)
    session = requests.Session()

    included = sum(1 for r in summary.values() if r["status"] == "포함")
    print(f"블로거 ID {len(ids)}개 · 목표 {args.target}명 · 이미 포함된 블로거 {included}명")

    for i, blog_id in enumerate(ids, 1):
        if included >= args.target:
            break
        prev = summary.get(blog_id)
        if prev and prev["status"] in ("포함", "제외"):              # 이전 실행에서 끝난 블로거
            continue

        print(f"\n[{i}/{len(ids)}] {blog_id}  (포함 {included}/{args.target})")
        row = {"blog_id": blog_id, "status": "실패", "posts_in_period": 0, "sampled": 0, "saved": 0, "note": ""}
        try:
            posts = list_posts_in_period(blog_id, session, args.delay)
            # 블로거마다 seed를 따로 만들어서, 순서가 바뀌거나 중간에 다시 실행해도 같은 글이 뽑히게 함
            rng = random.Random(f"{args.seed}-{blog_id}")
            sampled = sample_by_month(posts, args.per_month, rng)
            n_sampled = sum(len(v) for v in sampled.values())
            row.update(posts_in_period=len(posts), sampled=n_sampled,
                       **{m: len(v) for m, v in sampled.items()})
            print(f"  기간 내 글 {len(posts)}개 → 달별 추출 {n_sampled}개 "
                  f"({' '.join(str(len(sampled[m])) for m in MONTHS)})")

            if n_sampled < args.min_posts:
                row.update(status="제외", note=f"추출한 글이 {n_sampled}개 (기준 {args.min_posts}개 미만)")
                print(f"  → 제외: {row['note']}")
            else:
                saved, failed = [], 0
                for m in MONTHS:
                    for p in sampled[m]:
                        try:
                            post = fetch_post(blog_id, p["log_no"], session)
                            post["title"] = post["title"] or p["title"]
                            post["published_at"] = post["published_at"] or p["list_date"]
                            post["sample_month"] = m
                            saved.append(post)
                        except Exception as e:
                            failed += 1
                            print(f"    {p['log_no']} 실패: {e}")
                        time.sleep(args.delay)

                with (out / f"{blog_id}.jsonl").open("w", encoding="utf-8") as f:
                    for post in saved:
                        f.write(json.dumps(post, ensure_ascii=False) + "\n")

                row.update(saved=len(saved))
                if len(saved) >= args.min_posts:
                    row["status"] = "포함"
                    included += 1
                    print(f"  → 포함: {len(saved)}개 저장" + (f" (실패 {failed}개)" if failed else ""))
                else:
                    row.update(status="제외", note=f"본문 저장 {len(saved)}개 (실패 {failed}개)로 기준 미달")
                    (out / f"{blog_id}.jsonl").unlink(missing_ok=True)
                    print(f"  → 제외: {row['note']}")
        except Exception as e:
            row["note"] = f"{type(e).__name__}: {e}"
            print(f"  → 실패: {row['note']}")

        summary[blog_id] = row
        save_summary(summary_path, summary)                       # 블로거 하나 끝날 때마다 저장

    n_ex = sum(1 for r in summary.values() if r["status"] == "제외")
    n_fail = sum(1 for r in summary.values() if r["status"] == "실패")
    print(f"\n완료: 포함 {included}명, 제외 {n_ex}명, 실패 {n_fail}명")
    if included < args.target:
        print(f"※ 목표 {args.target}명에 {args.target - included}명 부족합니다. 블로거 ID를 더 추가해 주세요.")
    print(f"결과 폴더: {out}\n요약: {summary_path}")


if __name__ == "__main__":
    main()
