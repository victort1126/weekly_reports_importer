# Weekly Report Slack Importer

A private Slack app (Socket Mode) that watches a private channel for forwarded
weekly-work-report DMs, extracts structured fields with Azure OpenAI (GPT-5),
enriches the employee record from a "Roster" Google Sheet tab, and appends the
result to a "Report Log" tab.

## How it works

1. You forward a staff member's weekly-report DM, upload a Markdown report, or
   upload an Excel workbook containing several employees' reports into a
   private Slack channel.
2. The app receives the message over Socket Mode. It extracts forwarded text,
   downloads `.md`/`.xlsx` attachments, and converts workbook worksheets into
   structured text while preserving rows and columns.
3. Azure OpenAI (GPT-5) extracts structured fields (employee name, team, ISO
   week, tasks completed, next steps, blockers/risks, needs from others, demo
   links) as strict JSON. Bundled workbooks produce one report per employee.
4. The app looks up the employee in the `Roster` tab of your Google Sheet and
   corrects/fills in the team (and captures email/location) from there.
5. Decorative emojis are removed, Markdown formatting is converted to Google
   Sheets rich text, and one row per employee is appended to `Report Log`.
6. The bot replies in-thread with a summary (success) or an error message
   (failure) so you get immediate feedback.

## 1. Prerequisites

- Python 3.10+
- A Slack workspace where you can create/install a private app
- An Azure OpenAI resource with a GPT-5 deployment
- A personal Google account with a Google Sheet containing `Roster` and
  `Report Log` tabs (see column layout below)

## 2. Slack app setup

1. Go to https://api.slack.com/apps -> **Create New App** -> **From scratch**.
   Name it (e.g. "Weekly Report Importer") and pick your workspace.
2. **Socket Mode**: Settings -> Socket Mode -> toggle on. This generates an
   **App-Level Token** (starts with `xapp-`) with scope `connections:write`.
   Copy it into `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** -> Bot Token Scopes, add:
   - `groups:history`
   - `groups:read`
   - `chat:write`
   - `users:read`
   - `users:read.email`
   - `files:read`
4. **Event Subscriptions** -> Subscribe to bot events -> add `message.groups`.
5. Click **Install to Workspace** at the top of OAuth & Permissions. Copy the
   **Bot User OAuth Token** (starts with `xoxb-`) into `SLACK_BOT_TOKEN`.
6. Create the private channel you'll forward reports into, then invite the
   bot: `/invite @YourAppName`.
7. (Optional) If you want the app to *only* ever process that one channel, put
   its channel ID (right-click the channel -> View channel details -> copy ID
   at the bottom) into `SLACK_CHANNEL_ID`. Otherwise leave it blank and the app
   will process any private channel the bot is a member of.

## 3. Azure OpenAI setup

No portal changes are required beyond having a GPT-5 deployment. Gather:

- `AZURE_OPENAI_ENDPOINT` - e.g. `https://your-resource-name.openai.azure.com`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT` - the deployment name (not the base model name)
- `AZURE_OPENAI_API_VERSION` - a current API version, e.g. `2025-04-01-preview`

## 4. Google Sheets setup (OAuth Desktop-app flow)

Google now blocks service-account **key** creation by default
(`iam.disableServiceAccountKeyCreation`), and personal Gmail accounts have no
org admin to override it. Workload Identity Federation doesn't fit a
personal-account, single-user tool either. Instead, this app authenticates
**as you** via the OAuth 2.0 Desktop/Installed-App flow, which needs no key
and no sheet sharing.

1. Go to https://console.cloud.google.com -> create or select a project.
2. **APIs & Services -> Library** -> enable **Google Sheets API**.
3. **APIs & Services -> OAuth consent screen**:
   - User type: **External**
   - Add your own Google account under **Test users**
   - (No verification is required while the app stays in "Testing" mode)
4. **APIs & Services -> Credentials -> Create credentials -> OAuth client ID**:
   - Application type: **Desktop app**
   - Download the JSON and save it as `credentials/oauth_client.json`
5. Open your Google Sheet, and from its URL:
   `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`, copy the
   `<SPREADSHEET_ID>` into `GOOGLE_SPREADSHEET_ID`.
6. Make sure the sheet has these tabs/columns (exact header text matters for
   the `Roster` lookup, but not for `Report Log` appends since we write by
   position):
   - `Roster` tab, columns A-D: `Name`, `Location`, `Current Team/Product`, `email`
   - `Report Log` tab, columns B-J: `ISO Year-Week`, `Employee Name`, `Team`,
     `Tasks Completed`, `Next Steps`, `Blockers/Risks`, `Blocker Status`,
     `Needs from Others`, `Demo Links`

The **first time you run the app**, it will open a browser window asking you
to sign in and grant access to Google Sheets. After you approve, a refresh
token is saved to `credentials/token.json` and every subsequent run is fully
non-interactive. If running on a headless server, run the app once on a
machine with a browser, then copy `credentials/token.json` to the server.

> Note: while the OAuth consent screen is in "Testing" mode, Google may expire
> the refresh token after ~7 days of inactivity. If the app logs an
> authorization error, just delete `credentials/token.json` and re-run once to
> re-consent. You can avoid this by publishing the app on the consent screen.

## 5. Install and configure

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

copy .env.example .env        # then fill in the values described above
mkdir credentials             # place oauth_client.json here
```

## 6. Run

```bash
python app.py
```

The app connects over Socket Mode (no public URL/firewall rule needed) and
starts listening. Forward a report DM into the configured private channel to
test it end-to-end; the bot will reply in-thread with the parsed result.

### Supported report inputs

- Forwarded or pasted Slack message text.
- `.md` / `.markdown` files. The file is downloaded with the bot token and its
  Markdown formatting is preserved through extraction, then rendered as rich
  text in Google Sheets.
- `.xlsx` Excel workbooks (the common misspelling `.xslx` is also accepted).
  Every non-empty worksheet is read, and GPT returns one separate report per
  employee found across the workbook.
- Images and other non-report attachments remain demo/screenshot links.

Report files are limited to 15 MB, 50,000 populated workbook cells, and
250,000 extracted characters to prevent accidentally sending an excessively
large workbook to the LLM. Legacy binary `.xls` files are not supported; save
them as `.xlsx` first.

### Windows: double-click launcher

`run_app.bat` lets you start the app without opening a terminal or activating
the virtual environment yourself. Just double-click it (or run it from
Explorer/PowerShell). It will:

- `cd` into the project folder automatically (works no matter where it's
  launched from).
- Create `.venv` and install `requirements.txt` automatically on first run,
  if `.venv` doesn't exist yet.
- Warn you if `.env` is missing instead of failing silently.
- Run the app using the venv's Python directly (`.venv\Scripts\python.exe`),
  so activation is never required.
- Keep the window open (`pause`) after the app exits or errors, so you can
  read any error output (including the one-time Google consent step) before
  it closes.

Closing the window stops the app.

## Project layout

- `config.py` - loads and validates all configuration from `.env`
- `models.py` - `WeeklyReport` pydantic schema (the canonical extraction shape)
- `llm.py` - Azure OpenAI client + single/batch structured-output extraction
- `file_ingest.py` - securely downloads and parses Slack `.md`/`.xlsx` files
- `text_cleanup.py` - removes emojis without changing wording or line breaks
- `markdown_format.py` - converts markdown/mrkdwn (`*bold*`, `_italic_`, `# Heading`, ...)
  into plain text + Google Sheets rich-text formatting
- `sheets.py` - Google Sheets OAuth client, Roster lookup, Report Log append (incl.
  applying rich-text formatting from `markdown_format.py`)
- `slack_handler.py` - pulls report text/files/links out of a Slack message
- `app.py` - Slack Bolt app (Socket Mode) wiring everything together
- `run_app.bat` - Windows double-click launcher (see above)

## Formatting in Report Log cells

The LLM is instructed to preserve a report's original wording and formatting
markers verbatim (so it won't summarize or reformat your staff's writing).
Decorative emojis are removed deterministically after extraction, including
from section titles, while line breaks and Markdown markers remain intact.
Before writing to the sheet, `markdown_format.py` then converts common
markdown/Slack-mrkdwn syntax into actual Google Sheets rich text rather than
leaving literal markup characters in the cell:

- `**bold**` or `*bold*` -> **bold**
- `_italic_` -> *italic*
- `~~strike~~` or `~strike~` -> strikethrough
- `` `code` `` -> monospace font
- `# Heading` (through `######`) -> a bold line, with the `#`s removed
- `- `/`* `/`+ ` bullet lines -> normalized to `- ` bullet lines

This only issues extra Google Sheets API calls for cells that actually contain
markdown; plain-text reports are unaffected.
