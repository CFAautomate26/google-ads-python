# Conversion-spend maximizer automation

Automation for the CFA London Google Ads account (customer `9053151764`,
connected as `marketing.cfalondon@gmail.com`). It audits the account for
conditions that stop smart bidding from maximizing conversions, prints a
bounded change plan, and — only with explicit opt-in flags — applies it.

## Live audit findings — 2026-08-07 (last 30 days)

One enabled campaign: **CFA London-Search** (Performance Max, Maximize
Conversions bidding).

| Metric | Value |
| --- | --- |
| Spend | $362.05 |
| Clicks / impressions | 932 / 44,116 (avg CPC $0.39) |
| Primary conversions | **0** |
| All conversions | 3 (all from "Business profile - Call") |
| Daily budget | $20.00 |
| Target CPA constraint | **$111.00** |
| Search impression share | 16.7% |
| Impression share lost to budget | 4.7% |

### What this means

1. **Smart bidding has no signal.** The three primary conversion actions
   (Calls from ads, Local actions - Directions, Clicks to call) recorded
   zero conversions; the only conversions that fired ("Business profile -
   Call") are not primary, so `metrics.conversions` is 0 and Maximize
   Conversions is optimizing blind. Fixing which action is primary is the
   single highest-impact change and is deliberately left as a manual
   decision (see `flag_conversion_signal` proposals).
2. **The $111 target CPA throttles delivery for nothing.** A tCPA
   constraint needs conversion volume (≥15/30d as a rule of thumb) to
   steer bidding; with zero primary conversions it only suppresses spend.
   The automation proposes removing it (`remove_tcpa`, opt-in).
3. **Budget is not the bottleneck.** Only 4.7% of impression share is lost
   to budget (~78% is lost to ad rank), so raising the $20/day budget is
   NOT currently proposed — the budget rule only fires above 10% lost IS.

## Local-conversion diagnosis — 2026-08-07

Applied same day (user-approved): **removed the $111 target CPA** on
CFA London-Search; the campaign now runs unconstrained Maximize
Conversions. Verified via follow-up query.

Why "Local actions - Directions" / "Clicks to call" never fire:

- **Location plumbing is fine.** A `LOCATION_SYNC` asset set (Business
  Profile store #30029) is linked at the customer level, and the campaign
  carries CALL, LOGO, BUSINESS_NAME, and 6 SITELINK assets.
- **The spend goes to the wrong surfaces.** Last-30d network split:
  Search $257.64 (493 clicks, 5.3k imp), **Display/Content $99.35
  (435 clicks, 37.7k imp)**, YouTube $4.78, partners negligible. ~27% of
  budget buys display clicks with near-zero visit intent, and the
  campaign shows no meaningful Maps/local serving — 0 ad-attributed
  local actions in 12 months.
- **GBP interactions do happen organically**: trailing 12 months show
  Business profile - Call 16, Tracked call 3, Directions 1, Calls from
  ads 2 — all system-managed actions that can't be used as primary
  bidding signals via the API.
- Geo targeting is correct: 15 km proximity around the store.

Recommended next moves (not yet applied): re-shape the PMax campaign
around store-visit/local goals (or a dedicated local campaign) so Maps
and local surfaces get the budget that display burns today, and pursue
Store Visits conversion eligibility with Google.

## Usage

Requires a configured `google-ads.yaml` (developer token, OAuth
credentials, `login_customer_id`) — see the repository README.

```bash
# Read-only audit
python automation/conversion_spend_maximizer.py audit -c 9053151764

# Show the change plan without touching anything
python automation/conversion_spend_maximizer.py plan -c 9053151764

# Apply — requires per-change opt-in AND --confirm
python automation/conversion_spend_maximizer.py apply -c 9053151764 \
    --apply-tcpa --confirm
```

## Guardrails

- `audit` and `plan` never mutate; `apply` without `--confirm` is a no-op.
- Each mutation class needs its own opt-in flag (`--apply-tcpa`,
  `--apply-budget`).
- Budget increases are proposed only when >10% of impression share is lost
  to budget, and each step is capped at +20%, re-read at apply time.
- Conversion-goal (primary/secondary) changes are never auto-applied —
  they are flagged for a human because they change what "a conversion"
  means for billing-relevant optimization.

## Scheduling

Run `plan` on a daily/weekly schedule and review the output; run `apply`
manually after reviewing. The script exits non-zero on API errors so it is
cron/CI friendly.
