# Arena Trading

A standalone app with Paper Trading and Live Trading tabs on one server. Both are **paper simulations**; Live Trading does not place real broker orders. The two engines have independent in-memory state, allocations, positions and trades. The Google Sheet output belongs to Live Trading. Saved symbol presets are shared.

## Start on Mac

Copy the **entire Arena folder**, not just the two engines or HTML files. It includes the existing strategy, data, indicator, and Google Sheets modules. In Terminal:

```sh
cd "/path/to/Arena"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
caffeinate -i python app.py
```

Open http://localhost:8020/ and use the tabs. If port 8020 is already in use, stop the old Paper Live server first, or run `ARENA_PORT=8030 caffeinate -i python app.py` and open port 8030. VS Code isn't required.

On first use, configure and save each tab's settings and allocations. The browser stores the tabs' settings separately. The new `Arena/data` folder starts empty: no old paper positions or allocations are automatically transferred. **Don't run old and new instances for the same trading account/strategy without checking their independent positions.**

## Google Sheet

The Live Trading tab is a simulation, not a broker connection. Install the requirements above; share your sheet with the Google service account as Editor. The old default credentials path is `../Json/automation-project-429417-c51140fdff86.json`, relative to the Arena folder. If your credentials are elsewhere, before starting the server set:

```sh
export ARENA_GOOGLE_CREDENTIALS="/absolute/path/to/service-account.json"
```

Enter the spreadsheet name on Live Trading, save settings, and use **Test write**. The positions tab is cleared and rewritten: use a dedicated tab. Keep credentials private and out of Git.

## Important limits

- The full scan runs in the server while started, but **fast LTP polling runs from the open browser page**. Switching tabs or closing the browser page stops that page's fast polling; the slower full scan continues. To use fast polling for both, keep each page open in its own browser tab. Browser background throttling may still delay it. This is existing behavior, not a 24/7 guarantee.
- Stop each engine from its own tab. Restarting the server does not automatically restart scanning. This app is not ready for unattended real trading.
- This server has no login. Do not expose it publicly. It uses Flask's development server; use only on a trusted network.
- Google Sheets output is best effort, not a broker-grade order ledger. Check the queue/status and sheet yourself. Don't use it for unattended real orders.
