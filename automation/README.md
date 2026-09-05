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

## Store-goal restructure — 2026-08-07 (user-approved)

Root cause found in `campaign_conversion_goal`: only the two
PHONE_CALL_LEAD goals were `biddable`. The GET_DIRECTIONS and CONTACT
goals — the store-intent signals — were observation-only, so smart
bidding never optimized toward getting anyone directions to the store.

**Applied** via `campaignConversionGoals:mutate` (both confirmed 200):

- `23594873021~GET_DIRECTIONS~GOOGLE_HOSTED` → `biddable: true`
- `23594873021~CONTACT~GOOGLE_HOSTED` → `biddable: true`

Campaign now bids toward calls + directions + contact together —
i.e. store goals. Remaining follow-ups (pending tool approval in the
session): re-verify goal state, audit asset-group final URLs, and
monitor whether Maps/local serving picks up over the next 1–2 weeks
(`plan` run: watch the Spend-by-network section for CONTENT share
falling and local actions starting to record).

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

## Two-week re-audit — 2026-08-21 (last 14 days vs 2026-08-07 baseline)

Scheduled check-in results for campaign 23594873021:

| Metric | Baseline (30d, pre-change) | Re-audit (14d, post-change) |
| --- | --- | --- |
| Display/CONTENT spend share | ~27% (~$99/30d) | **$0 — eliminated** |
| Search spend | $258/30d | $284/14d (full $20/day now on Search) |
| Search impression share | 16.7% | **27.2%** |
| IS lost to budget | 4.7% | **23.3% — budget now the binding constraint** |
| Primary conversions | 0 | **1** (Business profile - Tracked call) |
| Ad-attributed directions | 0 | 0 (still) |

Config verified intact: Maximize Conversions with no tCPA; PHONE_CALL_LEAD,
GET_DIRECTIONS (GOOGLE_HOSTED), and CONTACT (GOOGLE_HOSTED) goals all
`biddable: true`.

Verdict: the restructure is working — display bleed is gone, search reach
is up ~63%, and the first primary conversion has recorded. The dedicated
local-campaign escalation is NOT triggered (Display 0% < 25% threshold).
The automation's budget rule now fires instead: 23.3% budget-lost IS >
10% threshold → proposed +20% step, $20/day → $24/day (pending Joshua's
approval). Directions conversions remain the open gap to watch at the
next check.

### Budget step applied — 2026-08-21 (user-approved)

Per the +20% guardrail (23.3% budget-lost IS > 10% threshold), raised
campaignBudgets/15385537187 from $20.00/day to **$24.00/day** via
`campaignBudgets:mutate`; re-read at apply time and verified after
(amountMicros 24000000). Next check: confirm budget-lost IS falls,
watch for first ad-attributed directions conversions.

## Post-budget-step audit — 2026-09-05 (last 14 days: Aug 22 – Sep 4)

Config intact: $24/day budget, Maximize Conversions, no tCPA.

| Metric | Aug 21 re-audit | Sep 5 audit |
| --- | --- | --- |
| Search impression share | 27.2% | **31.0%** |
| IS lost to budget | 23.3% | 22.2% (flat) |
| Avg daily spend | ~$20.3 (at cap) | ~$21.2 (below $24 cap) |
| Search share of spend | ~99.8% | ~98% (Display still ~$0) |
| Primary conversions (14d) | 1 | **0** |
| All conversions (14d) | 2 | 1 (Business profile - Call) |
| Ad-attributed directions | 0 | 0 |

Read: the structural wins hold (no display bleed, reach still climbing),
but the +20% budget step is NOT fully consumed (avg $21.2 < $24 cap;
budget-lost IS is peak-hour pacing, not day-level starvation) and primary
conversions fell back to zero. The >10% budget rule technically fires
again, but raising spend while measurable conversions are ~1 per
fortnight is not justified — the binding constraint is measurement, not
reach. Recommendation recorded: HOLD budget at $24/day; next lever is
conversion measurement (Store Visits eligibility, offline conversion
import of app/online orders, or a dedicated local campaign), not budget.
