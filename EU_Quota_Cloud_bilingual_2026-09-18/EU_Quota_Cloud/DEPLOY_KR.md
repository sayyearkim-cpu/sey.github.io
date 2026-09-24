# EU Quota Dashboard 클라우드 배포 가이드

이 폴더는 아래 구조로 작동합니다.

- **Cloud Run Job**: EU TARIC 사이트를 조회하는 Python 프로그램
- **Firestore**: 최신 데이터와 날짜별 과거 데이터를 저장하는 데이터베이스
- **Firebase Hosting**: 본사에 전달할 대시보드 링크
- **Cloud Scheduler**: 매일 오전 9시에 Cloud Run Job을 실행하는 예약 장치

본사 사용자는 링크만 열면 됩니다. Python과 EXE를 설치하지 않습니다.

## 이미 배포한 환경에서 이번 수정본으로 교체하는 방법

Cloud Shell에 새 ZIP을 업로드한 뒤 아래 명령을 **한 줄씩** 실행합니다. 기존 Scheduler와 Firestore 과거 데이터는 삭제하거나 다시 만들 필요가 없습니다.

```bash
cd ~
unzip -o EU_Quota_Cloud_Ready_2026-09-10.zip
cd EU_Quota_Cloud
gcloud builds submit --tag=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest
gcloud run jobs deploy eu-quota-daily-update --image=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest --region=europe-west3 --service-account=eu-quota-job@eu-quota-dashboard.iam.gserviceaccount.com --tasks=1 --max-retries=1 --task-timeout=20m --memory=512Mi
gcloud run jobs execute eu-quota-daily-update --region=europe-west3 --wait
npx firebase-tools deploy --only hosting --project eu-quota-dashboard
```

마지막 명령까지 성공한 후 `https://eu-quota-dashboard.web.app`을 새로고침합니다. 브라우저가 이전 화면을 보이면 `Ctrl+F5`를 누릅니다.

## 배포 전 확인

- Firebase 프로젝트 ID: `eu-quota-dashboard`
- Firestore 데이터베이스: `(default)`
- 권장 리전: `europe-west3` (독일 프랑크푸르트)

`quota_snapshots`, `quota_meta` 컬렉션은 직접 만들지 않습니다. 첫 번째 수집이 성공하면 프로그램이 자동 생성하며, 기존 PC 프로그램의 8일치 과거 기록도 함께 넣습니다.

## 1. Cloud Shell 열기

1. [Google Cloud Console](https://console.cloud.google.com/)에 접속합니다.
2. 화면 상단 프로젝트가 `eu-quota-dashboard`인지 확인합니다.
3. 우측 상단의 터미널 모양 **Cloud Shell 활성화** 버튼을 누릅니다.
4. 이 압축 파일을 Cloud Shell에 업로드하고 압축을 풉니다.
5. 터미널에서 `EU_Quota_Cloud` 폴더로 이동합니다.

## 2. 필요한 Google API 켜기

아래 명령을 한 덩어리씩 복사하여 Cloud Shell에 붙여넣습니다.

```bash
gcloud config set project eu-quota-dashboard

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  cloudscheduler.googleapis.com
```

## 3. 전용 서비스 계정 만들기

```bash
gcloud iam service-accounts create eu-quota-job \
  --display-name="EU Quota Cloud Run Job"

gcloud projects add-iam-policy-binding eu-quota-dashboard \
  --member="serviceAccount:eu-quota-job@eu-quota-dashboard.iam.gserviceaccount.com" \
  --role="roles/datastore.user"
```

이미 만들었다는 오류가 나오면 서비스 계정 생성 명령만 건너뛰고 다음 명령으로 진행합니다.

## 4. Python 프로그램을 컨테이너로 빌드

```bash
gcloud artifacts repositories create eu-quota \
  --repository-format=docker \
  --location=europe-west3 \
  --description="EU quota dashboard images"

gcloud builds submit \
  --tag=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest
```

`eu-quota` 저장소가 이미 존재한다는 오류가 나오면 저장소 생성은 건너뛰고 `gcloud builds submit`부터 실행합니다.

## 5. Cloud Run Job 생성

```bash
gcloud run jobs deploy eu-quota-daily-update \
  --image=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest \
  --region=europe-west3 \
  --service-account=eu-quota-job@eu-quota-dashboard.iam.gserviceaccount.com \
  --tasks=1 \
  --max-retries=1 \
  --task-timeout=20m \
  --memory=512Mi
```

## 6. 수동으로 한 번 실행

```bash
gcloud run jobs execute eu-quota-daily-update \
  --region=europe-west3 \
  --wait
```

명령 마지막에 성공이 표시되면 Firebase Console의 Firestore에서 다음 항목을 확인합니다.

- `quota_snapshots`: 기존 8일치 + 오늘 데이터
- `quota_meta`: 현재 데이터 요약과 과거 자료 이전 완료 표시

성공률이 90%보다 낮으면 안전장치가 작동하여 오늘 데이터를 저장하지 않고 실행이 실패로 끝납니다. 이 경우 기존 정상 데이터는 유지됩니다.

## 7. 대시보드 링크 배포

```bash
npx firebase-tools deploy \
  --only firestore:rules,hosting \
  --project eu-quota-dashboard
```

처음 실행하면서 로그인을 요구하면 화면에 표시되는 안내 링크로 Google 계정 인증을 완료합니다. 배포가 끝나면 다음과 비슷한 주소가 출력됩니다.

`https://eu-quota-dashboard.web.app`

이 주소를 본사 사용자에게 전달하면 됩니다.

## 8. 매일 오전 9시 자동 실행 설정

먼저 6~7단계의 수동 테스트가 성공한 뒤 설정합니다.

1. Google Cloud Console에서 **Cloud Run → Jobs**로 이동합니다.
2. `eu-quota-daily-update`를 선택합니다.
3. **Triggers/트리거 → Add Scheduler Trigger**를 선택합니다.
4. 주기에는 `0 9 * * *`를 입력합니다.
5. 시간대는 `Europe/Berlin`을 선택합니다.
6. 저장합니다.

## 데이터 공개 범위

현재 설정은 본사에서 별도 로그인 없이 링크를 열 수 있도록 쿼터 데이터에 공개 읽기 권한을 줍니다. 누구든 링크를 알면 화면과 데이터를 볼 수 있습니다. 사내 계정만 접속하도록 제한하려면 이후 Firebase Authentication을 추가해야 합니다.

## 이후 프로그램을 수정할 때

Python 수집 코드를 수정한 경우:

```bash
gcloud builds submit \
  --tag=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest

gcloud run jobs deploy eu-quota-daily-update \
  --image=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest \
  --region=europe-west3 \
  --service-account=eu-quota-job@eu-quota-dashboard.iam.gserviceaccount.com
```

대시보드 HTML만 수정한 경우:

```bash
npx firebase-tools deploy --only hosting --project eu-quota-dashboard
```
