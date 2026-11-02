# YouTube 자동 업로더

`inbox/` 폴더에 영상을 넣으면 YouTube에 자동으로 올려 주는 Python 프로그램입니다.
YouTube Data API v3를 사용하고, 예약 공개, 썸네일, 재생목록 추가를 지원합니다.

```
inbox/                     uploaded/                    failed/
  my-video.mp4   ──업로드──▶  my-video.mp4                 (오류가 난 파일과
  my-video.json  (메타데이터)  my-video.json                 *.error.json 사유)
  my-video.jpg   (썸네일)     my-video.jpg
                             my-video.result.json  ← 영상 ID, URL
```

## 1. 설치

Python 3.9 이상이 필요합니다.

```bash
cd youtube-uploader
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.json config.json
```

## 2. Google Cloud 설정 (최초 1회)

1. [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트를 만듭니다.
2. **API 및 서비스 → 라이브러리**에서 **YouTube Data API v3**를 사용 설정합니다.
3. **OAuth 동의 화면**을 설정합니다. 사용자 유형은 "외부"로 하고, 테스트 사용자에 업로드할 채널의 Google 계정을 추가합니다.
4. **사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**를 누르고 애플리케이션 유형을 **데스크톱 앱**으로 선택합니다.
5. JSON을 다운로드해 이 폴더에 `client_secret.json`이라는 이름으로 저장합니다.
6. 인증을 실행합니다.

   ```bash
   python uploader.py auth
   ```

   브라우저에서 채널 계정으로 로그인하면 `token.json`이 생깁니다. 이후에는 자동으로 갱신됩니다.
   브라우저가 없는 서버라면 `--no-browser`를 붙이고 출력된 URL을 다른 기기에서 여세요.

> `client_secret.json`과 `token.json`은 채널 접근 권한이 담긴 비밀 정보라서 `.gitignore`에 들어 있습니다. 절대 커밋하지 마세요.

## 3. 사용법

| 명령 | 설명 |
| --- | --- |
| `python uploader.py check` | 업로드 없이 `inbox/`의 메타데이터만 검사합니다 |
| `python uploader.py once` | 대기 중인 영상을 한 번 업로드하고 끝냅니다 |
| `python uploader.py watch` | 폴더를 계속 감시하며 업로드합니다 (Ctrl+C로 종료) |

옵션: `-c 설정파일`, `--log-file uploader.log`, `-v`(상세 로그)

### 메타데이터 파일 (선택)

영상과 같은 이름의 `.json` 파일을 함께 넣습니다. 파일이 없으면 `config.json`의 `defaults`를 쓰고, 제목은 파일 이름이 됩니다.
예시는 [`examples/my-video.json`](examples/my-video.json)에 있습니다.

| 키 | 설명 |
| --- | --- |
| `title` | 제목 (100자 이하, `<` `>` 불가) |
| `description` | 설명 (5000바이트 이하) |
| `tags` | 태그 배열 또는 `"a, b, c"` 문자열 (전체 500자 이하) |
| `categoryId` | 카테고리 ID (22 = 인물/블로그, 27 = 교육, 28 = 과학기술, 20 = 게임 등) |
| `privacyStatus` | `private` / `unlisted` / `public` |
| `publishAt` | 예약 공개 시간. 예: `"2026-11-10 18:00"` 또는 `"2026-11-10T18:00:00+09:00"`. 시간대를 생략하면 프로그램을 실행하는 컴퓨터의 현지 시간으로 봅니다. 지정하면 공개 시간 전까지 자동으로 `private`가 됩니다 |
| `thumbnail` | 썸네일 경로. 생략하면 같은 이름의 `.jpg`/`.png`를 찾습니다 |
| `playlistId` | 업로드 후 추가할 재생목록 ID (`PL...`) |
| `madeForKids` | 아동용 콘텐츠 여부 (`true`/`false`) |
| `notifySubscribers` | 구독자에게 알림을 보낼지 여부 (기본 `true`) |

### 동작 방식

- 복사가 끝나지 않은 파일은 건너뜁니다. 영상과 `.json` 파일이 `stable_seconds`(기본 10초) 동안 바뀌지 않아야 업로드합니다.
- 큰 파일은 8MB 단위로 나눠 올리는 재개 가능(resumable) 업로드를 씁니다. 네트워크 오류나 5xx 응답은 최대 8회까지 재시도합니다.
- 업로드에 성공하면 `uploaded/`로 옮기고 `*.result.json`에 영상 URL을 남깁니다.
- 메타데이터 오류나 업로드 실패는 `failed/`로 옮기고 `*.error.json`에 사유를 남깁니다. 고친 뒤 `inbox/`에 다시 넣으면 됩니다.
- **할당량을 초과하면** 파일을 `inbox/`에 그대로 두고, `watch` 모드는 1시간 뒤에 다시 시도합니다.

## 4. 꼭 알아야 할 제한

- **일일 할당량:** 기본 할당량은 하루 10,000 유닛이고 업로드 1회에 약 1,600 유닛이 듭니다. 그래서 **하루에 약 6개**까지 올릴 수 있습니다. 더 필요하면 Google에 할당량 증가를 신청해야 합니다.
- **미인증 앱의 영상은 비공개로 잠김:** Google의 API 감사(audit)를 통과하지 않은 프로젝트로 올린 영상은 공개 설정과 관계없이 **비공개로 고정**됩니다. 공개나 예약 공개를 쓰려면 [YouTube API 감사 신청](https://support.google.com/youtube/contact/yt_api_form)이 필요합니다.
- **테스트 모드 토큰 만료:** OAuth 동의 화면이 "테스트" 상태면 토큰이 7일 후 만료됩니다. 그러면 `python uploader.py auth`를 다시 실행하세요(`token.json`을 지운 뒤). "프로덕션"으로 게시하면 이 문제가 없어집니다.
- **썸네일:** 맞춤 썸네일은 전화번호 인증을 마친 채널만 설정할 수 있습니다. 실패해도 영상 업로드는 성공으로 처리됩니다.

## 5. 자동 실행 (선택)

**macOS / Linux, crontab:** 10분마다 실행합니다.
```cron
*/10 * * * * cd /경로/youtube-uploader && .venv/bin/python uploader.py once --log-file uploader.log
```

**Windows 작업 스케줄러:** 프로그램은 `.venv\Scripts\python.exe`, 인수는 `uploader.py watch --log-file uploader.log`, 시작 위치는 이 폴더로 지정합니다.

## 테스트

```bash
pip install pytest
python -m pytest -q
```
