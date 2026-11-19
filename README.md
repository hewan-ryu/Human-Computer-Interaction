# Human-Computer-Interaction
2026-2 HCI course Team project — 대화형 블로그 포스팅 작성 도우미

## 폴더 구조

```
front/      UI (정적 웹앱, `npm run dev`)
server/     대화 API + RAG 질문 루프 (FastAPI, gpt-5.6-luna + deepseek-v4-flash)
rag/        블로그 DB 벡터 검색 (임베딩, 인덱스 생성, 검색)
prompts/    작성 AI 프롬프트, 질문 루프 Step별 프롬프트
db/         블로그 포스팅 DB (crawler/ 수집·전처리, raw/, processed/)
tests/      서버 테스트 (데모 모드 가짜 LLM, API 키 불필요)
docs/       보고서
```

## 서비스 흐름

목표: AI 양산형 글이 아니라 사용자의 실제 경험이 드러나는 글. 원칙: **가장 경험적인 것이 가장 차별적인 것이다.**

| 기능 | 코드 |
|---|---|
| 회원가입 → 블로그 최신 글 10개로 문체 프로필 (`RE`로 다시 만들기) | `server/style_profile.py`, `prompts/style_profile.md` |
| 개인 DB (사용자, 프로필, 내 글, 메모, 작성 세션) | `server/storage.py` (SQLite, `server/data/app.db`) |
| 메모 → 바로 작성하러 가기 | `prompts/question_loop/opening_from_memo.md` |
| 작성 AI 말투·질문 원칙 (공감 + 꼬리질문 하나, + 버튼 질문 이유) | `prompts/writing_ai.md` |
| 경험이 없다고 하면 주제 추천 버튼 (추천 기능은 아직 없음) | `prompts/question_loop/step1_query.md` |
| RAG 꼬리질문 · 넘어가기 · 더 쓰러 가기 | `server/question_loop.py` |
| 답마다 '글에 반영하기' / '반영 없이 넘어가기' | `server/question_loop.py`, `front/app.js` |
| 실시간 글 (문체 few-shot, 사용자가 고친 문단은 잠금) → 질문이 끝나면 최종 완성 | `server/draft_writer.py`, `prompts/draft.md`, `prompts/draft_final.md` |
| 들어가야 할 내용 % / 차별성 있는 내용 % | `server/metrics.py`, `prompts/metrics.md` |
| 공통 DB (다른 블로거 글) | `db/`, `rag/` |

## RAG 질문 루프

```
사용자 답변 ─▶ [Step 1] 대화 요약 → 검색 쿼리                (LLM)
              [Step 2] 쿼리 임베딩 → 비슷한 글 10개, 이미 쓴 글 제외  (rag/)
              [Step 3] 10개 글 → 경험 측면 지도 aspect_map    (LLM)
              [Step 4] 꼬리질문 (차별점 우선, 다음은 안 다룬 측면) (LLM)
                 └ null이면 새 라운드 (Step 1로)
              [Step 5] 답변이 충분히 구체적인가? → 추가질문 (최대 2회) (LLM)
                 └ 충분하면 Step 4로
종료: 새로 가져올 글 없음 / 3라운드 / 사용자가 그만 (stop=true 또는 "그만" 등)
```

- 코드: `server/question_loop.py`, 상태: `server/state.py`

## LLM 모델 배분 (`server/llm.py`)

| 등급 | 기본 모델 | 호출 |
|---|---|---|
| writer (사용자에게 보이는 문장) | `gpt-5.6-luna` | Step 4 꼬리질문, Step 5 추가질문, 메모 첫 질문, 실시간 글·최종 완성, 문체 프로필 |
| fast (내부 분석) | `deepseek-v4-flash` | Step 1 쿼리·경험 판단, Step 3 측면 추출, 지표 문장 판정 |

### API 키 설정 (팀원용)
**server폴더에 .env파일 만들어서 엔드포인트 url, api key 넣기. (server/.env.example 참고)**
1. `cp server/.env.example server/.env`
2. `server/.env`의 `MINIAPP_LLM_BASE_URL`, `MINIAPP_LLM_API_KEY`를 채워요. 값은 팀에서 안전한 경로로 받아요.
3. API 서버를 다시 켜요.

- `server/.env`는 `.gitignore`에 있어 커밋되지 않아요. 키를 코드, README, 채팅에 적지 마세요.
- VibeHub에 배포한 앱에는 이 두 값이 자동으로 들어가요 (AI Agent 모델 사용 승인 후 재배포 필요).
fast 모델은 JSON 모드로 받고 코드에서 스키마를 검사해, 틀리면 한 번 다시 요청해요.
- 프롬프트: `prompts/writing_ai.md` (말투·형식), `prompts/question_loop/step*.md`

## 실행

```sh
pip install -r server/requirements.txt
cp server/.env.example server/.env        # MINIAPP_LLM_BASE_URL, MINIAPP_LLM_API_KEY 입력 (아래 'API 키 설정')
python -m rag.build_index                 # db/processed/clean.json 필요
uvicorn server.app:app --reload --port 8000
python -m pytest tests                    # 테스트
```

API 키·벡터 DB 없이 화면을 돌려 보려면 `DEMO_MODE=1 uvicorn server.app:app --port 8000` (가짜 LLM). 화면 실행은 `front/README.md`.
API 목록은 `server/app.py` 맨 위 주석에 있습니다.
