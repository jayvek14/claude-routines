#!/usr/bin/env python3
"""Daily PPC data pull for Jay's Daily PPC Report.

Read only. Pulls the allowlisted Google Ads accounts and writes one JSON file.
Credentials come from env vars (MK_*, DCL_*, GOOGLE_ADS_DEVELOPER_TOKEN) and are
never printed. Any account not in ACCOUNTS is never queried.

Usage: python3 ppc/daily_pull.py [output_path]
"""

import calendar
import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

from google.ads.googleads.client import GoogleAdsClient

# Allowlist. Nothing outside this list is ever touched or reported.
ACCOUNTS = [
    {"id": "8842482641", "name": "CW Limited", "mcc": "MK", "kpi": "ROAS"},
    {"id": "5665428180", "name": "London Building Supplies", "mcc": "MK", "kpi": "ROAS"},
    {"id": "5211757733", "name": "Wifi Hire", "mcc": "MK", "kpi": "ROAS"},
    {"id": "6676298096", "name": "WifiHire.COM (America)", "mcc": "MK", "kpi": "ROAS"},
    {"id": "8323356867", "name": "Bronwyn Conroy", "mcc": "MK", "kpi": "CPA"},
    {"id": "1070376006", "name": "The Lia Schorr Institute", "mcc": "MK", "kpi": "CPA"},
    {"id": "9661337492", "name": "The Ponsbourne", "mcc": "MK", "kpi": "CPA"},
    {"id": "5181522037", "name": "O'Neill Dental Care", "mcc": "DCL", "kpi": "CPA"},
]
ALLOWED_IDS = {a["id"] for a in ACCOUNTS}

METRICS = ("metrics.cost_micros, metrics.clicks, metrics.impressions, "
           "metrics.conversions_by_conversion_date, "
           "metrics.conversions_value_by_conversion_date")

DASHES = re.compile("[-‐‑‒–—―−]")
_SECRETS = []


def clean(s):
    """Strip dash characters and any credential values from text output."""
    s = str(s)
    for v in _SECRETS:
        s = s.replace(v, "***")
    return re.sub(r"\s+", " ", DASHES.sub(" ", s)).strip()


def d2s(d):
    return d.strftime("%Y/%m/%d")


def q(d):
    return d.strftime("%Y-%m-%d")


def client_for(prefix):
    keys = {
        "developer_token": "GOOGLE_ADS_DEVELOPER_TOKEN",
        "client_id": f"{prefix}_CLIENT_ID",
        "client_secret": f"{prefix}_CLIENT_SECRET",
        "refresh_token": f"{prefix}_REFRESH_TOKEN",
        "login_customer_id": f"{prefix}_LOGIN_CUSTOMER_ID",
    }
    cfg = {}
    for k, env in keys.items():
        val = os.environ.get(env)
        if not val:
            raise RuntimeError(f"missing env var {env}")
        cfg[k] = val.replace("-", "") if k == "login_customer_id" else val
        if k != "login_customer_id":
            _SECRETS.append(val)
    cfg["use_proto_plus"] = True
    return GoogleAdsClient.load_from_dict(cfg)


def metrics_dict(m):
    cost = m.cost_micros / 1e6
    conv = m.conversions_by_conversion_date
    val = m.conversions_value_by_conversion_date
    return {
        "cost": round(cost, 2),
        "clicks": m.clicks,
        "impressions": m.impressions,
        "conversions": round(conv, 2),
        "conversion_value": round(val, 2),
        "cpc": round(cost / m.clicks, 2) if m.clicks else None,
        "cpa": round(cost / conv, 2) if conv else None,
        "roas": round(val / cost, 2) if cost else None,
    }


def sum_rows(rows):
    tot = {"cost": 0.0, "clicks": 0, "impressions": 0, "conversions": 0.0, "conversion_value": 0.0}
    for r in rows:
        for k in tot:
            tot[k] += r[k]
    tot = {k: round(v, 2) if isinstance(v, float) else v for k, v in tot.items()}
    tot["cpc"] = round(tot["cost"] / tot["clicks"], 2) if tot["clicks"] else None
    tot["cpa"] = round(tot["cost"] / tot["conversions"], 2) if tot["conversions"] else None
    tot["roas"] = round(tot["conversion_value"] / tot["cost"], 2) if tot["cost"] else None
    return tot


def pull_account(svc, acct):
    cid = acct["id"]
    assert cid in ALLOWED_IDS
    search = lambda query: list(svc.search(customer_id=cid, query=query))
    out = {"id": cid, "name": acct["name"], "mcc": acct["mcc"], "kpi": acct["kpi"], "errors": []}

    def safe(label, fn):
        try:
            return fn()
        except Exception as e:  # keep going; report the section as failed
            msg = getattr(e, "failure", None) or e
            out["errors"].append(clean(f"{label}: {type(e).__name__}: {msg}")[:400])
            return None

    # Account info and dates in the account's own time zone.
    info = search("SELECT customer.descriptive_name, customer.currency_code, customer.time_zone, "
                  "customer.status, customer.manager FROM customer")[0].customer
    if info.manager:
        raise RuntimeError("allowlisted id is a manager account")
    tz = ZoneInfo(info.time_zone)
    today = dt.datetime.now(tz).date()
    y = today - dt.timedelta(days=1)
    dby = y - dt.timedelta(days=1)
    sdlw = y - dt.timedelta(days=7)
    l7_start = y - dt.timedelta(days=6)
    mtd_start = y.replace(day=1)
    days_in_month = calendar.monthrange(y.year, y.month)[1]
    out.update({
        "currency": info.currency_code,
        "time_zone": info.time_zone,
        "account_status": info.status.name,
        "dates": {"yesterday": d2s(y), "day_before": d2s(dby), "same_day_last_week": d2s(sdlw),
                  "last_7_days": f"{d2s(l7_start)} to {d2s(y)}", "month_to_date": f"{d2s(mtd_start)} to {d2s(y)}",
                  "days_elapsed_in_month": y.day, "days_in_month": days_in_month},
    })

    # Daily account totals for the last 35 days; periods are summed from these.
    start35 = y - dt.timedelta(days=34)
    daily = {}
    for r in search(f"SELECT segments.date, {METRICS} FROM customer "
                    f"WHERE segments.date BETWEEN '{q(start35)}' AND '{q(y)}'"):
        daily[r.segments.date] = metrics_dict(r.metrics)
    zero = metrics_dict(type("M", (), {"cost_micros": 0, "clicks": 0, "impressions": 0,
                                       "conversions_by_conversion_date": 0,
                                       "conversions_value_by_conversion_date": 0})())
    series = []
    d = start35
    while d <= y:
        series.append({"date": d2s(d), **daily.get(q(d), zero)})
        d += dt.timedelta(days=1)
    out["daily_last_35_days"] = series

    def period(a, b):
        return sum_rows([s for s in series if d2s(a) <= s["date"] <= d2s(b)])

    l7 = period(l7_start, y)
    prior28 = period(y - dt.timedelta(days=34), y - dt.timedelta(days=7))
    mtd = safe("mtd", lambda: metrics_dict(search(
        f"SELECT {METRICS} FROM customer WHERE segments.date BETWEEN '{q(mtd_start)}' AND '{q(y)}'")[0].metrics))
    out["periods"] = {
        "yesterday": period(y, y),
        "day_before": period(dby, dby),
        "same_day_last_week": period(sdlw, sdlw),
        "last_7_days": l7,
        "month_to_date": mtd,
        "prior_28_days_before_last_7": prior28,
    }
    yc = out["periods"]["yesterday"]["cpc"]
    l7c = l7["cpc"]
    out["cpc_change_vs_last_7_days_pct"] = round((yc / l7c - 1) * 100, 1) if yc and l7c else None
    l7_avg_cost = l7["cost"] / 7
    out["spend_yesterday_vs_l7_avg_pct"] = round(out["periods"]["yesterday"]["cost"] / l7_avg_cost * 100, 1) if l7_avg_cost else None

    # Recent changes per campaign (change_event allows up to 30 days back).
    changed = {}

    def changes():
        start = dt.datetime.now(tz) - dt.timedelta(days=7)
        rows = search("SELECT change_event.change_date_time, change_event.campaign, "
                      "change_event.change_resource_type, change_event.resource_change_operation "
                      "FROM change_event "
                      f"WHERE change_event.change_date_time >= '{start.strftime('%Y-%m-%d %H:%M:%S')}' "
                      f"AND change_event.change_date_time <= '{dt.datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}' "
                      "ORDER BY change_event.change_date_time DESC LIMIT 5000")
        for r in rows:
            c = r.change_event.campaign.split("/")[-1] if r.change_event.campaign else "account"
            e = changed.setdefault(c, {"count": 0, "last_change": None, "types": set()})
            e["count"] += 1
            e["types"].add(r.change_event.change_resource_type.name)
            e["last_change"] = e["last_change"] or r.change_event.change_date_time.replace("-", "/")
        return True
    safe("change_event", changes)

    # Serving campaigns with budgets, bidding, status reasons and metrics.
    def campaigns():
        base = ("campaign.id, campaign.name, campaign.status, campaign.serving_status, "
                "campaign.primary_status, campaign.primary_status_reasons, "
                "campaign.advertising_channel_type, campaign.bidding_strategy_type, "
                "campaign.bidding_strategy_system_status, campaign.bidding_strategy, "
                "campaign.start_date_time, campaign.target_cpa.target_cpa_micros, "
                "campaign.target_roas.target_roas, campaign.maximize_conversions.target_cpa_micros, "
                "campaign.maximize_conversion_value.target_roas, "
                "campaign_budget.id, campaign_budget.amount_micros, campaign_budget.explicitly_shared, "
                "campaign_budget.period")
        camps = {}
        for r in search(f"SELECT {base} FROM campaign WHERE campaign.status = 'ENABLED' "
                        "AND campaign.serving_status = 'SERVING'"):
            c = r.campaign
            tcpa = c.target_cpa.target_cpa_micros or c.maximize_conversions.target_cpa_micros
            troas = c.target_roas.target_roas or c.maximize_conversion_value.target_roas
            camps[str(c.id)] = {
                "id": str(c.id), "name": clean(c.name),
                "channel": c.advertising_channel_type.name,
                "primary_status": c.primary_status.name,
                "primary_status_reasons": [x.name for x in c.primary_status_reasons],
                "bidding_strategy_type": c.bidding_strategy_type.name,
                "bidding_strategy_system_status": c.bidding_strategy_system_status.name,
                "portfolio_bidding_strategy": bool(c.bidding_strategy),
                "learning": "LEARNING" in c.bidding_strategy_system_status.name,
                "start_date": c.start_date_time[:10].replace("-", "/") if c.start_date_time else None,
                "target_cpa": round(tcpa / 1e6, 2) if tcpa else None,
                "target_roas": round(troas, 2) if troas else None,
                "budget_id": str(r.campaign_budget.id),
                "daily_budget": round(r.campaign_budget.amount_micros / 1e6, 2),
                "budget_shared": r.campaign_budget.explicitly_shared,
                "budget_period": r.campaign_budget.period.name,
            }
        for label, a, b in (("yesterday", y, y), ("last_7_days", l7_start, y), ("month_to_date", mtd_start, y),
                            ("last_30_days", y - dt.timedelta(days=29), y)):
            extra = ", metrics.search_budget_lost_impression_share, metrics.search_rank_lost_impression_share" \
                if label == "last_7_days" else ""
            for r in search(f"SELECT campaign.id, {METRICS}{extra} FROM campaign "
                            f"WHERE campaign.status = 'ENABLED' AND segments.date BETWEEN '{q(a)}' AND '{q(b)}'"):
                cid_ = str(r.campaign.id)
                if cid_ in camps:
                    camps[cid_][label] = metrics_dict(r.metrics)
                    if extra:
                        bl = r.metrics.search_budget_lost_impression_share
                        rl = r.metrics.search_rank_lost_impression_share
                        camps[cid_]["search_budget_lost_is_7d"] = round(bl, 3) if bl else None
                        camps[cid_]["search_rank_lost_is_7d"] = round(rl, 3) if rl else None
        first_seen = {}
        for r in search("SELECT campaign.id, segments.date, metrics.impressions FROM campaign "
                        f"WHERE campaign.status = 'ENABLED' AND segments.date BETWEEN '{q(y - dt.timedelta(days=29))}' "
                        f"AND '{q(y)}' AND metrics.impressions > 0"):
            k = str(r.campaign.id)
            first_seen[k] = min(first_seen.get(k, r.segments.date), r.segments.date)
        for k, c in camps.items():
            ch = changed.get(k)
            c["changed_last_7_days"] = bool(ch)
            c["changes_last_7_days"] = {"count": ch["count"], "last_change": ch["last_change"],
                                        "types": sorted(ch["types"])} if ch else None
            c["days_with_data_in_last_30"] = (y - dt.date.fromisoformat(first_seen[k])).days + 1 if k in first_seen else 0
            c30 = c.get("last_30_days", {}).get("conversions", 0)
            c["guardrail_ok_for_proposals"] = (not c["changed_last_7_days"] and not c["learning"]
                                               and c["days_with_data_in_last_30"] >= 14 and c30 >= 30)
            c["limited_by_budget"] = ("BUDGET_CONSTRAINED" in c["primary_status_reasons"]
                                      or (c.get("search_budget_lost_is_7d") or 0) >= 0.10)
        return list(camps.values())
    camps = safe("campaigns", campaigns) or []
    out["serving_campaigns"] = camps
    out["campaigns_limited_by_budget"] = [
        {"id": c["id"], "name": c["name"], "daily_budget": c["daily_budget"],
         "search_budget_lost_is_7d": c.get("search_budget_lost_is_7d"),
         "reasons": c["primary_status_reasons"]} for c in camps if c["limited_by_budget"]]

    # Pacing against the sum of daily budgets of serving campaigns.
    budgets = {}
    for c in camps:
        if c["budget_period"] == "DAILY":
            budgets[c["budget_id"]] = c["daily_budget"]
    total_daily = round(sum(budgets.values()), 2)
    month_budget = total_daily * days_in_month
    mtd_cost = (mtd or {}).get("cost", 0)
    expected = total_daily * y.day
    projected = mtd_cost / y.day * days_in_month if y.day else 0
    out["pacing"] = {
        "total_daily_budget": total_daily,
        "implied_month_budget": round(month_budget, 2),
        "mtd_spend": mtd_cost,
        "mtd_vs_expected_pct": round(mtd_cost / expected * 100, 1) if expected else None,
        "projected_month_spend": round(projected, 2),
        "projected_vs_month_budget_pct": round(projected / month_budget * 100, 1) if month_budget else None,
        "note": "Budget basis is sum of current daily budgets of serving campaigns",
    }

    # Disapproved or limited ads in enabled ad groups of enabled campaigns.
    def ads():
        res = []
        for r in search("SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, "
                        "ad_group_ad.ad.type, ad_group_ad.policy_summary.approval_status, "
                        "ad_group_ad.policy_summary.review_status, ad_group_ad.policy_summary.policy_topic_entries "
                        "FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
                        "AND campaign.status = 'ENABLED' AND ad_group_ad.policy_summary.approval_status IN "
                        "('DISAPPROVED', 'APPROVED_LIMITED', 'AREA_OF_INTEREST_ONLY') LIMIT 200"):
            ps = r.ad_group_ad.policy_summary
            res.append({"campaign": clean(r.campaign.name), "campaign_id": str(r.campaign.id),
                        "ad_group": clean(r.ad_group.name), "ad_group_id": str(r.ad_group.id),
                        "ad_id": str(r.ad_group_ad.ad.id), "ad_type": r.ad_group_ad.ad.type_.name,
                        "approval_status": ps.approval_status.name, "review_status": ps.review_status.name,
                        "topics": sorted({clean(t.topic) for t in ps.policy_topic_entries})})
        return res
    out["disapproved_or_limited_ads"] = safe("ads", ads)

    # Enabled conversion actions with no conversions in the last 7 days.
    def conv_actions():
        acts = {}
        for r in search("SELECT conversion_action.resource_name, conversion_action.id, conversion_action.name, "
                        "conversion_action.category, conversion_action.primary_for_goal, conversion_action.type "
                        "FROM conversion_action WHERE conversion_action.status = 'ENABLED'"):
            ca = r.conversion_action
            acts[ca.resource_name] = {"id": str(ca.id), "name": clean(ca.name), "category": ca.category.name,
                                      "primary": ca.primary_for_goal, "type": ca.type_.name,
                                      "conv_7d": 0.0, "conv_30d": 0.0}
        for label, a in (("conv_7d", l7_start), ("conv_30d", y - dt.timedelta(days=29))):
            for r in search("SELECT segments.conversion_action, metrics.all_conversions FROM customer "
                            f"WHERE segments.date BETWEEN '{q(a)}' AND '{q(y)}'"):
                k = r.segments.conversion_action
                if k in acts:
                    acts[k][label] += r.metrics.all_conversions
        return [dict(v, conv_30d=round(v["conv_30d"], 2)) for v in acts.values() if v["conv_7d"] == 0]
    out["conversion_actions_zero_7d"] = safe("conversion_actions", conv_actions)

    # Billing and account level problems.
    def billing():
        problems = []
        if info.status.name != "ENABLED":
            problems.append(f"Account status is {info.status.name}")
        setups = search("SELECT billing_setup.id, billing_setup.status FROM billing_setup")
        statuses = [s.billing_setup.status.name for s in setups]
        if "APPROVED" not in statuses:
            problems.append(f"No approved billing setup (statuses: {', '.join(statuses) or 'none'})")
        budgets_ = []
        for r in search("SELECT account_budget.id, account_budget.status, account_budget.approved_spending_limit_micros, "
                        "account_budget.approved_spending_limit_type, account_budget.amount_served_micros, "
                        "account_budget.approved_start_date_time, account_budget.approved_end_date_time, "
                        "account_budget.approved_end_time_type "
                        "FROM account_budget WHERE account_budget.status = 'APPROVED'"):
            ab = r.account_budget
            now_s = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
            if ab.approved_start_date_time > now_s or (ab.approved_end_date_time and ab.approved_end_date_time < now_s):
                continue  # not the budget in force today
            limit = ab.approved_spending_limit_micros / 1e6 if ab.approved_spending_limit_micros else None
            served = ab.amount_served_micros / 1e6
            end = ab.approved_end_date_time or ab.approved_end_time_type.name
            remaining = round(limit - served, 2) if limit is not None else None
            budgets_.append({"id": str(ab.id), "limit": limit, "served": round(served, 2),
                             "remaining": remaining, "end": clean(str(end).replace("-", "/"))})
            if remaining is not None and l7_avg_cost and remaining < l7_avg_cost * 5:
                problems.append(f"Account budget {ab.id} has {remaining} left, under 5 days of spend")
        return {"problems": problems, "billing_setup_statuses": statuses, "account_budgets": budgets_}
    out["billing_and_status"] = safe("billing", billing)
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_pull.json")
    clients, result = {}, {"generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y/%m/%d %H:%M:%S"),
                           "accounts": [], "errors": []}
    for acct in ACCOUNTS:
        try:
            if acct["mcc"] not in clients:
                clients[acct["mcc"]] = client_for(acct["mcc"]).get_service("GoogleAdsService")
            result["accounts"].append(pull_account(clients[acct["mcc"]], acct))
        except Exception as e:
            msg = getattr(e, "failure", None) or e
            result["errors"].append(clean(f"{acct['name']} {acct['id']}: {type(e).__name__}: {msg}")[:500])
    with open(path, "w") as f:
        json.dump(result, f, indent=1, default=str)
    print(f"wrote {path}: {len(result['accounts'])} accounts, {len(result['errors'])} account errors, "
          f"{sum(len(a['errors']) for a in result['accounts'])} section errors")


if __name__ == "__main__":
    main()
