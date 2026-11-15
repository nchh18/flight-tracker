# SIN cheap flight tracker

A free tracker that checks Google Flights fares out of Singapore every 8 hours on GitHub's servers, so your laptop can stay off. It keeps a dashboard of the 3 cheapest fares for each destination and messages you on Telegram when it spots a deal.

**Destinations:**

- **Asia-Pacific:** Thailand, Vietnam, Hong Kong, Taiwan, China, Japan, Korea, Australia and New Zealand. Return trips only.
- **Europe:** Portugal, Spain, France, Ireland, Netherlands, Belgium, Germany, Luxembourg, Italy, Greece, Croatia, Slovenia, Austria, Czechia, Hungary, Denmark, Sweden, Finland, Iceland and Norway. Both one-way and return.

Departures are searched from 2 weeks to 1 year out.

**No budget airlines.** Any itinerary with a leg on a low-cost carrier is skipped, for example Scoot, AirAsia, Jetstar, VietJet, Ryanair or easyJet. The full list is `exclude_airlines` in `config.toml`.

**What you get:**

- **Dashboard.** It shows the 3 cheapest fares per destination, the cheapest fare in each city, and how each compares with the usual price. Tap a fare to open it in Google Flights.
- **Telegram alerts.** You get one when a fare is at least 25% below what that route usually costs, or under a target price you set. You also get a short daily summary.

## How it works

GitHub Actions runs `tracker.py` at about 08:17, 16:17 and 00:17 Singapore time. GitHub's scheduler can be 5–30 minutes late.

Each run:

1. Does about 180 searches in roughly 15 minutes.
2. Works through the departure weeks that have gone longest without a check, so the year ahead gets covered every week or two.
3. Re-checks the cheapest dates found so far.
4. Saves the results to `data/prices.csv` and rebuilds the dashboard.
5. Commits both back to the repo.

Prices come from Google Flights through the open-source [fast-flights](https://github.com/AWeirdDev/flights) library.

## Setup (about 15 minutes)

### 1. Create a Telegram bot

1. In Telegram, open a chat with **@BotFather**, send `/newbot` and follow the prompts. It gives you a **token** like `123456789:AAH...`.
2. Open a chat with your new bot and send it any message, for example `hi`.
3. In a browser, open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`, with your token pasted in.
4. Find `"chat":{"id":123456789`. That number is your **chat id**.

### 2. Decide: public or private repo

| | Public repo | Private repo |
|---|---|---|
| Dashboard | A proper web page at `https://<you>.github.io/<repo>/`, which works well on your phone | The `DASHBOARD.md` file in your repo, opened on github.com or in the GitHub app |
| Who can see it | Anyone with the link can see the fares and your destination list. Your Telegram details stay secret either way. | Only you |
| Free run time | Unlimited | 2,000 minutes a month; this uses about 1,400 |

GitHub Pages, the free web hosting, only works on public repos unless you pay for GitHub Pro.

### 3. Put the code on GitHub

1. Create a new repository on github.com.
2. Upload everything in this folder, keeping the structure:
   - `.github/workflows/tracker.yml`
   - `.gitignore`
   - `config.toml`
   - `requirements.txt`
   - `tracker.py`
   - `README.md`

**Add file → Upload files** accepts dragged folders. If the `.github` folder doesn't come through:

1. Use **Add file → Create new file**.
2. Type `.github/workflows/tracker.yml` as the name.
3. Paste in the file's contents.

### 4. Add your Telegram secrets

1. Go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add two secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

### 5. Run it once

1. Go to the **Actions** tab, open **Flight tracker**, and click **Run workflow**.
2. Wait about 15 minutes.
3. Afterwards, the repo has a `data` folder, a `docs` folder and a `DASHBOARD.md`.

If the last step fails with a permissions error:

1. Go to **Settings → Actions → General → Workflow permissions**.
2. Choose **Read and write permissions**.
3. Run it again.

### 6. Public repo only: turn on the dashboard page

1. Go to **Settings → Pages**.
2. Under **Build and deployment**, choose **Deploy from a branch**.
3. Pick branch **main** and folder **/docs**, then click **Save**.

After a minute or two the page link appears at the top of that settings page.

To get the link in your daily Telegram summary, paste it into `config.toml`:

```toml
[dashboard]
url = "https://<you>.github.io/<repo>/"
```

That's it. From now on it runs by itself every 8 hours.

## Reading the dashboard

The top of the page lists every destination with its cheapest return fare, cheapest first, split into Asia-Pacific and Europe. Use it to see where it's cheap to go right now.

Under each destination you'll see:

- **The 3 cheapest fares**, each with its dates, airline, stops and when it was last checked.
- **A "below typical" tag** when a fare is at least 5% under the usual price for that city. The usual price is the median of everything seen in the last 30 days.
- **"Cheapest in each city"**, which expands to show the best fare for every airport in that destination.

Only fares seen in the last 7 days are shown. Older prices may no longer be available.

The first day or two will look sparse. The full year of dates fills in over about a week.

## Tuning (`config.toml`)

Edit it on github.com with the pencil icon. Changes apply from the next run.

Destinations are listed under `[regions.<name>]`. Each belongs to a group, `[groups.asia]` or `[groups.europe]`, which holds shared settings. A setting on a destination overrides its group's.

| Setting | What it does |
|---|---|
| `exclude_airlines` | Airlines to skip (budget carriers by default). Remove a name to allow it, or add one as Google Flights spells it. |
| `airports` per destination | Where to search. Add or remove IATA codes. |
| `trips` per group or destination | `["return"]`, `["one-way"]`, or both |
| `trip_lengths` per group or destination | Return-trip lengths to try, in days |
| `round_trip_target`, `one_way_target` | Telegram alert whenever a fare is at or below this. These are rough starting guesses for full-service airlines, so adjust them after a week. Set to `0` to turn off. |
| `drop_pct` | How far below typical counts as a deal for alerts (default 25%) |
| `top_n`, `lookback_days` (under `[dashboard]`) | Fares shown per destination, and how recent they must be |
| `days_ahead_min` / `days_ahead_max` | Departure window (default 14–365 days) |
| `one_way_samples` / `round_trip_samples` | Searches per airport per run. More means faster coverage but longer runs. Europe uses 2 because it has many airports. |
| `max_stops` | Uncomment to cap stops. Transits are allowed by default. |
| `hide_self_transfer` | Hides separate-ticket itineraries, which are cheaper but unprotected if you miss a connection |

To add a destination, copy a block such as `[regions.greece]`, give it a new name, set its `group`, and list its airports. You can also give an airport a display name under `[names]`.

When you change the airline list, fares on newly excluded airlines are removed from the saved history on the next run, and those dates get searched again.

To change how often it runs, edit the `cron` line in `.github/workflows/tracker.yml`. Times are in UTC.

## Testing on your own computer (optional)

```bash
pip install -r requirements.txt
python tracker.py --dry-run --limit 10 --force-digest   # print messages, save nothing
python tracker.py --dashboard                           # rebuild the dashboard from saved data
```

## Good to know

- **Unofficial data source.** fast-flights reads the Google Flights website. Google doesn't offer an official API, and automated use is against its terms, so keep the volume modest; the defaults do. If Google changes its site, searches will fail until fast-flights is updated. The fix is usually to bump the version in `requirements.txt`.
- **Rate limiting.** If Google stops answering GitHub's servers, the run stops early and you get a ⚠️ message, at most once a day. Usually the next run is fine. If it keeps happening, lower the samples or remove a few airports.
- **Fares a year out are patchy.** Airlines usually open sales about 11 months ahead, so the far end of the window often has no flights yet. Those weeks fill in over time.
- **Prices are a snapshot.** They're for 1 adult in economy, and budget airlines may not include bags. Always confirm on the booking page.
- **Schedule stopped?** GitHub can pause scheduled workflows in inactive repos. The tracker's own commits normally prevent that. If it does happen, re-enable it from the Actions tab.
