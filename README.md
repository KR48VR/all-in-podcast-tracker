# All-In Podcast Tracker

A weekly-updating website that tracks the [All-In Podcast](https://allin.com).
Each Saturday morning (9am Singapore time) it pulls the newest episode,
analyzes it with an open-source AI model, updates trend charts, and publishes
a refreshed brief — plus an AI chatbot you can ask anything about the whole
archive.

## What you'll end up with

- A website at `https://<your-username>.github.io/podcast-tracker/` (or your
  own Cloudflare Pages URL) with:
  - A weekly brief at the top (what's new + how it fits past trends)
  - Trend charts (topics over time, recurring guests, sentiment shifts)
  - A searchable archive of every episode
  - A chatbot that answers questions across all episodes
- A GitHub repo that updates itself every Saturday morning

## One-time setup (about 15 minutes)

You need three free accounts. If you already have any of them, skip ahead.

### 1. GitHub account
- Go to [github.com](https://github.com) and sign up if you don't have an account.
- Create a new repository called `podcast-tracker`. Make it public (required
  for free GitHub Pages) or private (you'll need to use Cloudflare Pages).

### 2. Groq API key (for the AI analysis + chatbot)
- Go to [console.groq.com](https://console.groq.com) and sign in with Google
  or GitHub.
- Go to **API Keys** → **Create API Key**. Give it a name, copy the key.
- Keep this tab open — you'll paste the key into GitHub and Cloudflare.

Groq is free for personal use at generous limits. No credit card needed.

### 3. Cloudflare account (for the chatbot backend)
- Go to [dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up)
  and sign up.
- You'll use this to host a tiny "Worker" that powers the chatbot.

## Deploying the project

### Upload the code to GitHub

1. Download this folder (or copy it) onto your computer.
2. Open a terminal inside the folder and run:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/podcast-tracker.git
   git push -u origin main
   ```

### Add the Groq API key to GitHub

1. In your GitHub repo, go to **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret**.
2. Name: `GROQ_API_KEY`. Value: paste the key from console.groq.com.

### Kick off the first run (the big backfill)

1. Go to the **Actions** tab in your GitHub repo.
2. Click **Weekly episode update** on the left, then **Run workflow**.
3. **Check the "backfill" box**, leave "transcribe audio" on, and click
   **Run workflow**.
4. This processes every past episode. It takes a while (30–90 minutes
   depending on how many have no transcripts). You can close the tab — it
   runs in the cloud.

When it finishes, the repo will have a populated `data/` folder and
`site/data.json`. From here on, the Saturday cron run will only process
the new episode each week (fast).

### Publish the website

You have two options. Pick one.

**Option A: GitHub Pages (simplest)**

1. Repo → **Settings** → **Pages**.
2. Source: "Deploy from a branch". Branch: `main`. Folder: `/site`.
3. Click Save. In a minute or two, your site is live at
   `https://<your-username>.github.io/podcast-tracker/`.

**Option B: Cloudflare Pages (connects to GitHub, auto-deploys)**

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** →
   **Connect to Git**. Authorize GitHub.
2. Select your `podcast-tracker` repo.
3. Build settings:
   - Framework preset: **None**
   - Build command: *(leave empty)*
   - Build output directory: `site`
4. Deploy. Your site is live at a `.pages.dev` URL.

### Deploy the chatbot backend (Cloudflare Worker)

This is the "tiny backend" that answers chat questions — separate from
the website because GitHub Pages can't run code, only serve files.

1. On your computer, install the Cloudflare CLI:
   ```bash
   npm install -g wrangler
   ```
2. From the project folder:
   ```bash
   cd worker
   wrangler login
   wrangler secret put GROQ_API_KEY
   # paste your Groq key when prompted
   wrangler deploy
   ```
3. Wrangler prints a URL like `https://allin-chat.yourname.workers.dev`.
   Copy it.

### Wire the chatbot into the site

1. Open `site/config.js` in your repo.
2. Replace the empty string with your Worker URL:
   ```js
   window.APP_CONFIG = { CHAT_ENDPOINT: "https://allin-chat.yourname.workers.dev" };
   ```
3. Commit and push. GitHub Pages (or Cloudflare Pages) redeploys automatically.

That's it. You now have a living, weekly-updating podcast tracker.

## How the weekly update works

- Every Saturday at 01:00 UTC (09:00 Singapore time), GitHub Actions:
  1. Pulls new episodes from the podcast RSS feed.
  2. Grabs transcripts from allin.com, or transcribes the audio via Whisper
     on Groq if no transcript is posted.
  3. Runs the four-lens analysis (takeaways, trends, guests, sentiment).
  4. Regenerates `data.json` and the weekly brief.
  5. Commits the changes to the repo, which auto-deploys the updated site.

You don't have to do anything. If you want to trigger it manually (say, to
check a new episode before Saturday), go to Actions → Weekly episode update
→ Run workflow.

## Local development (optional)

If you want to try things on your computer before deploying:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GROQ_API_KEY=<your-key>

python src/fetch_episodes.py --backfill --limit 3      # grab 3 episodes
python src/analyze_episode.py                           # analyze them
python src/build_site.py                                # regenerate site

# then open site/index.html in your browser
```

## Cost

- GitHub Pages + GitHub Actions: **free**.
- Cloudflare Pages + Workers: **free** up to 100,000 requests/day.
- Groq API: **free** for personal-scale usage.

Total ongoing cost: $0 for typical use.

## File layout

```
podcast-tracker/
├── .github/workflows/weekly.yml   # the Saturday cron job
├── src/
│   ├── fetch_episodes.py          # pulls episodes from RSS + transcripts
│   ├── analyze_episode.py         # LLM analysis per episode
│   └── build_site.py              # generates site/data.json + brief
├── site/
│   ├── index.html                 # the dashboard
│   ├── config.js                  # chat endpoint setting
│   └── data.json                  # generated each week
├── worker/
│   ├── index.js                   # Cloudflare Worker (chatbot backend)
│   └── wrangler.toml
├── data/
│   ├── episodes/                  # one JSON per episode (full analysis)
│   ├── briefs/                    # rolling weekly briefs (markdown)
│   └── trends.json                # aggregated trends
├── requirements.txt
└── README.md
```

## Troubleshooting

**"The workflow failed at fetch_episodes.py"**
Usually means the RSS feed URL changed. Edit `RSS_CANDIDATES` at the top of
that file — any RSS URL for the show will work.

**"My chatbot says the endpoint isn't configured"**
Edit `site/config.js` and make sure `CHAT_ENDPOINT` is set to your Worker URL,
then commit.

**"The site looks empty"**
The pipeline hasn't run yet. Go to Actions → Weekly episode update → Run
workflow with the backfill box checked.

**"I want to track a different podcast"**
Change `RSS_CANDIDATES` in `src/fetch_episodes.py` and the scraping selectors
in `fetch_transcript_from_allin()`. Everything else is podcast-agnostic.
