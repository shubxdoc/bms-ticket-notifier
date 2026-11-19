# BMS Ticket Notifier

Watches one or more movies on [BookMyShow](https://in.bookmyshow.com) and sends you a **Telegram message** and/or an **email** when showtimes open at your preferred theatres and screens.

## What you get alerted about

| Alert | Fires when |
|---|---|
| 📅 **NEW DATE OPENED** | a date in BookMyShow's date strip goes from "not open" to bookable **and** has matching shows at your filtered theatres |
| 🆕 **NEW** | a new showtime appears, or an existing show gains a new price category at your filtered theatres |
| 🟢 **BACK** | a price category that was **sold out** has seats again |

**You will not be alerted when** seats merely shift between "available", "filling fast", and "almost full", when a show is removed, when a price changes, or when a format opens only at theatres outside your `BMS_THEATRE` list.

## How it works

1. **cron-job.org** calls the GitHub API every ~10 minutes to trigger the workflow via `workflow_dispatch`. (GitHub's own `schedule:` trigger is unreliable on forks and often never fires.)
2. `main.py` runs with `curl_cffi` to impersonate real browser TLS fingerprints (starting with Safari 17 and rotating to Chrome 120 / Chrome 124 on retries) and reuses the working session across requests to bypass BookMyShow's HTTP 403 blocks.
3. For each movie URL in `BMS_URL`, it auto-discovers available formats (`ET` codes) matching your `#format` tag, removes duplicate fallback shows returned for unopened dates, and filters strictly by `BMS_THEATRE` (including optional `:Screen` rules) and `BMS_TIME`.
4. State is merged safely (`merge_state`): if any date fails with an HTTP 403, its previous state is preserved so it won't trigger false `NEW` alerts on the next run.
5. If anything changed, it sends a Telegram message and/or an email via Resend, and commits the updated `bms_state.json` back to the repo (`[skip ci]`).

## Setup

### 1. Fork the repo and enable Actions

Fork this repository, open the **Actions** tab, and enable workflows.

### 2. Create a Telegram bot (optional, recommended)

1. Message **@BotFather** on Telegram, send `/newbot`, and copy the **token**.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id`.

### 3. Create a Resend account (optional)

Sign up at [resend.com](https://resend.com) and create an API key. Without your own verified domain, you can only send **to the email you signed up with**, from an address like `anything@resend.dev`.

### 4. Add repository secrets

**Settings → Secrets and variables → Actions → Secrets**

| Secret | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID |
| `RESEND_API_KEY` | Resend API key |
| `RESEND_FROM_EMAIL` | Sender, e.g. `anything@resend.dev` |
| `RESEND_TO_EMAIL` | Your Resend signup email |
| `PROXY_URL` | *(Optional)* HTTP/SOCKS5 proxy URL if you want to route traffic through a custom proxy |

You can use Telegram, email, or both. Any channel with missing secrets is skipped automatically.

### 5. Add repository variables

**Settings → Secrets and variables → Actions → Variables**

| Variable | Description | Example |
|---|---|---|
| `BMS_URL` | One or more BookMyShow movie URLs, comma-separated, with optional `#format` tags | `https://in.bookmyshow.com/movies/hyderabad/ramayana/buytickets/ET00493076#hindi 3d+hindi 2d` |
| `BMS_DATES` | Dates to watch (`YYYYMMDD`), comma-separated | `20261114,20261115,20261116,20261117` |
| `BMS_THEATRE` | Theatre filter (comma-separated, case-insensitive). Supports `Theatre:Screen` rules | `ALLU,Prasads:PCX,AAA:LED` |
| `BMS_TIME` | Time-of-day filter, comma-separated *(delete this variable to include all time slots)* | `afternoon,evening` |

#### Multi-movie & `#format` auto-discovery (`BMS_URL`)

You only need **one URL per movie** (any format's URL works as the entry point). Append `#` followed by the formats or languages you want, separated by `+`:

* **Specific formats for one movie:** `.../ET00493076#hindi 3d+hindi 2d`
* **Any language for specific formats:** `.../ET00493076#3d+2d`
* **All formats of a movie:** `.../ET00493076#all` or `.../ET00493076`
* **Multiple different movies:** separate URLs with a comma:


Only the city and `ET` code are read from the URL; the movie slug is ignored. If a movie's bookings haven't opened yet, it will log `Unknown Movie` until BookMyShow activates its layout widgets—tracking still works normally.

#### Theatre & screen filtering (`BMS_THEATRE`)

* Plain names (e.g. `ALLU`, `Prasads`) match **all** screens at that theatre.
* Adding `:Screen` (e.g. `Prasads:PCX`, `AAA:LED`) matches that theatre **only** when the show's screen or format label contains that keyword.

#### Time periods (`BMS_TIME`)

| Period | Shows starting |
|---|---|
| `morning` | 6:00 AM – 11:59 AM |
| `afternoon` | 12:00 PM – 3:59 PM |
| `evening` | 4:00 PM – 6:59 PM |
| `night` | 7:00 PM – 11:59 PM |

> **Note:** GitHub rejects empty variable values (and `""` is read as literal quotes). To watch **all** showtimes—including early morning shows before 6:00 AM—delete the `BMS_TIME` variable entirely.

**Supported cities:** Chennai, Mumbai, Delhi-NCR, Bengaluru, Hyderabad, Kolkata, Pune, and Kochi. Other cities can be added to `REGION_MAP` in `main.py`.

### 6. Schedule it with cron-job.org

## Running locally

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen

export BMS_URL="[https://in.bookmyshow.com/movies/hyderabad/ramayana/buytickets/ET00493076#hindi](https://in.bookmyshow.com/movies/hyderabad/ramayana/buytickets/ET00493076#hindi) 3d+hindi 2d"
export BMS_DATES="20261114,20261115"
export BMS_THEATRE="ALLU,Prasads:PCX,AAA:LED"
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"

uv run --with curl_cffi main.py
```

## Files

| File | Purpose |
|---|---|
| `main.py` | Fetches showtimes via `curl_cffi`, auto-discovers formats, detects changes, sends alerts |
| `bms_state.json` | Last-seen state, committed by the workflow |
| `.github/workflows/bms-checker.yml` | The workflow triggered by `cron-job.org` (`uv run --with curl_cffi main.py`) |
| `pyproject.toml`, `uv.lock` | Python dependencies |
