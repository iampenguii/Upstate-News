# Upstate SC Feed Bot

A bot that watches local news RSS, National Weather Service alerts, and county
and city meeting agendas for Upstate South Carolina, and posts new items to a
private Discord server. It exists so chapter organizers do not have to check
a dozen sites by hand to know what is happening locally.

It runs every 15 minutes as a GitHub Actions workflow. There is no server to
maintain and nothing to keep running on anyone's laptop.

---

## What it watches and where it posts

Four Discord channels, one webhook each:

| Channel             | What lands there                                            |
|---------------------|-------------------------------------------------------------|
| `#news-feed`        | Local news outlets (RSS)                                    |
| `#weather-alerts`   | NWS active alerts for the seven Upstate counties            |
| `#government-watch` | County and city meeting agendas                             |
| `#bot-status`       | Weekly heartbeat and feed-failure notices (one channel, both)|

### Sources covered

**Local news (`#news-feed`):**

- WYFF News 4 (Greenville, NBC, Hearst)
- WSPA 7News (Spartanburg, CBS, Nexstar)
- Greenville Journal

**Weather (`#weather-alerts`):** NWS active alerts, filtered to Greenville,
Spartanburg, Pickens, Anderson, Cherokee, Laurens, and Union counties by SAME
code.

**Government meetings (`#government-watch`):**

- Spartanburg County Council (CivicPlus RSS)
- City of Spartanburg (CivicPlus RSS; one feed covers City Council, Design
  Review Board, and other bodies)
- City of Greenville (CivicClerk OData API; see the caveat below)
- Greenville County (proxied through a Substack newsletter; see the limitation
  below)

### What is deliberately not covered

**FOX Carolina (WHNS) is excluded.** It is a Gray Media station, and Gray's
CMS exposes no public RSS feed. Scraping it was considered and rejected: the
maintenance cost of a scraper is not worth it when the other outlets cover
the same beat. If FOX Carolina ever publishes a real feed, add it to
`news_feeds` in `config.yaml` and it will work with no code change.

**GVLtoday is excluded, and this is a correction to the original plan.** The
project was specified with GVLtoday at `https://gvltoday.6amcity.com/index.rss`
as a fourth news source. That URL does not return RSS. It returns an HTML
application shell. 6AM City, the company behind GVLtoday, rebuilt its site on
a JavaScript framework and no longer publishes a feed: `/index.rss`, `/feed`,
`/rss`, `/feed.xml`, and `/index.xml` all redirect to HTML pages. This was
verified during the build, not assumed. GVLtoday is a newsletter-first product
and the web feed appears to be gone.

This is the same call as FOX Carolina: no feed, and a scraper of a client-
rendered JavaScript app is not worth building or maintaining. GVLtoday's
coverage overlaps heavily with the three outlets that do work. If 6AM City
restores a real feed, add it back to `news_feeds` as a one-line entry. The
config file has a comment marking where it would go.

**No social media tracking.** The major platforms do not expose feed APIs the
bot can use without fragile scraping or paid access. This is a deliberate
scope limit, not an oversight. Future iterations can add specific, feed-shaped
sources as they are identified: other Substack RSS feeds, Mastodon account RSS
(every Mastodon profile has an `.rss` URL), and organization websites that
publish real RSS. Add each as a line in `news_feeds` or as a new keyword-
filtered proxy like the Substack one.

---

## How it is deployed

The bot runs as two GitHub Actions workflows:

- `.github/workflows/poll.yml` runs every 15 minutes. It fetches every source,
  posts new items, and updates state.
- `.github/workflows/heartbeat.yml` runs Monday mornings and posts the weekly
  summary to `#bot-status`.

State (which items have already been posted) lives in a SQLite file. That file
is **not** committed to the repository. It is stored in the GitHub Actions
cache and restored at the start of each run. Keeping it out of git keeps the
repo lean and keeps item titles out of git history.

### A note on cron reliability

GitHub Actions scheduled workflows are best-effort. Under platform load, a
scheduled run can be delayed by several minutes or skipped entirely. For news
and meeting agendas this does not matter; a meeting agenda posted 25 minutes
late instead of 15 is fine. For weather alerts it is marginal. A severe-weather
warning delayed by a skipped run is a real, if small, cost.

This is an accepted tradeoff for v1, in exchange for having no server to run.
If weather-alert latency ever becomes a genuine problem, that is the first
constraint to address, and the fix is to move the NWS fetcher (only the NWS
fetcher) to something with a tighter and more reliable schedule: a small always-
on worker, a cron job on a cheap VPS, or a serverless function on a real
timer. The rest of the bot can stay on the 15-minute Actions schedule.

---

## Setup

You need a private GitHub repository and a Discord server you administer.

### 1. Create the four Discord webhooks

In Discord, for each of `#news-feed`, `#weather-alerts`, `#government-watch`,
and `#bot-status`:

1. Open the channel, then **Edit Channel** > **Integrations** > **Webhooks**.
2. Click **New Webhook**, give it a name (for example "Feed Bot"), and
   **Copy Webhook URL**.

You now have four URLs. Treat them like passwords. See **Opsec** below.

### 2. Put the bot in a private repository

Create the repository as **Private**. This is not optional. A public repo plus
a `.env` file that someone pastes into the wrong place is a predictable way to
leak the webhook URLs. Private repo is the floor.

Push all the files in this project to that repo.

### 3. Add the webhook URLs as repository Secrets

In the repo: **Settings** > **Secrets and variables** > **Actions** >
**New repository secret**. Add four secrets, names exactly as below:

- `NEWS_WEBHOOK_URL`
- `WEATHER_WEBHOOK_URL`
- `GOVERNMENT_WEBHOOK_URL`
- `STATUS_WEBHOOK_URL`

The workflows read these and pass them to the bot as environment variables.
Deployed runs do **not** use a `.env` file. The `.env` file is only for running
the bot locally; see **Running locally** below.

### 4. Set the NWS contact address

Open `config.yaml` and replace the example address in `nws.user_agent` with a
real contact address the chapter controls. NWS asks for a contact so they can
reach you if the bot misbehaves; a request with no User-Agent is rejected
outright. This address goes in a committed file, so use a role address (for
example a shared inbox), not a personal one.

### 5. Enable Actions and seed the state

Open the **Actions** tab and enable workflows if prompted.

Then run the poller once in **seed mode** before letting it run normally. On a
cold start the state database is empty, so every item in every feed counts as
new. Without seeding, the first run would dump every outlet's entire current
front page into Discord at once. Seeding records everything as "already seen"
without posting it.

Go to **Actions** > **Poll feeds** > **Run workflow**, set the **seed** input
to `true`, and run it. After that one seeding run, the schedule takes over and
only genuinely new items post.

(There is also a per-source flood guard, `max_posts_per_source` in
`config.yaml`, which caps how many items one source can post in a single run.
Seeding is still the right first step; the cap is a backstop.)

### 6. Enable 2FA on the GitHub account hosting the repo

The account that owns this repo controls the webhook secrets and the
workflows. Enable two-factor authentication on it. Use a hardware security key
if you have one; an authenticator app is the minimum. Do not use SMS 2FA.

A compromised hosting account means an attacker can read the webhook URLs from
the Secrets, change the workflows, or post through the bot. 2FA is what stands
between a phished password and that outcome.

### Running locally

To run the bot from a laptop (for testing or for the manual fallback work):

1. Install Python 3.12 or newer.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in the four webhook URLs.
4. `python main.py --seed` once to seed, then `python main.py` for a real run.
5. `python heartbeat.py --force` to test the heartbeat without waiting for
   Monday.

`.env` is git-ignored. Keep it that way.

---

## The weekly heartbeat

Every Monday at 09:00 Eastern, the bot posts a line to `#bot-status` that looks
like this:

> Heartbeat (last 7 days). Polled 8 sources every 15 min. 640 of 672 runs
> completed with no source errors; 41 transient source errors total. Posted
> 88 news / 3 weather / 12 meeting items.

This is load-bearing. Read it.

**A push-only bot is invisible when nothing happens.** A genuinely quiet week,
where no meetings were scheduled and the weather was calm, looks exactly like a
bot that died on Tuesday. There is no way to tell the difference by watching
the channels. The heartbeat is the difference.

- **If the heartbeat appears and the numbers look reasonable:** the bot is
  alive and working. A low item count is fine if the week was quiet.
- **If the heartbeat does not appear at all on a Monday:** something is wrong.
  The poller may have stopped, the heartbeat workflow may be disabled, or the
  state cache may have been lost. Check the repo's Actions tab.
- **If the heartbeat appears but the numbers are wrong** (zero runs, an
  obviously too-low run count, a source listed as "currently failing"): also
  wrong, and the message itself tells you which part to look at.

If a source is in a failure streak when the heartbeat runs, the heartbeat
appends a "Currently failing" line naming it. So the weekly message is also a
standing health check, not only a liveness ping.

Do not disable the heartbeat workflow to reduce noise. One message a week is
the cost of knowing the bot is alive. It is cheap, and it closes the silent-
failure gap that would otherwise let a dead bot go unnoticed for months.

---

## Feed-failure notices

Separate from the weekly heartbeat, the bot watches itself during normal runs.

Every fetch is wrapped so one failing source never crashes the run; the bot
logs the error, counts it, and moves on. If a source keeps failing for more
than 24 hours of consecutive runs, the bot posts a notice to `#bot-status` with
the feed name, how long it has been down, and the last error. It re-posts that
notice every 24 hours while the outage continues, so a multi-day outage does
not go quiet after one message. The counter resets the moment the source
succeeds again.

The CivicClerk fetcher gets extra scrutiny because it is the most fragile
source. On any non-2xx response it logs the full status, headers, and the
first 500 characters of the body, and it posts a notice to `#bot-status` the
first time it fails rather than waiting 24 hours.

---

## Source-specific caveats

### City of Greenville (CivicClerk): undocumented API, may break

The City of Greenville publishes meetings through a CivicClerk portal. There is
no RSS feed; the bot calls the portal's backing JSON API directly.

That API is undocumented. Its JavaScript front end checks an
`X-Bypass-Recaptcha-Secret` header, which is a strong hint that the API may
rate-limit or block clients that do not look like the portal itself. The bot
mitigates this by sending a browser-like User-Agent and matching `Origin` and
`Referer` headers, and by polling no more often than every 15 minutes. It may
still break: the API could change its schema, start enforcing the recaptcha
check, or block the GitHub Actions IP ranges.

**Manual fallback runbook.** If the CivicClerk fetcher fails for more than 48
hours, treat it as broken until further notice. Do not assume it will recover
on its own. The workaround is manual and it needs a named owner:

> One chapter member visits https://greenvillesc.portal.civicclerk.com each
> Monday and posts upcoming City of Greenville meetings to `#government-watch`
> by hand.

Assign that person now, before the fetcher breaks, not after. The reason to
write this down is that the failure mode is silent in the worst way: the
fetcher goes dark, no City of Greenville meetings appear, and because the
channel still has other jurisdictions' meetings in it, nobody notices the gap
for months. The 24-hour failure notice and the weekly heartbeat both help, but
the explicit human runbook is the real backstop. Naming the fallback is what
makes it a fallback instead of a hope.

### Greenville County (Substack proxy): works, but biased

Greenville County government publishes no RSS, no iCal, and no JSON feed. None.

To cover it at all, the bot proxies the weekly newsletter of County Council
Chair Benton Blount, which lists the schedule of Council, standing committee,
and community meetings. The bot reads his Substack RSS feed and posts only the
items whose title or content matches one of: `meeting`, `council`, `agenda`,
`commission`, `hearing`, `workshop`. Tune that keyword list in `config.yaml`
after the first month, based on what actually shows up.

**Known limitation, and it is a real one.** Blount is the Council Chair. His
newsletter is not a neutral government calendar; it is curated through his
political interests. For routine, uncontested meetings this is fine. For
contested items, it is not. On an ICE OUT pressure campaign, a zoning fight, a
sheriff-accountability push, the Chair has an incentive to under-publicize the
exact meetings the chapter most needs to know about. A meeting he chooses not
to mention in his newsletter will not appear in `#government-watch`.

Treat this proxy as adequate for routine awareness and unreliable for any
campaign that is putting pressure on the Council. When the chapter is working a
contested item, somebody should be checking the county's own agenda postings
directly, not trusting this feed.

**Plan to replace it within about three months.** Two viable paths:

1. Scrape the county's own agenda directory at
   `https://www.greenvillecounty.org/apps/DirectoryListings/countycouncilagendas/`.
   It has a stable directory structure; agendas land as PDFs in dated folders.
   This is the authoritative source and is not curated by anyone with a stake
   in what gets seen.
2. Subscribe to the county's own meeting-notification email list and parse
   those emails into the bot.

Either removes the dependence on the Chair's editorial judgment. Until one of
them is built, the Substack proxy is a stopgap, and it should be understood as
one.

---

## Opsec

**Webhook URLs are passwords.** Anyone holding a webhook URL can post anything
to that channel, with no authentication. Treat the four URLs accordingly.

- They live in repository Secrets (deployed) or a git-ignored `.env` file
  (local). They are never hardcoded and never committed.
- Do not paste a webhook URL into a chat, a screenshot, a support ticket, or a
  public repo. A URL in a screenshot is a leaked URL.
- **If a URL leaks**, regenerate it. In Discord: **Edit Channel** >
  **Integrations** > **Webhooks**, select the webhook, and either delete it and
  make a new one or use the option to reset the URL. The old URL stops working
  immediately. Then update the matching repository Secret (and your local
  `.env`) with the new URL. See **Rotating a webhook URL** below for the steps.
- **Rotate all four webhook URLs every 6 to 12 months even with no known
  leak.** A leak is not always visible. Rotation on a schedule bounds how long
  a silently-leaked URL stays useful to whoever has it.

**Who should hold the credentials.** The GitHub account that hosts this repo
holds the webhook secrets and controls the workflows. That account should not
belong to the chapter's most public-facing organizer. If a chapter's public
face gets doxed and their personal accounts come under hostile probing, the
bot infrastructure should not be inside that blast radius. Put the repo on an
account held by someone less exposed, ideally an account created for
infrastructure rather than a personal one.

**Why the repo is private.** A public repo invites the predictable failure
where a `.env` file, a webhook URL, or some other secret ends up committed or
pasted and is then world-readable and indexed. Private is the floor. Private
plus 2FA on the hosting account plus webhook rotation is the actual posture.

**No member data anywhere.** No member names, emails, or other identifying
information appears in the code, in `config.yaml`, or in the committed state.
The SQLite state file stores item GUIDs and titles only, and it is not
committed at all. Keep it that way. The NWS contact address in `config.yaml`
should be a role address, not a person's.

---

## Discord as a venue: a known tradeoff

This belongs on the record, not buried.

Discord is closed-source. It is owned by a private company. That company has
cooperated with law enforcement subpoenas, and it stores all message content
server-side, indefinitely. Anything this bot posts to Discord is deposited into
that database and stays there.

For a Discord server the chapter already uses for general chatter, the bot does
not create a new exposure. The meeting agendas and news links it posts are
public information already, and the chapter's conversation is already sitting
in Discord regardless. The bot does not make that worse.

But the calculus changes if the chapter's work escalates. If `#government-watch`
becomes the place where people discuss sheriff-meeting strategy, or coordinate
pressure on contested votes, then the bot is automatically depositing the
agenda items those discussions hang off into a database that is subpoenable.
The discussion and the bot's posts sit in the same place.

This is not a v1 problem and the bot does not try to solve it. It is named here
so it is a known tradeoff rather than an unexamined assumption. If it ever does
matter, the hardening path is to move this bot, and the sensitive discussion,
off Discord onto a Matrix or Signal-based equivalent. Matrix in particular can
host the same kind of channel structure with end-to-end encryption and self-
hosted servers. Porting the bot would mean rewriting only `discord_post.py`;
the fetchers and state logic are venue-agnostic.

---

## Maintenance

### Adding a new CivicPlus calendar

CivicPlus calendar feeds are identified by a CID number. To add another
calendar for a jurisdiction already in `config.yaml` (for example a Planning
Commission under Spartanburg County):

1. Go to that jurisdiction's calendar page, for example
   `https://www.spartanburgcounty.gov/calendar.aspx`.
2. Find the calendar you want and note its CID. It is in the calendar's RSS or
   page URL as `CID=NN`.
3. In `config.yaml`, add that number to the `cids` list for the matching
   `civicplus_feeds` entry:

   ```yaml
   - name: "Spartanburg County Council"
     jurisdiction: "Spartanburg County"
     feed_template: "https://www.spartanburgcounty.gov/RSSFeed.aspx?ModID=58&CID={cid}"
     cids: [22, 31]   # 22 = Council, 31 = the new calendar
   ```

That is the whole change. To add a calendar for a brand-new jurisdiction, copy
a `civicplus_feeds` block and set its `name`, `jurisdiction`, and
`feed_template`.

### Rotating a webhook URL

Whether a URL leaked or it is just the scheduled 6-to-12-month rotation:

1. In Discord, open the channel's **Integrations** > **Webhooks**.
2. Select the webhook and either reset its URL or delete it and create a new
   one. The old URL stops working immediately.
3. Copy the new URL.
4. In the GitHub repo, go to **Settings** > **Secrets and variables** >
   **Actions**, open the matching secret (for example `NEWS_WEBHOOK_URL`), and
   update it with the new URL.
5. If anyone runs the bot locally, update the URL in their `.env` too.
6. The next scheduled run uses the new URL. Nothing else needs to change.

---

## Project layout

```
main.py                     Poll entry point. Orchestrates the fetchers.
heartbeat.py                Weekly summary builder. Separate workflow step.
discord_post.py             Webhook posting and one embed builder per source type.
state.py                    SQLite wrapper: de-duplication, error counts, run stats.
config.yaml                 Channel routing, feed list, county codes, keywords.
.env.example                Template for the four webhook URLs (local runs only).
requirements.txt            Python dependencies.
fetchers/
  rss.py                    Generic RSS fetcher: news, Substack, CivicPlus.
  nws.py                    NWS active-alerts fetcher with county filtering.
  civicclerk.py             CivicClerk OData fetcher for City of Greenville.
.github/workflows/
  poll.yml                  Every 15 minutes.
  heartbeat.yml             Mondays, 09:00 ET.
```

### State tables

SQLite, one table per feed type: `news_items`, `weather_alerts`, `meetings`,
`substack_items`. Each row is `guid` (primary key), `source`, `posted_at`,
`title` (kept for debugging only). A `source_errors` table tracks failure
streaks; a `runs` table records per-run counters that the heartbeat sums.
Rows older than 90 days are pruned at the end of every run.

For NWS alerts specifically: an alert that is genuinely updated (severity
change, area expansion) gets a fresh alert id from NWS. The bot keys on that
id, so an update posts as a new embed. The bot does not edit prior Discord
messages; it posts the new version and leaves the old one in place.
