# 리드AI 콘텐츠 검수 웹

초등 1~6학년 콘텐츠의 어휘·지문·문항·정답을 검사하는 **로컬 웹 테스트 버전**입니다. OpenAI API를 서버에서 호출하며, 이용자가 Codex를 설치할 필요가 없는 웹 구조로 전환 중입니다.

현재는 GitHub 코드 공유와 로컬 시험까지 제공합니다. GitHub Pages 게시나 인터넷 서버 배포는 하지 않습니다.

## 빠른 시작

Python 3.12 이상을 권장합니다.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-web.txt
cp .env.example .env
# 편집기에서 .env에 새 API 키를 입력합니다.
python web_server.py
```

브라우저에서 **http://127.0.0.1:8770/** 을 엽니다. Windows에서는 가상환경 활성화 명령이 `.venv\Scripts\activate`입니다. 이번 검증은 macOS에서 수행했습니다.

- `OPENAI_API_KEY`: 서버에서만 읽는 API 키. GitHub·브라우저 코드·보고서에 넣지 않습니다.
- `OPENAI_MODEL`: 기본 `gpt-5.4`. 계정에서 해당 모델 사용 권한이 있어야 합니다.
- `READAI_ACCESS_PASSWORD`: 선택적인 공유 작업공간 접속 비밀번호. 설정하면 12자 이상이며 로그인 화면이 나타납니다.
- 키 없이도 **빠른 형식 검사**를 사용할 수 있습니다. AI 검토는 키를 설정하고 서버를 다시 시작한 뒤 사용합니다.
- 대화나 공개 장소에 올린 키는 교체하고 새 키를 환경변수에 등록하세요. `.env`는 Git에서 제외됩니다.

OpenAI API 상태의 “설정됨”은 키가 서버에 있다는 뜻입니다. 실제 인증·모델 권한·사용 한도는 실행 시 확인합니다. ChatGPT/Codex 사용량과 별개의 API 프로젝트 사용량이 적용됩니다.

## 검수 흐름

1. 시트 URL, 학년과 세트를 선택합니다.
2. 형식 검사 또는 AI 검토를 실행합니다.
3. AI는 세트목록 → 어휘 → 지문원문 → 문항 순서로 읽고, 역순으로 다시 검토합니다.
4. 오류 후보와 확인 제안을 구분해 원문·위치·근거·수정안을 표시합니다.
5. Markdown/JSON 결과를 다운로드합니다. JSON에는 두 단계의 전체 결과·인용·재판정·토큰 사용량을 보존합니다.

동시 검수는 한 건으로 제한합니다. 중단 시 완료된 부분은 보존하며, 서버 재시작 후 이전 실행이 완료된 것처럼 표시하지 않습니다. 중단 시 연결을 취소하지만 이미 서비스에 전달된 요청의 과금 취소를 보장하지 않습니다. 자동 유료 재시도는 하지 않습니다. 인증·권한·한도 오류(401·403·429)가 발생하면 나머지 세트의 API 호출을 멈추고 전체 형식 검사 결과를 보존합니다.

세트배분과 교과서 대응, 성취기준 코드 정확성, 외부 교과서 비교는 제외합니다. 없는 학년을 만들어 채우지 않으며 정상 반복을 오류로 보지 않습니다. AI는 담당자의 최종 확인을 돕는 도구입니다.

## 시트 형식과 보존

현재 지원하는 탭 ID와 이름은 다음과 같습니다.

| 탭 ID | 이름 |
|---|---|
| 2 | 2_세트목록 |
| 3 | 3_어휘 |
| 5 | 5_문항 |
| 6 | 6_지문원문 |

CSV로 읽을 수 있는 기존 리드AI 시트 형식을 사용합니다. 임의의 탭 ID와 열 이름을 자동 변환하거나 비공개 Google 계정 로그인을 대행하지 않습니다. 공유 설정 변경, 원본 쓰기, 모바일 콘텐츠 자동 반영 기능은 없습니다.

입력 스냅샷과 보고서는 서버의 `.local/web/`에 저장됩니다. GitHub에는 개인 보고서·스냅샷·로그인·API 키가 포함되지 않습니다. 기존 Mac 프로그램이 있는 경우 그 프로그램의 로컬 저장 자료를 선택할 수 있지만, 다른 컴퓨터로 자동 전송되지 않습니다.

## 현재 웹 버전의 범위

- FastAPI/Uvicorn 기반이며 **127.0.0.1**에서만 실행합니다. 외부 주소를 허용하는 설정은 아직 제공하지 않습니다.
- 접속 비밀번호는 한 팀의 공유 작업공간을 위한 기능입니다. 개인별 계정·권한·보고서 소유권 분리는 아직 없습니다.
- 한 서버 프로세스·한 실행 작업 기준입니다. 여러 서버나 여러 worker로 띄우지 않습니다.
- 공개 운영 전에는 HTTPS, 개인별 인증·권한, 비공개 시트 연결, 작업 큐·영구 저장, 비용 한도와 운영 모니터링이 필요합니다.
- GitHub는 코드를 공유하는 곳이며, [GitHub Pages는 정적 사이트 호스팅](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)이므로 이 Python 검수 서버를 직접 실행하지 못합니다.

기존 Codex CLI 방식은 `python3 app.py`로 별도 실행할 수 있습니다(기본 8769 포트). 웹 버전은 서버의 OpenAI API를 사용하며 둘의 보고서 폴더와 과금 경로가 다릅니다.

## 테스트

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s tests_web -v
node --check web/app.js
node --check web/login.js
```

자동 테스트는 합성 자료와 모의 AI를 사용하며 API 비용이나 시트 변경을 발생시키지 않습니다. `tests/ui_reliability.cjs`는 Playwright가 별도로 설치된 개발 환경에서 사용하는 브라우저 회귀 시험입니다.

실제 점검 결과와 API 403 제한은 [웹 검증 기록](WEB_VALIDATION.md)에 정리했습니다.

`ci/check.yml.example`은 GitHub Actions 설정 예제입니다. 현재 저장소에서는 자동 실행되지 않습니다. 저장소 관리자가 워크플로 등록 권한을 가진 환경에서 `.github/workflows/check.yml`로 추가할 수 있습니다.

## 구현 참고

- [FastAPI 수명 주기](https://fastapi.tiangolo.com/advanced/events/)
- [OpenAI 구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI API 인증](https://developers.openai.com/api/reference/overview)

키는 서버에만 두고, 구조화 출력 뒤에도 실제 셀·인용·역순 재검토 기록을 검사합니다. 요청 원문이나 공급자의 오류 본문을 로그로 출력하지 않습니다.
