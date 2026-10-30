# Setup

One-time steps to connect the accounts this project uses. Everything here is
free to start except the Anthropic API key, which is only spent when you run
the **Analyze** workflow yourself.

Do these in order. Each step says exactly what to copy into GitHub.

## 1. GitHub Pages

1. Repo **Settings → Pages → Build and deployment → Source**: choose **GitHub Actions**.
2. After this PR merges, the **Deploy site** workflow publishes the site to
   `https://mendus59.github.io/is_this_normal/`.

## 2. Cloudflare R2 (documents and published data)

Full step-by-step instructions, including moving DNS from Squarespace to
Cloudflare so the bucket can use `data.yourdomain.com`, are in
[docs/r2-setup.md](docs/r2-setup.md). In short: move the domain's nameservers to
Cloudflare (registration stays at Squarespace), create the `isthisnormal-data`
bucket, connect the custom domain, add the CORS policy, and create an Object
Read & Write API token scoped to that bucket.

## 3. Neon Postgres (citation graph and analyses)

1. Sign up at <https://neon.tech> and create a project named `isthisnormal`
   (Postgres 16 or newer, region closest to you).
2. **Connection Details**: copy the connection string (pooled is fine). It looks like
   `postgresql://user:password@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require`.
3. Nothing else to do. The first pipeline run creates the tables and enables `pgvector`.

## 4. Free data API keys

- **api.data.gov** (Congress.gov and GovInfo): sign up at <https://api.data.gov/signup/>; the key arrives by email.
- **CourtListener**: create an account at <https://www.courtlistener.com/sign-in/>, then copy your token from
  **Profile → Developer tools → API token**.
- Federal Register needs no key.

## 5. Anthropic API key (the only paid piece)

1. At <https://console.anthropic.com>, create a workspace named `isthisnormal`
   and set a **monthly spend limit** on it. This is the backstop behind the per-run budget cap.
2. Create an API key in that workspace.

## 6. Put everything into GitHub

**Settings → Secrets and variables → Actions → Secrets → New repository secret**:

| Secret | Value |
|---|---|
| `DATABASE_URL` | Neon connection string |
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 token access key |
| `R2_SECRET_ACCESS_KEY` | R2 token secret |
| `R2_BUCKET` | `isthisnormal-data` |
| `DATA_GOV_API_KEY` | api.data.gov key |
| `COURTLISTENER_TOKEN` | CourtListener token |

**Same page → Variables tab → New repository variable**:

| Variable | Value |
|---|---|
| `PUBLIC_DATA_BASE_URL` | Public bucket URL from step 2.3, no trailing slash |
| `PIPELINE_ENABLED` | `true` (scheduled jobs skip quietly until this is set) |

**The paid environment.** Settings → **Environments → New environment** named `paid`:

1. Tick **Required reviewers** and add yourself. Every Analyze run now waits for your approval.
2. **Deployment branches and tags → Selected branches** → add `main`.
3. **Environment secrets → Add secret**: `ANTHROPIC_API_KEY` = the key from step 5.

Do **not** add `ANTHROPIC_API_KEY` as a repository secret. Keeping it only in
`paid` is what stops the scheduled jobs from ever spending money.

## 7. First run

1. **Actions → Ingest → Run workflow** (leave "since" blank). The first run creates
   the database tables and loads every presidential document since 2000 from
   the Federal Register. Later runs happen daily on their own.
2. **Actions → Publish data → Run workflow**. Writes `manifest.json` and the catalog to R2.
3. **Actions → Deploy site → Run workflow** (needed once so the site picks up
   `PUBLIC_DATA_BASE_URL`). Open the site; the catalog should list the documents.

## Running locally (optional)

```sh
cd pipeline && python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
export DATABASE_URL=... R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=isthisnormal-data
itn migrate && itn ingest && itn publish
pytest

cd ../web && npm install && PUBLIC_DATA_BASE_URL=https://data.yourdomain.org npm run dev
```
