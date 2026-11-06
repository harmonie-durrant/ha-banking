# HA Banking

A Home Assistant custom integration for tracking your bank account balance.

Requires Home Assistant **2026.11.0** or later.

## Installation

### Marketplace (custom repository)

1. In Home Assistant, go to **Settings → Marketplace** → ⋮ → **Custom repositories**.
2. Add `https://github.com/harmonie-durrant/ha-banking` with category **Integration**.
3. Find **HA Banking** in the Marketplace, install it and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and search for **HA Banking**.

### Manual

Copy `custom_components/ha_banking` into your Home Assistant `config/custom_components/` directory and restart.

## Setup (Enable Banking)

This integration uses [Enable Banking](https://enablebanking.com), which is free for personal use
in **restricted production** mode (only accounts you link yourself).

1. Create an account on the [Enable Banking control panel](https://enablebanking.com/cp/applications).
2. Register a new application in **Production** environment:
   - Redirect URL: `https://enablebanking.com/` (or any URL you like, it just has to match).
   - Keep the generated private key `.pem` file.
3. In the control panel, **link your accounts** to activate the application in restricted mode.
4. In Home Assistant, add the **HA Banking** integration and fill in:
   - Application ID
   - Private key (paste the full `.pem` content)
   - Redirect URL (the one registered above)
   - Country (e.g. `FR`)
5. Pick your bank, open the authorization link, approve access at your bank, then paste the
   full URL you land on back into Home Assistant.

After authorizing, you choose which accounts to track; each account's current balance is shown to
help you choose. Card accounts showing 0 are marked ⚠ and not selected by default: immediate-debit
cards always report a balance of 0 (spending comes straight out of the current account).

One device is created per tracked account, each with a **Balance** sensor, an **Access expires**
sensor and a **Refresh** button.

### Options

Open the integration in **Settings → Devices & services**, then **Configure**:

- **Tracked accounts**: untick an account to remove it (with its entities). Balances are shown for
  tracked accounts; to see every account's balance, use **Reconfigure** with
  **Authorize bank access again**.
- **Refresh interval** (default 6 h, 1–24 h). Banks usually allow only ~4 automatic requests per
  day, so going below 6 h may get requests refused.
- **Main balance**: which balance type the Balance sensor shows. **Automatic** picks, in order,
  `CLBD` (booked), `ITBD`, `XPCD`, `ITAV`, `CLAV`, `OPBD`, `OPAV`. The types your bank actually
  returns are listed in the sensor's `available_balance_types` attribute.
- **Import history** (on by default): see below.

### Balance history

Right after you authorize access at your bank (first setup, renewal, or **Reconfigure** with
**Authorize bank access again**), the integration fetches up to 6 months of booked transactions
and rebuilds one end-of-day balance per day, starting from the current booked balance. These are
added to the Balance sensor's long-term statistics, so the history graph shows them as if they had
been recorded all along.

- It runs once per authorization, in the background. Many banks only return more than 90 days of
  transactions shortly after you log in; if 6 months is refused, 89 days are requested instead.
- Only days before anything Home Assistant has already recorded are imported, so existing history
  is never overwritten.
- It's a daily value rebuilt from booked transactions, so it can differ slightly from what your
  bank showed at the time (pending operations, booking vs value date). The history starts at the
  oldest transaction your bank returned.
- Already set up before this feature? Use **Reconfigure** with **Authorize bank access again**
  ticked to import your history.

### Notes

- The last balances are saved, so restarting or reloading Home Assistant (or changing options)
  doesn't call the bank again while the data is younger than the refresh interval. If the bank's
  rate limit is hit anyway, the last known balance stays displayed until the next refresh.
- The **Refresh** button fetches all accounts of that bank right away, ignoring the saved data.
  Each press counts toward the bank's daily limit.
- The Balance sensor's `balance_type` attribute shows which balance is used, and `fetched_at` when
  it was retrieved.
- Bank access expires (typically 90–180 days). Each account has an **Access expires** sensor.
  A week before expiry, Home Assistant asks you to re-authenticate (**Settings → Devices & services**):
  approve access at your bank again and paste the URL back. Your entities and history are kept.

### Changing credentials or adding a missing account

Open the integration in **Settings → Devices & services**, then **⋮ → Reconfigure** to change the
application ID, private key or redirect URL. Tick **Authorize bank access again** to log in at your
bank again: this also refreshes the list of accounts. Entities and history are kept.

## Troubleshooting

### An account is missing (e.g. livret a / savings account)

Banks only have to expose *payment* accounts through open banking (PSD2/DSP2), so savings accounts
such as Livret A, LEP or LDDS are often not returned at all. Some banks also let you pick which
accounts to share on their consent screen.

To see what your bank actually returns, enable debug logging, then **Reconfigure** with
**Authorize bank access again** ticked (or add the integration again):

```yaml
logger:
  logs:
    custom_components.ha_banking: debug
```

The log then lists every account returned by the bank (`Adding account: …`), and warns about
accounts returned without permission to read them (`Skipping account that … does not allow
querying`). An account missing from both lists was not returned by the bank.

## Development

### Local test environment

Run a local Home Assistant with the integration loaded straight from this repo:

```bash
scripts/setup     # once: creates .venv with Home Assistant (requirements_dev.txt)
scripts/develop   # starts Home Assistant on http://localhost:8123 using ./config
```

Restart Home Assistant (Ctrl+C then `scripts/develop`, or **Settings → System → Restart**) to pick up code changes.
Only `config/configuration.yaml` is committed; everything else in `config/` is local state.

When reaching Home Assistant through a forwarded port (GitHub Codespaces, VS Code) you may get
`400: Bad Request`. In **Settings → System → Network**, enable **Use X-Forwarded-For** and add
`127.0.0.1` and `::1` as trusted proxies, then restart.

### Tests

```bash
pip install -r requirements_test.txt
pytest
```

## License

MIT
