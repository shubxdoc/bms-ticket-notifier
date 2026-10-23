# BMS Ticket Notifier

Watches a movie on [BookMyShow](https://in.bookmyshow.com) and sends you a **Telegram message** and/or an **email** when something you care about changes.

## What you get alerted about

| Alert | Fires when |
|---|---|
| 📅 **NEW DATE OPENED** | a date in BookMyShow's date strip goes from "not open" to bookable |
| 🆕 **NEW** | a new showtime appears, or an existing show gains a new price category |
| 🟢 **BACK** | a price category that was **sold out** has seats again |

**You will not be alerted when** seats merely fill up or free up between "available", "filling fast" and "almost full", when a show is removed, or when a price changes. Only a sold-out category returning counts.

## How it works

1. **cron-job.org** calls the GitHub API every ~10 minutes to start the workflow. (GitHub's own `schedule:` trigger is unreliable on forks and often never fires.)
2. `main.py` asks BookMyShow's showtimes API for each date you listed, filters by theatre and time of day, and compares the result with the last saved state in `bms_state.json`.
3. If something changed, it sends a Telegram message and/or an email through Resend.
4. The workflow commits the updated `bms_state.json` back to the repo (commit message ends with `[skip ci]`).


## Setup

### 1. Fork the repo and enable Actions

### 2. Create a Telegram bot (optional, recommended)

1. Message **@BotFather** on Telegram, send `/newbot`, and copy the **token**.
2. Send your new bot any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat.id`. That is your **chat id**.

### 3. Create a Resend account (optional)

Sign up at [resend.com](https://resend.com) and create an API key. Without your own verified domain you can only send **to the email you signed up with**, from an address like `anything@resend.dev`.

### 4. Add repository secrets

**Settings → Secrets and variables → Actions → Secrets**

| Secret | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat id |
| `RESEND_API_KEY` | Resend API key |
| `RESEND_FROM_EMAIL` | Sender, e.g. `anything@resend.dev` |
| `RESEND_TO_EMAIL` | Your Resend signup email |

You can use Telegram, email, or both. A channel whose secrets are missing is skipped, and the other still works.

### 5. Add repository variables

**Settings → Secrets and variables → Actions → Variables**

| Variable | Description | Example |
|---|---|---|
| `BMS_URL` | BookMyShow tickets page URL | `https://in.bookmyshow.com/movies/delhi/.../ET00493076` |
| `BMS_DATES` | Dates to watch, `YYYYMMDD`, comma-separated | `20261105,20261106,20261107,20261108` |
| `BMS_THEATRE` | Theatre name filter (case-insensitive substring match, comma-separated) | `PVR,IMAX` |
| `BMS_TIME` | Time-of-day filter, comma-separated | `afternoon,evening` *leave empty to include all time slots* |

**Time periods:**

| Period | Shows starting |
|---|---|
| `morning` | 6:00 AM – 11:59 AM |
| `afternoon` | 12:00 PM – 3:59 PM |
| `evening` | 4:00 PM – 6:59 PM |
| `night` | 7:00 PM – 11:59 PM |

> **Finding the right `BMS_URL`:** the part `ET00493076` is the *event code*, and it differs for each language and format of the same film (for example Hindi 3D versus Telugu 2D). Open the exact listing you want on BookMyShow and copy its code from the URL. Only the city and the `ET` code are read from the URL; the movie slug is ignored.


**Supported cities:** Chennai, Mumbai, Delhi-NCR, Bengaluru, Hyderabad, Kolkata, Pune and Kochi. Other cities need an entry in `REGION_MAP` in `main.py`.

### 8. Schedule it with cron-job.org

## Running locally

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen

export BMS_URL="https://in.bookmyshow.com/movies/hyderabad/namit-malhotras-ramayana/buytickets/ET00493076"
export BMS_DATES="20261105,20261106"
export BMS_THEATRE="Prasads"
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"

uv run main.py
```

## Files

| File | Purpose |
|---|---|
| `main.py` | Fetches showtimes, detects changes, sends alerts |
| `bms_state.json` | Last-seen state, committed by the workflow |
| `.github/workflows/bms-checker.yml` | The workflow started by cron-job.org |
| `pyproject.toml`, `uv.lock` | Python dependencies |
