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

One device per account is created, each with a **Balance** sensor.

### Notes

- Balances are refreshed every 6 hours: banks usually allow only ~4 background requests per day.
  Each Home Assistant restart also triggers one refresh.
- The "main" balance is picked in this order: `CLBD` (booked), `ITBD`, `XPCD`, `ITAV`, `CLAV`, `OPBD`, `OPAV`.
  The sensor's `balance_type` attribute shows which one is used.
- Bank access expires (typically 90–180 days). Re-authentication is not implemented yet:
  delete and re-add the integration when the sensor becomes unavailable.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

## License

MIT
