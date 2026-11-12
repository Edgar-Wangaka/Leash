# Turn on Render auto-deploy (connect the GitHub app)

**Why:** Render already has the repo (`github.com/Edgar-Wangaka/Leash`) and
Auto-Deploy is set to "On Commit", but no push has ever started a deploy. The
service was created from the public repo URL without the Render GitHub App,
so GitHub never tells Render about new commits. Every deploy so far was
started by hand. These steps take about five minutes.

## 1. Open the service's repository settings

1. Go to https://dashboard.render.com and sign in.
2. Open the **leash** web service.
3. Click **Settings**, then scroll to **Build & Deploy**.
4. Find **Repository**. Next to `https://github.com/Edgar-Wangaka/Leash`
   you'll see an **Edit** button, or a note that the repo isn't connected
   through GitHub.

## 2. Connect GitHub and install the Render app

1. Click **Edit** next to the repository, then **Connect GitHub** (or
   **Configure account** if GitHub is already listed).
2. GitHub opens "Install Render".
   - Choose your account, **Edgar-Wangaka**.
   - Under **Repository access**, pick **Only select repositories**, then
     choose **Leash**. Render needs no access to your other repos.
   - Click **Install** (or **Save**) and confirm with your GitHub password or
     2FA if asked.
3. Back in Render, select **Edgar-Wangaka/Leash** from the list and save.

## 3. Check the branch and auto-deploy

Still under **Settings → Build & Deploy**:
- **Branch:** `sponsor-integrations` for now. After the pull request is merged,
  change it to `main`.
- **Auto-Deploy:** **On Commit**.

Leave the other settings as they are:
- Build command `pip install ".[remote,postgres]"`
- Start command `python -m leash.remote`
- Health check `/healthz`

## 4. Prove it works

Make an empty commit and push it from the project folder:

```bash
git commit --allow-empty -m "Check Render auto-deploy"
```

```bash
git push
```

Within about 30 seconds, Render's **Events** tab shows a new deploy whose
trigger is the commit message, not "Manual Deploy" or "API". When it says
**Live**, https://www.agentleash.me is on the new commit.

## If it doesn't work

- **No deploy appears:** in GitHub, open Settings → Applications →
  Installed GitHub Apps → Render → Configure, and check that **Leash** is in
  the selected repositories.
- **Render says the repository is inaccessible:** repeat step 2. The app was
  probably installed on a different account or organization.
- **A deploy starts but fails:** open the deploy's logs. The environment
  variables are unchanged by this, so the usual cause is a code error, which
  `pytest` would show too.
- **You'd rather not connect it:** nothing breaks. Deploys keep working with
  **Manual Deploy → Deploy latest commit**.
