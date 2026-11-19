# moa — front

대화형 블로그 글쓰기 화면입니다. 모든 데이터는 `server/` API(기본 `http://localhost:8000`)에 저장됩니다.

## 로컬 실행

1. API 서버를 먼저 켭니다 (저장소 루트에서). API 키 없이 화면만 보려면 데모 모드:

   ```sh
   DEMO_MODE=1 uvicorn server.app:app --port 8000
   ```

2. 이 폴더에서 화면을 띄웁니다 (Node 또는 Python 중 하나):

   ```sh
   npm run dev                       # Node
   python3 -m http.server 3000       # Python
   ```

3. 브라우저에서 http://localhost:3000 을 엽니다. API 주소를 바꾸려면 `index.html`에서
   `app.js`보다 먼저 `<script>window.MOA_API = 'http://...'</script>`를 넣으세요.

## 화면

- **로그인/회원가입**: 가입하면 블로그 최신 글 10개로 문체 프로필을 만듭니다. 오른쪽 위 `RE`로 다시 만들 수 있어요.
- **왼쪽 글**: 답변할 때마다 내 문체로 갱신됩니다. 직접 고친 문단은 🔒 표시되고 AI가 다시 고치지 않아요.
- **오른쪽 대화**: RAG 꼬리질문. `이 질문은 넘어갈게요`, `질문 그만하고 글 완성하기`.
- **작성 후**: 편집기 아래 `들어가야 할 내용 %`, `차별성 있는 내용 %`와 `더 쓰러 가기`.
- **메모**: `✎ 메모`에 경험을 적어 두고 `바로 작성하러 가기`로 시작.

`npm test`는 `article.js`(글 병합) 테스트를 실행합니다.
