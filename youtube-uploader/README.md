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

> **PC가 없고 iPad만 있나요?** 아래 [iPad만으로 쓰기](#ipad만으로-쓰기-google-drive--github-actions)를 보세요. PC 설치 없이 클라우드에서 돌아갑니다.

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

## iPad만으로 쓰기 (Google Drive + GitHub Actions)

PC 없이 이렇게 돌아갑니다.

```
iPad 사진 앱 ──공유──▶ Google Drive "YouTube-업로드" 폴더
                              │  (매시간)
                     GitHub Actions 가 영상을 내려받아 YouTube 에 업로드
                              │
          성공 → "YouTube-완료" 폴더 (+ 영상 주소가 담긴 *.result.json)
          실패 → "YouTube-실패" 폴더 (+ 사유가 담긴 *.error.json)
```

설정은 처음 한 번만 하면 되고, 모두 iPad의 Safari와 앱에서 할 수 있습니다.
Google Cloud Console은 iPad에서 화면이 깨질 수 있습니다. 그럴 때는 Safari 주소창의 **가가** 버튼 → **데스크탑 웹 사이트 요청**을 켜세요.

### 1단계. Google Cloud 설정 (약 10분)

1. [console.cloud.google.com](https://console.cloud.google.com/)에서 새 프로젝트를 만듭니다.
2. **API 및 서비스 → 라이브러리**에서 아래 두 API를 각각 **사용 설정**합니다.
   - **YouTube Data API v3**
   - **Google Drive API**
3. **OAuth 동의 화면**(Google 인증 플랫폼)을 설정합니다.
   - 앱 이름과 이메일을 입력하고, 대상(사용자 유형)은 **외부**를 고릅니다.
   - **앱 게시(프로덕션으로 푸시)** 를 누릅니다. ⚠️ **이 단계가 가장 중요합니다.** "테스트" 상태로 두면 7일마다 토큰이 만료되어 자동 업로드가 멈춥니다. 본인만 쓰는 앱이라 Google 검토를 받지 않아도 됩니다.
4. **사용자 인증 정보(클라이언트) → 만들기 → OAuth 클라이언트 ID**를 누릅니다.
   - 애플리케이션 유형: **웹 애플리케이션**
   - 승인된 리디렉션 URI: `https://developers.google.com/oauthplayground`
   - 만들고 나면 나오는 **클라이언트 ID**와 **클라이언트 보안 비밀번호**를 메모 앱에 잠시 적어 둡니다.

### 2단계. 리프레시 토큰 받기 (약 3분)

리프레시 토큰은 GitHub가 사용자 대신 YouTube와 Drive에 접근할 때 쓰는 열쇠입니다.

1. Safari에서 [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground)를 엽니다.
2. 오른쪽 위 ⚙️(톱니바퀴)를 누르고 **Use your own OAuth credentials**를 체크한 뒤, 1단계에서 받은 ID와 비밀번호를 붙여 넣습니다.
3. 왼쪽 **Step 1**의 맨 아래 입력칸(Input your own scopes)에 아래 한 줄을 그대로 붙여 넣고 **Authorize APIs**를 누릅니다.
   ```
   https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/drive
   ```
4. **영상을 올릴 YouTube 채널의 계정**으로 로그인합니다. 채널이 여러 개면 업로드할 채널을 고르세요.
   "Google에서 확인하지 않은 앱" 화면이 나오면 **고급 → (앱 이름)(으)로 이동**을 누르고 모든 권한을 허용합니다. 본인이 만든 앱이라서 나오는 정상적인 안내입니다.
5. **Step 2**에서 **Exchange authorization code for tokens**를 누르면 나오는 **Refresh token** 값을 복사합니다.

### 3단계. Google Drive 폴더 만들기

Drive 앱에서 폴더 3개를 만듭니다. 이름은 자유롭게 정해도 됩니다.

- `YouTube-업로드`: 올릴 영상을 넣는 곳
- `YouTube-완료`: 업로드가 끝난 영상이 옮겨지는 곳
- `YouTube-실패`: 오류가 난 영상이 옮겨지는 곳

각 폴더의 **⋯ → 링크 복사**로 폴더 링크를 받아 둡니다. 링크만 복사하는 것이라 공유 설정은 바뀌지 않습니다.

### 4단계. GitHub에 비밀값 등록

Safari에서 이 저장소를 열고 **Settings → Secrets and variables → Actions → New repository secret**을 눌러 아래 6개를 하나씩 등록합니다.

| 이름 | 값 |
| --- | --- |
| `YT_CLIENT_ID` | 1단계의 클라이언트 ID |
| `YT_CLIENT_SECRET` | 1단계의 클라이언트 보안 비밀번호 |
| `YT_REFRESH_TOKEN` | 2단계의 Refresh token |
| `DRIVE_INBOX_FOLDER` | `YouTube-업로드` 폴더 링크 |
| `DRIVE_UPLOADED_FOLDER` | `YouTube-완료` 폴더 링크 |
| `DRIVE_FAILED_FOLDER` | `YouTube-실패` 폴더 링크 |

등록하고 나면 메모 앱에 적어 둔 값은 지우세요. Secrets에 넣은 값은 로그에 `***`로 가려집니다.

### 5단계. 시작하기

1. 이 기능이 담긴 PR을 `main`에 병합합니다. 예약 실행은 기본 브랜치에서만 동작합니다.
2. 처음 한 번은 바로 시험해 봅니다. 저장소의 **Actions → YouTube 자동 업로드 → Run workflow**를 누르세요. GitHub 앱에서도 실행할 수 있습니다.
3. 그다음부터는 **매시간 자동으로** 실행됩니다.

### 매일 쓰는 법

1. **사진 앱**에서 영상을 고르고 **공유 → Drive → `YouTube-업로드` 폴더**로 올립니다.
2. 제목·설명·예약 시간을 정하려면 같은 폴더에 **Google 스프레드시트**를 하나 만듭니다(이름 자유). 한 번 만들어 두고 계속 쓰면 됩니다. 첫 줄은 아래처럼 씁니다.

   | 파일 | 제목 | 설명 | 태그 | 공개 | 예약 | 재생목록 |
   | --- | --- | --- | --- | --- | --- | --- |
   | IMG_1234.MOV | 제주 여행 브이로그 | 3박 4일 제주 여행 | 여행, 제주, 브이로그 | 공개 | 2026-11-10 18:00 | |
   | IMG_1240 | 맛집 리뷰 | | 맛집 | 일부공개 | | PLxxxx |

   - **파일:** Drive에 올린 영상 파일 이름입니다. 확장자는 생략해도 됩니다.
   - **공개:** `공개` / `일부공개` / `비공개` 중 하나를 씁니다.
   - **예약:** 한국 시간으로 적습니다. 예약을 지정하면 그 시간까지 비공개로 있다가 자동 공개됩니다.
   - 열은 필요한 것만 있어도 됩니다. 이 밖에 `카테고리`, `아동용`(예/아니오), `썸네일` 열도 쓸 수 있습니다.
   - 시트에 적지 않은 영상은 파일 이름이 제목이 됩니다. Drive 앱에서 영상 이름을 바꿔 두는 방법도 있습니다.
3. **썸네일:** 영상과 같은 이름의 사진(`IMG_1234.jpg`)을 같은 폴더에 넣으면 됩니다.
4. **결과 확인:** `YouTube-완료` 폴더의 `*.result.json`에 영상 주소가 있습니다. 실패하면 `YouTube-실패` 폴더의 `*.error.json`에서 이유를 볼 수 있습니다. 고친 뒤 영상을 `YouTube-업로드`로 다시 옮기면 재시도합니다.

### 알아 둘 점

- **업로드 개수:** 한 번 실행할 때 최대 3개씩 올립니다(하루 할당량이 약 6개라서). **Run workflow**로 직접 실행할 때는 개수를 바꿀 수 있고, 저장소 Variables에 `MAX_VIDEOS_PER_RUN`을 등록하면 기본값을 바꿀 수 있습니다.
- **올라가는 시점:** 영상은 Drive 업로드가 끝난 뒤, 다음 정시 실행(매시 23분 무렵) 때 올라갑니다. GitHub 사정으로 몇 분에서 수십 분 늦어질 수 있습니다.
- **로그 공개:** 이 저장소는 **공개 저장소**라서 Actions 로그(영상 파일 이름과 제목)를 누구나 볼 수 있습니다. 비밀값은 가려집니다. 로그를 숨기고 싶으면 저장소를 비공개로 바꾸세요. 비공개 저장소는 Actions 무료 사용 시간이 월 2,000분인데, 매시간 실행해도 그 안에 충분히 들어갑니다.
- **장기 미사용:** 공개 저장소에 60일 동안 커밋이 없으면 GitHub가 예약 실행을 멈춥니다. 그때는 Actions 탭에서 **Enable workflow**를 누르면 됩니다.
- **토큰 오류:** "리프레시 토큰으로 로그인하지 못했습니다"라는 오류가 나면 2단계를 다시 해서 `YT_REFRESH_TOKEN`을 새 값으로 바꾸세요. Google 계정 비밀번호를 바꾸면 토큰이 무효가 됩니다.
- 위 [꼭 알아야 할 제한](#4-꼭-알아야-할-제한)(API 감사 전에는 비공개로 고정 등)은 이 방식에도 똑같이 적용됩니다.

## 테스트

```bash
pip install pytest
python -m pytest -q
```
