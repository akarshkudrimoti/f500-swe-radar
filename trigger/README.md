# Trigger

A Cloudflare Worker that fires the radar's `watch.yml` workflow every 15 minutes.

## Why this exists

GitHub treats `schedule:` as best effort and drops runs under load. Measured on
this repo: a `*/15` cron registered at 21:21 UTC produced **one** run by 00:03
UTC — an effective cadence of about 2h41m instead of 15 minutes. Making the repo
public removed the billing ceiling but not the scheduling one.

`workflow_dispatch` is not throttled that way. Cloudflare keeps its own cron, so
the Worker calls the dispatch API and GitHub runs the workflow on request. The
workflow itself is untouched; only the decision of *when* moves.

The `schedule:` block stays in `watch.yml` as a free backstop — if the Worker is
ever down, GitHub's unreliable schedule is better than no schedule.

## Setup

Everything below runs on your machine. The token is stored as a Cloudflare
secret and never enters this repository.

**1. Create a GitHub token.** Settings → Developer settings → Personal access
tokens → Fine-grained tokens. Scope it to `f500-swe-radar` only, and give it one
permission: **Actions: Read and write**. Nothing else. Copy the token.

**2. Install and sign in.**

```bash
npm install -g wrangler
wrangler login
```

**3. Store the token as a secret.**

```bash
cd trigger
wrangler secret put GITHUB_TOKEN
```

Paste the token at the prompt. Optionally add a manual-fire key:

```bash
wrangler secret put TRIGGER_KEY
```

**4. Deploy.**

```bash
wrangler deploy
```

## Checking it

```bash
wrangler tail                      # live log, one line per cron fire
curl https://f500-swe-radar-trigger.<your-subdomain>.workers.dev
```

The health page reports the repo, workflow, branch and whether the token is set.
It never prints the token.

With `TRIGGER_KEY` set you can fire it by hand:

```bash
curl "https://f500-swe-radar-trigger.<your-subdomain>.workers.dev/?key=YOUR_KEY"
```

Confirm runs are arriving as `workflow_dispatch` rather than `schedule`:

```bash
gh run list --workflow="Watch for changes" --limit 10 \
  --json createdAt,event -q '.[] | "\(.createdAt) \(.event)"'
```

## Behaviour worth knowing

- **It skips when a run is already in progress or queued.** The workflow's
  concurrency group would queue a second run rather than run it twice, but a
  queue that never drains is a slow leak; when a fetch overruns the interval,
  skipping is the honest response.
- **If the runs API can't be read, it dispatches anyway** and lets GitHub queue —
  failing toward a redundant run rather than a missed one.
- **A dispatch returns 204 with no body.** Anything else is logged with the
  status and the first 300 characters of the response.

## Cost

Free tier: 100,000 Worker requests/day. At 15 minutes this uses 96, plus two
GitHub API calls per fire. Cron triggers are capped per Worker on the free plan
(this uses one).

## Changing the interval

Edit `crons` in `wrangler.toml` and redeploy. Cloudflare's granularity goes down
to one minute, but each fire makes the workflow probe 285 real employers' career
sites — the limit worth respecting is theirs, not the platform's.
