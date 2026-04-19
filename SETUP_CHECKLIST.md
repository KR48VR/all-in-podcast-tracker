# Quick Setup Checklist

A simpler at-a-glance version of the README. Tick things off as you go.

- [ ] **Create GitHub account** at github.com (if you don't have one)
- [ ] **Create a new repo** called `podcast-tracker` (public is easiest)
- [ ] **Upload this code** to the repo (see README for the git commands)
- [ ] **Get a free Groq API key** at console.groq.com → API Keys
- [ ] **Add the Groq key as a GitHub secret** named `GROQ_API_KEY`
      (Repo → Settings → Secrets → Actions → New repository secret)
- [ ] **Run the first job manually** — Actions tab → Weekly episode update
      → Run workflow → tick **both** `backfill` and `transcribe_audio` → Run
      (transcribe_audio is what gives you ▶ jump-to-moment timestamp links)
- [ ] **Wait for the job to finish** (~1–3 hours for 300+ episodes with Whisper;
      ~30–60 min without)
- [ ] **Turn on GitHub Pages** — Settings → Pages → Branch: main, Folder: /site
- [ ] **Create a Cloudflare account** at dash.cloudflare.com/sign-up
- [ ] **Install wrangler** — `npm install -g wrangler`
- [ ] **Deploy the Worker** — from the `worker/` folder run:
      `wrangler login` → `wrangler secret put GROQ_API_KEY` → `wrangler deploy`
- [ ] **Copy the Worker URL** that wrangler prints
- [ ] **Paste the URL into `site/config.js`** as the value of `CHAT_ENDPOINT`
- [ ] **Commit and push** — the site redeploys automatically

Total time: ~15 minutes of clicking, plus waiting for the backfill.

After the first Saturday, the site updates itself with no action from you.
