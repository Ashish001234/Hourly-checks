# Setting up your job dashboard — no coding needed

This gives you a real website that refreshes itself every hour
and stays live even when this chat isn't open, for free, on GitHub.

## One-time setup (about 5 minutes)

1. **Create a GitHub account** if you don't have one: https://github.com/signup

2. **Create a new repository**
   - Go to https://github.com/new
   - Name it something like `grad-job-dashboard`
   - Set it to **Public** (required for free GitHub Pages)
   - Don't check any of the "initialize with" boxes
   - Click **Create repository**

3. **Upload these files**

   The folder `grad-job-dashboard/site/` on your computer is already a
   prepared git repository with everything committed, so you can just
   push it. On the new empty repo's page GitHub shows you its URL —
   use it here:

   ```
   cd grad-job-dashboard/site
   git remote add origin https://github.com/<your-username>/grad-job-dashboard.git
   git push -u origin main
   ```

   Push from inside `site/` — `index.html` has to land at the top level
   of the repo, or GitHub Pages won't find the page.

   *If you'd rather not use the command line:* on your new (empty) repo
   page click **"uploading an existing file"**, then select everything
   inside `site/` (not the folder itself) and drag it onto the upload
   box — easiest in Chrome, which preserves subfolders like
   `.github/workflows/`. Scroll down and click **Commit changes**. If a
   folder didn't come through (GitHub shows a flat file list rather than
   `.github/workflows/refresh.yml` as one path), use **Add file → Create
   new file**, type the full path as the filename, and paste that file's
   contents in.

4. **Turn on GitHub Pages**
   - Go to your repo's **Settings** tab → **Pages** (left sidebar)
   - Under "Build and deployment" → **Source**, choose **GitHub Actions**

5. **Turn on the scheduled workflow**
   - Go to the **Actions** tab → you'll see "Refresh listings and deploy"
   - Click into it once and hit **"I understand my workflows, go ahead
     and enable them"** if prompted
   - Click **Run workflow** once to trigger the first deploy manually

6. **Find your site**
   - After that first run finishes (~1 minute), go back to **Settings → Pages**
   - Your live URL will be shown at the top, something like:
     `https://<your-username>.github.io/grad-job-dashboard/`

That's it. From here on, GitHub's own servers fetch fresh listings and
redeploy the page every hour — nothing on your end has to run.

## Adjusting things later

- **Change the refresh times or frequency**: open
  `.github/workflows/refresh.yml` in GitHub's web editor (pencil icon),
  edit the `cron:` lines (times are in UTC), commit.
- **Trigger a refresh right now**: Actions tab → "Refresh listings and
  deploy" → **Run workflow**.
- **Add companies to the FAANG+/Big Tech tiers**: edit `FAANG` /
  `BIGTECH` in `scripts/fetch_jobs.py`.

## What this pulls from

Four community trackers, merged and de-duplicated: **SimplifyJobs**,
**speedyapply**, **jobright-ai**, and **vanshb03**. Using several is the
point — in Aug 2026 vanshb03 went a week without an update while the others
posted daily, and a single-source board can't tell "the source is stale"
apart from "nobody is hiring".

If one tracker is down or changes format, the refresh keeps going with the
rest. It only refuses to write when fewer than two sources responded, so a
bad run never replaces good data with a short list.

See **README.md** for how merging, dates, and the NEW badge work.

## Staying under 12 hours

- The board refreshes hourly, so a new posting shows up within about an hour.
- **NEW** marks anything first seen since you last hit **Mark all seen** —
  that button is the "I've reviewed everything up to here" marker.
- **New only** plus **Posted today** is the fast daily pass.
- Sponsorship flags come from the trackers. Most roles say nothing either
  way, so **Sponsor-friendly** hides only the ones explicitly closed to
  sponsorship, rather than guessing about the rest.

⚠️ GitHub turns off scheduled workflows after 60 days of no repository
activity on public repos. If refreshes ever stop, open the **Actions** tab
and re-enable the workflow.
