# EU Quota Dashboard — Cloud Run Job + Firestore + Firebase Hosting

This package migrates the desktop EXE workflow to a shared web dashboard.

## Architecture

- Cloud Run Job: runs `cloud_job.py` once and exits.
- Firestore: stores the latest and historical daily snapshots.
- Firebase Hosting: serves `public/index.html` as a shared URL.
- Cloud Scheduler: triggers the Cloud Run Job daily after manual testing.

No Python or EXE installation is required on dashboard viewers' computers.

## Dashboard behavior in this release

- Shows collection health, successful/attempted counts, failed order numbers, and the official EU update date.
- Compares each selected snapshot with its immediately preceding snapshot.
- Adds newly exhausted, consumption-rate jump, pending-allocation jump (100 t or more), and <=5% remaining summaries.
- Uses tonnes by default, with an optional kg view; Excel keeps both units.
- Keeps Order number and Origins visible while scrolling horizontally.
- Preserves raw negative/over-100% values and labels pending allocation above balance as `초과 대기`.
- Uses Light as the first-visit default and formats collection time in `Europe/Berlin` with CET/CEST.
- Accepts common Korean country searches such as `일본`, `영국`, `한국`, and `터키`.

- Adds a `비교·예측` (Compare · Forecast) view at `#compare`: pick a date range and up to 8
  categories to compare consumption rate, available quota, actual remaining, or pending
  allocation on one chart, plus a donut of consumed volume share. The comparison table below
  always lists every category, not just the ones charted.
- Forecasts use ordinary linear regression on the current quarter only (same as Excel
  `FORECAST.LINEAR`), shown after 5 recorded days, and are labelled reference-only.
- Quarter rollover: daily-change cards never compare across quarters. The scraper stores
  TARIC `Amount` (initial + carry-over) as `amount_kg/t` and `Transferred Amount` as
  `transferred_kg/t`; remaining/consumption rates use `amount` as the denominator.
  `quarterly_kg/t` still means the initial quota. Older snapshots without these fields fall
  back to `quarterly_kg` and show `-` for carry-over.
- **Cost stays flat as history accumulates.** The status view only ever loads the latest
  snapshot plus the previous one (for the daily-change cards); the date-picker dropdown loads
  just an id/quarter-label list, not full snapshots. The Compare view reads the tiny
  `quota_category_summary` collection (a few KB/day) for only the date range actually
  selected, instead of every accumulated snapshot. A specific past date or an order's
  multi-quarter history dialog fetches its (larger) full snapshot data lazily, only when
  opened. See `loadCloudData`, `selectHistoryDate`, `ensureFullHistoryLoaded`, and
  `fetchCompareRange` in `public/index.html`.

## Firestore collections created automatically

- `quota_snapshots/{YYYY-MM-DD}`: one full snapshot per Berlin day (~70KB; used for the status
  table, Excel export, and order history).
- `quota_category_summary/{YYYY-MM-DD}`: per-category rollup of the same day (~2KB; used by the
  Compare view so browsing years of history stays cheap). Backfilled once from existing
  `quota_snapshots` on first run after this collection was introduced.
- `quota_meta/current`: summary for the latest successful snapshot.
- `quota_meta/seed_history`: prevents duplicate seed-history imports.
- `quota_meta/category_summary_backfill`: prevents re-running the backfill above.

The eight historical JSON files supplied with the desktop package are imported
automatically on the first successful Cloud Run execution.

## Safety behavior

The existing 90% success threshold is preserved. If fewer than 90% of the
tracked order numbers succeed, the job exits with status 1 and does not write
the new snapshot to Firestore.

## Fixed deployment values

- Project: `eu-quota-dashboard`
- Region: `europe-west3` (Frankfurt)
- Cloud Run Job: `eu-quota-daily-update`
- Artifact Registry: `eu-quota`
- Runtime service account: `eu-quota-job`

## Deploy from Google Cloud Shell

This procedure needs no local Docker or Google Cloud CLI installation.

1. Open Google Cloud Console and select `eu-quota-dashboard`.
2. Open Cloud Shell and upload this ZIP.
3. Extract it and enter the `EU_Quota_Cloud` directory.
4. Run each command block below in order.

```bash
gcloud config set project eu-quota-dashboard

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  cloudscheduler.googleapis.com
```

Create a dedicated runtime identity and grant Firestore access:

```bash
gcloud iam service-accounts create eu-quota-job \
  --display-name="EU Quota Cloud Run Job"

gcloud projects add-iam-policy-binding eu-quota-dashboard \
  --member="serviceAccount:eu-quota-job@eu-quota-dashboard.iam.gserviceaccount.com" \
  --role="roles/datastore.user"
```

Create the Docker image repository and build the image:

```bash
gcloud artifacts repositories create eu-quota \
  --repository-format=docker \
  --location=europe-west3 \
  --description="EU quota dashboard images"

gcloud builds submit \
  --tag=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest
```

Create the Cloud Run Job:

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

Run once and wait for completion:

```bash
gcloud run jobs execute eu-quota-daily-update \
  --region=europe-west3 \
  --wait
```

After success, open Firebase Console > Firestore and confirm that
`quota_snapshots` and `quota_meta` exist.

Deploy read-only Firestore rules and the dashboard:

```bash
npx firebase-tools deploy \
  --only firestore:rules,hosting \
  --project eu-quota-dashboard
```

Open the printed Hosting URL and test the date selector, order-number trend
dialog, filters, and Excel download.

## Add the daily schedule after validation

1. Google Cloud Console > Cloud Run > Jobs > `eu-quota-daily-update`.
2. Open Triggers and add a Cloud Scheduler trigger.
3. Schedule: `0 9 * * *`.
4. Timezone: `Europe/Berlin`.

Do not add the scheduler before the manual execution and dashboard test pass.

## Updating later

After changing Python code:

```bash
gcloud builds submit \
  --tag=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest

gcloud run jobs deploy eu-quota-daily-update \
  --image=europe-west3-docker.pkg.dev/eu-quota-dashboard/eu-quota/job:latest \
  --region=europe-west3 \
  --service-account=eu-quota-job@eu-quota-dashboard.iam.gserviceaccount.com
```

After changing only the dashboard:

```bash
npx firebase-tools deploy --only hosting --project eu-quota-dashboard
```
