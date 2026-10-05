# managenicvisions

Business tools for NicVisions Photography.

## Show monitor

Every morning, GitHub checks the [MyCompPrep competition calendar](https://mycompprep.com/competition-calendar/)
for bodybuilding shows in South Australia or Victoria. When a new one is
listed, it opens an issue in this repo labelled **show-alert**, and GitHub
emails you about it.

- **First run:** one issue lists every SA/VIC show already on the calendar.
  After that you only hear about new ones.
- **If something breaks** (the site is down, blocks the check, or changes its
  layout), one issue labelled **show-monitor-error** opens. Close it once it's
  sorted.
- **Run it now:** Actions tab → *Show monitor* → *Run workflow*.
- **Not getting emails?** Make sure you're watching this repo (Watch button →
  *All Activity*) and that email is on under GitHub Settings → Notifications.

The code is in `scripts/show_monitor.py`; the schedule is in
`.github/workflows/show-monitor.yml`.
