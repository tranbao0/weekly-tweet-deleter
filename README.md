# weekly-tweet-deleter

Deletes your old X posts once a week.
Each week, it deletes your posts, quote posts, and replies that are older than 30 days, and it undoes your reposts that are older than 30 days.
It keeps your pinned post.
It never touches likes or posts by other people.

It runs on Windows, in your default browser, with a browser profile of its own.
Supported browsers are Chrome, Edge, Brave, Opera, Opera GX, Vivaldi, and Chromium.
If your default browser is not one of them (for example Firefox), the script uses Microsoft Edge.

## How it works

The script starts your browser with its own profile folder, `browser-profile`, next to the script.
You log in to X in that window once, during setup, and the login stays in that folder.
Your normal browser profile is not used, and your normal browser windows can stay open during a run.

A run reads your Posts, Replies, and Reposts tabs first, and changes nothing during that pass.
Then it removes only the items that the first pass found to be old.
Before each removal, it checks that item again: it must be yours, not pinned, and older than the limit.

A repost shows the date of the original post, not the date of your repost.
So the script counts a repost's age from the first day it saw that repost.
On the first live run only, a repost of a post that is already older than 30 days counts as old.

The script stops and removes nothing more when:

- X shows a login page or a challenge.
- X shows its pages in a language other than English.
- The browser is logged in to an account other than the saved one.
- You use the mouse or keyboard in the script's tab during a run.

While a run is active, the browser has a debug port open on `127.0.0.1`, so programs on this PC can control that browser window.
The script closes the browser when the run ends.
If you open a tab in that window during a run, the script leaves the window open, and the port stays open until you close the window.
When the window stays open, the script shows a Windows notification that tells you to close the browser.
It also writes that message into its own tab, but only when that tab is not in use.
If the tab may be in use, you get the Windows notification only.

## Requirements

- Windows 10 or 11
- Python 3.12 or later, with the `py` launcher
- A supported browser (Microsoft Edge is always present on Windows)
- An X account with the X display language set to English

## Setup

1. Download this repository and put the folder on your Windows drive, for example in `%USERPROFILE%\weekly-tweet-deleter`.
2. Open PowerShell in that folder.
3. Install the dependency:

   ```powershell
   py -3 -m pip install --user -r requirements.txt
   ```

4. Run setup:

   ```powershell
   py -3 tweet_cleanup.py setup --handle your_handle
   ```

   A browser window opens.
   Log in to X in that window.
   Setup then saves your handle and your pinned post, and closes the window.
   If the window stays open, close it.
   If the logged-in account is not `your_handle`, setup stops and saves nothing.
   Without `--handle`, setup saves the handle of the account that you log in to.
5. Do a dry run, then read `logs\removed.csv`:

   ```powershell
   py -3 tweet_cleanup.py run
   ```

   Each row with the status `dry_run` is an item that a live run would remove.
6. When the list is correct, do the first live run:

   ```powershell
   py -3 tweet_cleanup.py run --live
   ```

7. Register the weekly task:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\register_task.ps1
   ```

## Usage

| Command | What it does |
| --- | --- |
| `py -3 tweet_cleanup.py setup` | Opens the browser for the X login, then saves the handle of that account |
| `py -3 tweet_cleanup.py setup --handle NAME` | The same, but stops if the logged-in account is not `NAME` |
| `py -3 tweet_cleanup.py run` | Dry run: lists what it would remove and changes nothing |
| `py -3 tweet_cleanup.py run --live` | Removes the old items |

Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--days N` | `30` | Removes items older than N days |
| `--max N` | `200` | Removes at most N items per day; the next runs remove the rest |
| `--browser PATH` | Windows default browser | Path to the exe of a Chromium browser |

A live run waits 2 to 5 seconds between removals.
After one live run finishes without errors on a day, later live runs on the same day exit at once.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Done, or already done today |
| `1` | Stopped or failed; the reason is in `logs\run.log` |
| `3` | The script's browser window was already open, so the run was skipped |

## Weekly task

`register_task.ps1` registers the Windows task `TweetCleanup`.
The task runs `py -3 tweet_cleanup.py run --live` once a week, on Sunday at 12:00.
If the run fails or is skipped, the task tries again every hour for 12 hours.
The task runs only while you are logged on to Windows, and it also runs on battery.
If the PC is off at the start time, the task runs when the PC comes back on.

| Command | What it does |
| --- | --- |
| `.\register_task.ps1 -Time 23:59` | Registers the task with another start time |
| `.\register_task.ps1 -Day Friday` | Registers the task on another day of the week |
| `.\register_task.ps1 -DryRun` | Prints the task settings and registers nothing |
| `.\register_task.ps1 -Unregister` | Removes the task |

## Files

| File | Content |
| --- | --- |
| `browser-profile\` | The script's browser profile, with your X login |
| `state.json` | Saved handle, pinned post ID, repost first-seen dates, last live run date |
| `logs\removed.csv` | One row per item: `dry_run` in a dry run; in a live run, a `pending` row before and a result row after each removal |
| `logs\run.log` | Run log |

Keep `browser-profile\` private: it holds your X login.
To log out the script, delete that folder.

## Tests

```bash
uv run --no-project --with pytest --with playwright==1.65.2 python -m pytest -q
```

The tests run against a local copy of the X timeline in `tests/fixtures` and never open x.com.
`tests/test_browser_smoke.py` runs only on Windows, and it uses a temporary browser profile.

## License

MIT
