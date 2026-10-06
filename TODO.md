# TODO

- spread.py:173 — surface upstream body like chat.py (low priority)
- halt watchdog: activate `python -m app.halt_watchdog --once` via cron/systemd
  (closes incident 2026-09-28 gap #4 *without* the PID-1840 restart). Command
  shipped + tested; needs Andre's OK to add the timer (approval-gated).
