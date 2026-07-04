# AMC Ticket Watcher

Polls specific AMC showtime pages (e.g. *The Odyssey*, IMAX, AMC Lincoln
Square 13), looks for open seats within rows you care about, and pings a
Discord webhook when it finds a qualifying block of adjacent seats.

## How it works

It uses a real (headless) browser via Playwright to open each showtime's
seat-picker page — the same page you'd see clicking through manually — and
reads the seat map straight out of the rendered DOM. This is more reliable
than trying to guess AMC's private internal API endpoints, which aren't
public and change without notice. AMC does have an official developer API,
but it requires a vetted vendor key meant for business partners, so it's not
practical here.

**Why you supply the showtime URL yourself:** AMC's site doesn't offer a
stable way to programmatically search "showtimes for movie X at theatre Y"
that holds up over time — that part of their site changes often. Grabbing
the direct link once per showtime is a 10-second manual step that makes the
rest of the script much less fragile.

## 1. Get your showtime URLs

For each showtime you want watched:
1. Go to amctheatres.com, find *The Odyssey* at AMC Lincoln Square 13, IMAX.
2. Click through to the seat picker for that specific date/time.
3. Copy the URL from your browser's address bar.
4. Paste it into `config.yaml` under `showtimes:` with a readable `label`.

## 2. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 3. Configure

Edit `config.yaml`:
- `allowed_rows` — list every row letter you'd accept (AMC skips the letter
  "I"). Look at the actual seat map once to confirm which letters are near
  the back if you want "row G and further back."
- `min_seats_together` — e.g. `2` for a pair.
- `discord_webhook_url` — Discord server → Settings → Integrations →
  Webhooks → New Webhook → Copy URL.
- `poll_interval_seconds` — how often to check (90–120s is reasonable; don't
  go much lower or you risk getting rate-limited/blocked).
- `notify_cooldown_minutes` — avoid re-pinging you for the same open seats
  over and over.

## 4. Test it

```bash
python watcher.py --config config.yaml --once
```

If it reports "No seats parsed" for a showtime, AMC's markup doesn't match
the detection logic. Run:

```bash
python watcher.py --config config.yaml --debug "Fri 7/10 7:00 PM IMAX" --headed
```

`--headed` opens a visible browser window so you can watch what actually
loads. This also saves `debug_single.png` and `debug_single.html`, and
prints a status breakdown, e.g.:

```
Seat status breakdown (seat_type, status) -> count:
  AMC Club Rocker                              OPEN      ->  341
  AMC Club Rocker                              OCCUPIED  ->  126
  Wheelchair Space                             OPEN      ->    6
  Wheelchair Companion AMC Club Rocker         OCCUPIED  ->    6
Rows detected: A, B, C, D, E, F, G, H, J, K, L, M
```

AMC's seat picker uses one hidden checkbox `<input>` per seat with a `name`
attribute holding the seat id (e.g. `"A18"`) and an `aria-label` describing
its type and status — taken seats start with the word **"Occupied"** (e.g.
`"Occupied AMC Club Rocker A18"` vs. just `"AMC Club Rocker A33"` when open).
That's read directly from the app's own state rather than inferred from
colors or styling, so it should hold up well — but if AMC changes this
markup later, re-run `--debug --headed`, open `debug_single.html`, and
search for `aria-label=` on an `<input>` to see the new pattern, then update
`SEAT_ID_RE` / `find_seats()` near the top of `watcher.py` to match.

## 5. Run continuously

```bash
python watcher.py --config config.yaml
```

## Deploying it for free (no server needed): GitHub Actions

This is the best free option if you don't have a server or VPS. It's
genuinely free (unlimited minutes on a public repo), needs zero
infrastructure, and a workflow file is already included at
`.github/workflows/watcher.yml`.

**The tradeoff:** GitHub's scheduler can't run more often than every 5
minutes (and may slip a few minutes during high load), so your effective
check interval becomes ~5 min instead of `poll_interval_seconds`. Fine for
this use case — seats don't usually vanish in seconds.

### Setup

1. **Create a new GitHub repo** and push this folder to it (a **public**
   repo gets unlimited free Actions minutes; a private repo gets 2,000
   free minutes/month, which is roughly 400+ runs at ~5 min each — likely
   plenty, but public is safer if you want this running for weeks).

   ```bash
   git init
   git add .
   git commit -m "AMC ticket watcher"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **Add your Discord webhook as a secret**, not in `config.yaml`:
   - Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: your webhook URL
   
   (Leave `discord_webhook_url` blank in `config.yaml` — the script prefers
   the environment variable automatically.)

3. **Fill in your real showtime URLs** in `config.yaml` and commit/push.

4. That's it. The workflow runs automatically every ~5 minutes. You can
   also trigger a manual run any time from the repo's **Actions** tab →
   "AMC Ticket Watcher" → "Run workflow" — useful for testing without
   waiting for the schedule.

5. Check the **Actions** tab to see run logs/history, confirm it's finding
   seats correctly, and debug anything that fails.

Notification cooldown state (`watcher_state.json`) persists across runs via
GitHub's build cache, so you won't get re-pinged every 5 minutes for the
same open seats.

### About the 2,000 free minutes/month (private repos only)

**Public repos get unlimited Actions minutes — this whole problem goes away
if the repo is public.** The rest of this section only matters if you want
to keep it private.

The workflow uses a prebuilt Playwright Docker image
(`mcr.microsoft.com/playwright/python`) instead of installing Chromium from
scratch every run — that was the main reason a run took ~3 minutes (browser
download + OS dependency installation on a brand-new disposable VM, every
single time). With the browser already baked into the image, a run should
now typically finish in under a minute.

That said, **GitHub bills each job rounded up to the nearest minute**, so
even a 45-second run costs 1 minute. Do the math for a private repo running
every 5 minutes, 24/7, for a full month:

```
24 hours × 60 min / 5 min interval = 288 runs/day
288 runs/day × 30 days              = 8,640 runs/month
8,640 runs × ≥1 billed minute each  = 8,640+ minutes/month
```

That's well over the 2,000 free minutes even after the speed fix — the
5-minute schedule itself is the issue, not per-run efficiency, once you're
running continuously for weeks. Your real options if staying private:

- **Widen the schedule.** Every 30 minutes → ~1,440 runs/month → comfortably
  under 2,000 minutes. Change the cron line in
  `.github/workflows/watcher.yml` to `*/30 * * * *`.
- **Only run it during the window that matters** (e.g. enable the workflow
  a few days before the release, disable it after) rather than leaving it
  on indefinitely.
- **Make the repo public.** Nothing sensitive is exposed either way — your
  Discord webhook lives in a repo *secret*, which stays hidden regardless of
  the repo's visibility.

If you want true near-real-time (60-90s) polling indefinitely without any
minute limits, that's what the Oracle Always Free VM option below is for.

**Note:** scheduled workflows on GitHub automatically stop running if the
repo has had zero commits for 60 days — not a concern for a short-term
ticket watch, but worth knowing if you leave this running long-term.

## Alternative: a small always-on VM

If you want tighter polling (closer to real-time, e.g. every 60-90s) and
don't mind a bit more setup, Oracle Cloud's "Always Free" tier includes a
small VM that runs 24/7 at no cost indefinitely (requires signing up with a
credit card for verification, but the free-tier resources aren't billed).
On a VM like that, use the `systemd` or Docker approach below instead of
GitHub Actions.

## Running it yourself on a machine you control

**A small VPS / home server (simplest, recommended):**
```bash
nohup python watcher.py --config config.yaml > watcher.log 2>&1 &
```
or better, a `systemd` service so it restarts on crash/reboot:
```ini
# /etc/systemd/system/amc-watcher.service
[Unit]
Description=AMC Ticket Watcher
After=network.target

[Service]
WorkingDirectory=/opt/amc-watcher
ExecStart=/opt/amc-watcher/venv/bin/python watcher.py --config config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Then: `sudo systemctl enable --now amc-watcher`

**Docker** (if you'd rather not manage Python/Playwright deps on the host):
use the official `mcr.microsoft.com/playwright/python` base image, `COPY`
this folder in, `pip install -r requirements.txt`, and `CMD ["python",
"watcher.py"]`.

## Notes / limits

- This only *reads* seat availability — it does not select or purchase
  tickets. You still book manually once you get the alert.
- Respect AMC's terms of service and don't set the poll interval too
  aggressively.
- Seat-map markup can change; the `--debug` flow above is there so you can
  fix detection yourself without waiting on anyone else.
