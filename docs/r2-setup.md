# Cloudflare R2 setup (domain on Squarespace)

**Why DNS has to move:** Cloudflare only lets an R2 bucket use your own domain
(like `data.yourdomain.com`) when Cloudflare runs that domain's DNS. So you keep
the domain registered at Squarespace and point its nameservers at Cloudflare.
Your Squarespace website and email keep working as long as their DNS records are
copied over, and Part A walks through that check.

Can't change DNS yet? Skip Part A, do Parts B–E, and use the bucket's temporary
`r2.dev` URL in step C3. You can switch to the custom domain later by changing one
GitHub variable.

Replace `yourdomain.com` everywhere below with your real domain.

---

## Part A: Move DNS to Cloudflare (keep the domain at Squarespace)

**A1. Write down your current records.** In Squarespace go to **Settings → Domains →
yourdomain.com → DNS** (sometimes labeled **DNS Settings**). Screenshot every record,
especially:
- the **Squarespace Defaults** (A records and the `www` CNAME for your website),
- **MX** and **TXT** records if you have email on this domain (Google Workspace, etc.),
- any other custom records.

**A2. Create a Cloudflare account** at https://dash.cloudflare.com/sign-up.

**A3. Add your domain.** On the Cloudflare dashboard home, choose **Add a domain** (it
may say "Onboard a domain"). Enter `yourdomain.com`, leave **Quick scan for DNS records**
selected, and continue. Pick the **Free** plan.

**A4. Check the imported records against your screenshots.** Cloudflare lists what it
found. Add anything missing so the list matches Squarespace exactly. Then:
- Set the website records (the Squarespace A records and the `www` CNAME) to
  **DNS only** (grey cloud), not Proxied. Squarespace issues its own SSL certificate,
  and proxying can break it.
- Leave MX and TXT records as they are. They're always DNS only.

**A5. Copy the two Cloudflare nameservers** Cloudflare shows next, e.g.
`ada.ns.cloudflare.com` and `bob.ns.cloudflare.com`. Yours will have different names.

**A6. Turn off DNSSEC at Squarespace** if it's on: **Domains → yourdomain.com → DNS →
DNSSEC**, and remove or disable it. Switching nameservers while DNSSEC is on can take your
domain offline.

**A7. Switch nameservers at Squarespace:** **Domains → yourdomain.com → DNS → Domain
nameservers → Use custom nameservers**. Delete the existing ones, enter the two Cloudflare
nameservers, and save.

**A8. Wait for activation.** Back in Cloudflare, click **Check nameservers**. It usually
goes active within an hour but can take up to 24–48 hours, and Cloudflare emails you when
it's done. Then check that your website still loads (and that email still arrives, if you
have it on this domain).

---

## Part B: Create the bucket

**B1.** In the Cloudflare sidebar open **R2 Object Storage** and enable R2. It asks for a
payment method even though usage within the free tier (10 GB stored, unlimited downloads)
costs nothing.

**B2.** Click **Create bucket**:
- Name: `isthisnormal-data`
- Location: **Automatic**
- Default storage class: **Standard**

---

## Part C: Make the bucket publicly readable

**C1. (Needs Part A finished.)** Open the bucket → **Settings → Custom Domains → Add**.
Enter `data.yourdomain.com` and confirm. Cloudflare creates the DNS record itself. Wait
until the status shows **Active**.

**C2.** Same Settings page → **CORS Policy → Add CORS policy**, and paste:
```json
[
  {
    "AllowedOrigins": ["https://mendus59.github.io", "http://localhost:4321"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "MaxAgeSeconds": 3600
  }
]
```
This allows the GitHub Pages site (and local testing) to read the data. If you later put
the site itself on your domain, add that address to `AllowedOrigins`.

**C3. Your public data URL** is `https://data.yourdomain.com`.
Without a custom domain: **Settings → Public Development URL (r2.dev) → Allow** gives a
`https://pub-….r2.dev` URL. It's rate-limited, so use it only to get started.

---

## Part D: Create the access key the pipeline uses

**D1.** Go to **R2 Object Storage** overview → **Manage API tokens** (may read **API Tokens**)
→ **Create API token** (pick the Account API token option if asked).

**D2.** Settings:
- Permissions: **Object Read & Write**
- Specify bucket: **Apply to specific buckets only → `isthisnormal-data`**
- TTL: **Forever**

**D3.** Click Create, and copy **Access Key ID** and **Secret Access Key**. The secret is shown
only once. Also copy your **Account ID**, shown on the R2 overview page under Account Details.

Don't paste these into the chat. They go straight into GitHub in Part E.

---

## Part E: Add them to GitHub

In https://github.com/mendus59/is_this_normal/settings/secrets/actions:

**Secrets tab → New repository secret**, one at a time:

| Name | Value |
|---|---|
| `R2_ACCOUNT_ID` | Account ID from D3 |
| `R2_ACCESS_KEY_ID` | Access Key ID from D3 |
| `R2_SECRET_ACCESS_KEY` | Secret Access Key from D3 |
| `R2_BUCKET` | `isthisnormal-data` |

**Variables tab → New repository variable**:

| Name | Value |
|---|---|
| `PUBLIC_DATA_BASE_URL` | `https://data.yourdomain.com` (or the r2.dev URL), no trailing slash |

---

## Check it worked

After the PR is merged, run **Actions → Ingest**, then **Actions → Publish data**.
Then open `https://data.yourdomain.com/manifest.json` in a browser. If it shows a
small JSON file with a version and an artifact count, R2 is set up correctly.
