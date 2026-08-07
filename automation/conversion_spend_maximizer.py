#!/usr/bin/env python
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Conversion-spend maximizer automation.

Audits a Google Ads account for conditions that prevent smart bidding from
maximizing conversions, produces a prioritized change plan, and (only with
--apply --confirm) executes the plan through the Google Ads API.

Subcommands:
  audit   Read-only. Prints campaign spend, conversion signal health, and
          impression-share diagnostics.
  plan    Read-only. Prints the concrete changes the automation would make,
          with the guardrails that bound each one.
  apply   Mutates the account. Requires BOTH --apply-tcpa/--apply-budget style
          opt-in flags AND --confirm; otherwise it behaves like `plan`.

Run with a configured google-ads.yaml (see repository README) or pass
--credentials-path.

Usage:
  python conversion_spend_maximizer.py audit -c 9053151764
  python conversion_spend_maximizer.py plan  -c 9053151764
  python conversion_spend_maximizer.py apply -c 9053151764 \
      --apply-tcpa --confirm
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# Guardrails. Every mutation the automation is capable of is bounded by these.
# A conversion count below MIN_CONVERSIONS_FOR_TCPA means the account has too
# little signal for a target-CPA constraint to do anything but throttle spend.
MIN_CONVERSIONS_FOR_TCPA = 15
# Budget increases are only proposed when budget-lost impression share exceeds
# this fraction, and each step is capped at MAX_BUDGET_STEP_PCT.
BUDGET_LOST_IS_THRESHOLD = 0.10
MAX_BUDGET_STEP_PCT = 0.20
LOOKBACK = "LAST_30_DAYS"


@dataclass
class Proposal:
    """A single bounded change the automation wants to make."""

    kind: str  # "remove_tcpa" | "raise_budget" | "flag_conversion_signal"
    campaign_id: str
    campaign_name: str
    detail: str
    mutable: bool  # False = surfaced for a human, never auto-applied


@dataclass
class AccountSnapshot:
    campaigns: list = field(default_factory=list)
    conversion_actions: list = field(default_factory=list)
    conversion_breakdown: list = field(default_factory=list)


def fetch_snapshot(client, customer_id):
    """Reads everything the audit needs in three GAQL queries."""
    ga_service = client.get_service("GoogleAdsService")
    snapshot = AccountSnapshot()

    campaign_query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.bidding_strategy_type,
          campaign.maximize_conversions.target_cpa_micros,
          campaign_budget.resource_name,
          campaign_budget.amount_micros,
          metrics.cost_micros,
          metrics.clicks,
          metrics.impressions,
          metrics.conversions,
          metrics.all_conversions,
          metrics.search_impression_share,
          metrics.search_budget_lost_impression_share
        FROM campaign
        WHERE campaign.status = 'ENABLED'
          AND segments.date DURING {LOOKBACK}
    """
    for row in ga_service.search(customer_id=customer_id, query=campaign_query):
        snapshot.campaigns.append(row)

    action_query = """
        SELECT
          conversion_action.id,
          conversion_action.name,
          conversion_action.type,
          conversion_action.category,
          conversion_action.primary_for_goal,
          conversion_action.counting_type
        FROM conversion_action
        WHERE conversion_action.status = 'ENABLED'
    """
    for row in ga_service.search(customer_id=customer_id, query=action_query):
        snapshot.conversion_actions.append(row)

    breakdown_query = f"""
        SELECT
          segments.conversion_action_name,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM campaign
        WHERE segments.date DURING {LOOKBACK}
          AND metrics.all_conversions > 0
    """
    for row in ga_service.search(
        customer_id=customer_id, query=breakdown_query
    ):
        snapshot.conversion_breakdown.append(row)

    return snapshot


def build_proposals(snapshot):
    """Turns the snapshot into bounded change proposals."""
    proposals = []
    firing_actions = {
        row.segments.conversion_action_name
        for row in snapshot.conversion_breakdown
    }
    primary_actions = {
        row.conversion_action.name
        for row in snapshot.conversion_actions
        if row.conversion_action.primary_for_goal
    }

    # Conversions recorded only by non-primary actions means bidding gets no
    # signal from the conversions that actually happen. This is a tracking
    # decision, so it is surfaced but never auto-applied.
    orphaned = firing_actions - primary_actions
    for row in snapshot.campaigns:
        campaign = row.campaign
        cid, cname = str(campaign.id), campaign.name

        if row.metrics.conversions == 0 and orphaned:
            proposals.append(
                Proposal(
                    kind="flag_conversion_signal",
                    campaign_id=cid,
                    campaign_name=cname,
                    detail=(
                        "0 primary conversions while these non-primary "
                        f"actions fired: {sorted(orphaned)}. Make the real "
                        "conversion action primary (or import the right "
                        "goal) so Maximize Conversions has a signal."
                    ),
                    mutable=False,
                )
            )

        tcpa = campaign.maximize_conversions.target_cpa_micros
        if tcpa and row.metrics.conversions < MIN_CONVERSIONS_FOR_TCPA:
            proposals.append(
                Proposal(
                    kind="remove_tcpa",
                    campaign_id=cid,
                    campaign_name=cname,
                    detail=(
                        f"Target CPA ${tcpa / 1e6:.2f} is set with only "
                        f"{row.metrics.conversions:.0f} primary conversions "
                        f"in 30d (< {MIN_CONVERSIONS_FOR_TCPA}). The cap "
                        "throttles delivery without enough signal to steer "
                        "it; remove it until conversion volume recovers."
                    ),
                    mutable=True,
                )
            )

        lost = row.metrics.search_budget_lost_impression_share
        if lost > BUDGET_LOST_IS_THRESHOLD:
            budget = row.campaign_budget.amount_micros
            new_budget = int(budget * (1 + MAX_BUDGET_STEP_PCT))
            proposals.append(
                Proposal(
                    kind="raise_budget",
                    campaign_id=cid,
                    campaign_name=cname,
                    detail=(
                        f"{lost:.0%} of impression share lost to budget; "
                        f"raise daily budget ${budget / 1e6:.2f} -> "
                        f"${new_budget / 1e6:.2f} "
                        f"(+{MAX_BUDGET_STEP_PCT:.0%} step cap)."
                    ),
                    mutable=True,
                )
            )

    return proposals


def print_audit(snapshot):
    logger.info("=== Account audit (%s) ===", LOOKBACK)
    for row in snapshot.campaigns:
        campaign, metrics = row.campaign, row.metrics
        tcpa = campaign.maximize_conversions.target_cpa_micros
        logger.info(
            "Campaign %s (%s / %s)\n"
            "  spend $%.2f | clicks %s | impressions %s\n"
            "  conversions %.1f (all: %.1f) | budget $%.2f/day%s\n"
            "  search IS %.1f%% | lost to budget %.1f%%",
            campaign.name,
            campaign.advertising_channel_type.name,
            campaign.bidding_strategy_type.name,
            metrics.cost_micros / 1e6,
            metrics.clicks,
            metrics.impressions,
            metrics.conversions,
            metrics.all_conversions,
            row.campaign_budget.amount_micros / 1e6,
            f" | tCPA ${tcpa / 1e6:.2f}" if tcpa else "",
            metrics.search_impression_share * 100,
            metrics.search_budget_lost_impression_share * 100,
        )
    logger.info("--- Conversion actions ---")
    for row in snapshot.conversion_actions:
        action = row.conversion_action
        logger.info(
            "  %s [%s/%s] primary=%s",
            action.name,
            action.type_.name,
            action.category.name,
            action.primary_for_goal,
        )
    logger.info("--- 30d conversions by action ---")
    if not snapshot.conversion_breakdown:
        logger.info("  (none recorded)")
    for row in snapshot.conversion_breakdown:
        logger.info(
            "  %s: %.1f (value %.2f)",
            row.segments.conversion_action_name,
            row.metrics.all_conversions,
            row.metrics.all_conversions_value,
        )


def apply_proposals(client, customer_id, proposals, apply_tcpa, apply_budget):
    """Executes opted-in, mutable proposals. Never touches anything else."""
    campaign_service = client.get_service("CampaignService")
    budget_service = client.get_service("CampaignBudgetService")

    for proposal in proposals:
        if not proposal.mutable:
            logger.info(
                "[manual] %s: %s", proposal.campaign_name, proposal.detail
            )
            continue

        if proposal.kind == "remove_tcpa" and apply_tcpa:
            operation = client.get_type("CampaignOperation")
            campaign = operation.update
            campaign.resource_name = campaign_service.campaign_path(
                customer_id, proposal.campaign_id
            )
            campaign.maximize_conversions.target_cpa_micros = 0
            client.copy_from(
                operation.update_mask,
                client.get_type("FieldMask")(
                    paths=["maximize_conversions.target_cpa_micros"]
                ),
            )
            campaign_service.mutate_campaigns(
                customer_id=customer_id, operations=[operation]
            )
            logger.info("[applied] %s", proposal.detail)
        elif proposal.kind == "raise_budget" and apply_budget:
            # Re-read the budget at apply time so the +20% step is computed
            # from the current value, not a stale snapshot.
            ga_service = client.get_service("GoogleAdsService")
            query = f"""
                SELECT campaign_budget.resource_name,
                       campaign_budget.amount_micros
                FROM campaign
                WHERE campaign.id = {proposal.campaign_id}
            """
            row = next(
                iter(ga_service.search(customer_id=customer_id, query=query))
            )
            operation = client.get_type("CampaignBudgetOperation")
            budget = operation.update
            budget.resource_name = row.campaign_budget.resource_name
            budget.amount_micros = int(
                row.campaign_budget.amount_micros * (1 + MAX_BUDGET_STEP_PCT)
            )
            client.copy_from(
                operation.update_mask,
                client.get_type("FieldMask")(paths=["amount_micros"]),
            )
            budget_service.mutate_campaign_budgets(
                customer_id=customer_id, operations=[operation]
            )
            logger.info("[applied] %s", proposal.detail)
        else:
            logger.info(
                "[skipped — not opted in] %s: %s",
                proposal.campaign_name,
                proposal.detail,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["audit", "plan", "apply"], help="What to do."
    )
    parser.add_argument(
        "-c", "--customer-id", required=True, help="Customer ID, digits only."
    )
    parser.add_argument(
        "--credentials-path",
        default=None,
        help="Path to google-ads.yaml (defaults to standard lookup).",
    )
    parser.add_argument(
        "--apply-tcpa",
        action="store_true",
        help="Opt in to removing under-supported target-CPA constraints.",
    )
    parser.add_argument(
        "--apply-budget",
        action="store_true",
        help="Opt in to bounded budget increases.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for apply to mutate anything.",
    )
    args = parser.parse_args()

    client = GoogleAdsClient.load_from_storage(args.credentials_path)

    try:
        snapshot = fetch_snapshot(client, args.customer_id)
        proposals = build_proposals(snapshot)

        if args.command == "audit":
            print_audit(snapshot)
            return

        logger.info("=== Change plan (%d proposals) ===", len(proposals))
        for proposal in proposals:
            tag = "auto" if proposal.mutable else "manual"
            logger.info(
                "[%s] %s: %s", tag, proposal.campaign_name, proposal.detail
            )

        if args.command == "apply":
            if not args.confirm:
                logger.info("--confirm not set; nothing was changed.")
                return
            apply_proposals(
                client,
                args.customer_id,
                proposals,
                apply_tcpa=args.apply_tcpa,
                apply_budget=args.apply_budget,
            )
    except GoogleAdsException as ex:
        for error in ex.failure.errors:
            logger.error("Request failed: %s", error.message)
        sys.exit(1)


if __name__ == "__main__":
    main()
