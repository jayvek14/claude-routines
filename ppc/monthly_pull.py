#!/usr/bin/env python3
"""Monthly audit pull for Jay's Monthly Audit. Read only.

Per managed Google account: last full month vs the month before (account and
campaign), impression share, wasted spend (search terms and keywords with no
conversions), campaign settings (networks, location targeting, PMax and Demand
Gen asset automation, URL expansion), RSA and asset group ad strength,
Display and YouTube placements, conversion actions, and security (users,
pending invitations, manager links, product links).

Usage: python3 ppc/monthly_pull.py [output_path] [--accounts id,id,...]
"""

import calendar
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daily_pull import ACCOUNTS, METRICS, clean, client_for, d2s, metrics_dict, q  # noqa: E402
from weekly_pull import M_STD, NEVER_TOUCH, pct, std  # noqa: E402

KNOWN = {a["id"]: a for a in ACCOUNTS}


def month_range(year, month):
    return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])


def pull_account(svc, acct):
    cid = acct["id"]
    assert cid in KNOWN and cid not in NEVER_TOUCH
    search = lambda query: svc.search(customer_id=cid, query=query)
    out = {"id": cid, "name": acct["name"], "kpi": acct["kpi"], "errors": []}

    def safe(label, fn):
        try:
            return fn()
        except Exception as e:
            out["errors"].append(clean(f"{label}: {type(e).__name__}: {getattr(e, 'failure', None) or e}")[:400])
            return None

    info = list(search("SELECT customer.currency_code, customer.time_zone, customer.status, "
                       "customer.auto_tagging_enabled FROM customer"))[0].customer
    today = dt.datetime.now(ZoneInfo(info.time_zone)).date()
    first_this = today.replace(day=1)
    lm_end = first_this - dt.timedelta(days=1)
    lm = month_range(lm_end.year, lm_end.month)
    pm_end = lm[0] - dt.timedelta(days=1)
    pm = month_range(pm_end.year, pm_end.month)
    rng = lambda r: f"segments.date BETWEEN '{q(r[0])}' AND '{q(r[1])}'"
    out.update({"currency": info.currency_code, "account_status": info.status.name,
                "auto_tagging": info.auto_tagging_enabled,
                "months": {"last_month": f"{d2s(lm[0])} to {d2s(lm[1])}", "month_before": f"{d2s(pm[0])} to {d2s(pm[1])}",
                           "days_in_last_month": lm[1].day}})

    def totals():
        res = {}
        for label, r in (("last_month", lm), ("month_before", pm)):
            rows = list(search(f"SELECT {METRICS} FROM customer WHERE {rng(r)}"))
            res[label] = metrics_dict(rows[0].metrics) if rows else None
        a, b = res["last_month"] or {}, res["month_before"] or {}
        res["change_pct"] = {k: pct(a.get(k), b.get(k)) for k in ("cost", "conversions", "conversion_value", "cpa", "roas", "cpc")}
        return res
    out["totals"] = safe("totals", totals)

    def campaigns():
        camps = {}
        for label, r in (("last_month", lm), ("month_before", pm)):
            for row in search("SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
                              f"campaign.bidding_strategy_type, {METRICS}, metrics.search_impression_share, "
                              "metrics.search_budget_lost_impression_share, metrics.search_rank_lost_impression_share "
                              f"FROM campaign WHERE campaign.status != 'REMOVED' AND {rng(r)}"):
                c = camps.setdefault(str(row.campaign.id), {"id": str(row.campaign.id), "name": clean(row.campaign.name),
                                                            "status": row.campaign.status.name,
                                                            "channel": row.campaign.advertising_channel_type.name,
                                                            "bidding": row.campaign.bidding_strategy_type.name})
                m = row.metrics
                c[label] = dict(metrics_dict(m),
                                search_is=round(m.search_impression_share, 3) if m.search_impression_share else None,
                                lost_is_budget=round(m.search_budget_lost_impression_share, 3) if m.search_budget_lost_impression_share else None,
                                lost_is_rank=round(m.search_rank_lost_impression_share, 3) if m.search_rank_lost_impression_share else None)
        return sorted(camps.values(), key=lambda c: -((c.get("last_month") or {}).get("cost") or 0))
    out["campaigns"] = safe("campaigns", campaigns)

    def settings():
        res = []
        for row in search("SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
                          "campaign.network_settings.target_google_search, campaign.network_settings.target_search_network, "
                          "campaign.network_settings.target_content_network, campaign.network_settings.target_partner_search_network, "
                          "campaign.geo_target_type_setting.positive_geo_target_type, "
                          "campaign.asset_automation_settings, campaign.brand_guidelines_enabled "
                          "FROM campaign WHERE campaign.status = 'ENABLED'"):
            c = row.campaign
            res.append({"id": str(c.id), "name": clean(c.name), "channel": c.advertising_channel_type.name,
                        "search_partners": c.network_settings.target_search_network,
                        "display_network": c.network_settings.target_content_network,
                        "location_option": c.geo_target_type_setting.positive_geo_target_type.name,
                        "asset_automation": {s.asset_automation_type.name: s.asset_automation_status.name
                                             for s in c.asset_automation_settings}})
        return res
    out["campaign_settings"] = safe("settings", settings)

    def waste():
        terms = []
        for row in search(f"SELECT search_term_view.search_term, campaign.id, campaign.name, ad_group.id, {M_STD} "
                          f"FROM search_term_view WHERE {rng(lm)} AND metrics.conversions = 0 AND metrics.cost_micros > 0 "
                          "ORDER BY metrics.cost_micros DESC LIMIT 300"):
            terms.append(dict(std(row.metrics), term=clean(row.search_term_view.search_term),
                              campaign_id=str(row.campaign.id), campaign=clean(row.campaign.name), ad_group_id=str(row.ad_group.id)))
        kws = []
        for row in search("SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, campaign.name, "
                          f"ad_group.id, ad_group_criterion.criterion_id, {M_STD} FROM keyword_view "
                          f"WHERE {rng(lm)} AND metrics.conversions = 0 AND metrics.cost_micros > 0 "
                          "AND ad_group_criterion.status = 'ENABLED' ORDER BY metrics.cost_micros DESC LIMIT 100"):
            kws.append(dict(std(row.metrics), keyword=clean(row.ad_group_criterion.keyword.text),
                            match_type=row.ad_group_criterion.keyword.match_type.name, campaign=clean(row.campaign.name),
                            ad_group_id=str(row.ad_group.id), criterion_id=str(row.ad_group_criterion.criterion_id)))
        return {"zero_conv_search_term_cost": round(sum(t["cost"] for t in terms), 2), "top_zero_conv_terms": terms[:60],
                "zero_conv_keyword_cost": round(sum(k["cost"] for k in kws), 2), "top_zero_conv_keywords": kws[:40]}
    out["wasted_spend"] = safe("waste", waste)

    def ads():
        rsa = {}
        for row in search("SELECT campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad_strength, "
                          "ad_group_ad.policy_summary.approval_status FROM ad_group_ad "
                          "WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' AND ad_group_ad.status = 'ENABLED' "
                          "AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            s = row.ad_group_ad.ad_strength.name
            rsa[s] = rsa.get(s, 0) + 1
        groups = []
        for row in search("SELECT campaign.name, asset_group.id, asset_group.name, asset_group.ad_strength "
                          "FROM asset_group WHERE asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            groups.append({"campaign": clean(row.campaign.name), "asset_group_id": str(row.asset_group.id),
                           "asset_group": clean(row.asset_group.name), "ad_strength": row.asset_group.ad_strength.name})
        disapproved = [r.ad_group_ad.ad.id for r in search(
            "SELECT ad_group_ad.ad.id FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED' AND campaign.status = 'ENABLED' "
            "AND ad_group_ad.policy_summary.approval_status = 'DISAPPROVED'")]
        return {"rsa_ad_strength_counts": rsa, "asset_groups": groups, "disapproved_ads": len(disapproved)}
    out["ads_and_assets"] = safe("ads", ads)

    def placements():
        res = []
        for row in search("SELECT detail_placement_view.display_name, detail_placement_view.group_placement_target_url, "
                          f"detail_placement_view.placement_type, campaign.id, campaign.name, {M_STD} "
                          f"FROM detail_placement_view WHERE {rng(lm)} AND metrics.cost_micros > 0 "
                          "ORDER BY metrics.cost_micros DESC LIMIT 150"):
            p = row.detail_placement_view
            res.append(dict(std(row.metrics), placement=clean(p.display_name), url=p.group_placement_target_url,
                            type=p.placement_type.name, campaign_id=str(row.campaign.id), campaign=clean(row.campaign.name)))
        return res
    out["placements"] = safe("placements", placements)

    def conv_actions():
        acts = {}
        for row in search("SELECT conversion_action.resource_name, conversion_action.name, conversion_action.status, "
                          "conversion_action.primary_for_goal, conversion_action.type, conversion_action.category "
                          "FROM conversion_action WHERE conversion_action.status = 'ENABLED'"):
            ca = row.conversion_action
            acts[ca.resource_name] = {"name": clean(ca.name), "primary": ca.primary_for_goal, "type": ca.type_.name,
                                      "category": ca.category.name, "last_month": 0.0, "month_before": 0.0}
        for label, r in (("last_month", lm), ("month_before", pm)):
            for row in search(f"SELECT segments.conversion_action, metrics.all_conversions FROM customer WHERE {rng(r)}"):
                if row.segments.conversion_action in acts:
                    acts[row.segments.conversion_action][label] += round(row.metrics.all_conversions, 2)
        return list(acts.values())
    out["conversion_actions"] = safe("conversion_actions", conv_actions)

    def security():
        users = [{"email": r.customer_user_access.email_address, "role": r.customer_user_access.access_role.name,
                  "created": (r.customer_user_access.access_creation_date_time or "")[:10].replace("-", "/"),
                  "invited_by": r.customer_user_access.inviter_user_email_address}
                 for r in search("SELECT customer_user_access.email_address, customer_user_access.access_role, "
                                 "customer_user_access.access_creation_date_time, "
                                 "customer_user_access.inviter_user_email_address FROM customer_user_access")]
        invites = [{"email": r.customer_user_access_invitation.email_address,
                    "role": r.customer_user_access_invitation.access_role.name,
                    "status": r.customer_user_access_invitation.invitation_status.name}
                   for r in search("SELECT customer_user_access_invitation.email_address, "
                                   "customer_user_access_invitation.access_role, "
                                   "customer_user_access_invitation.invitation_status FROM customer_user_access_invitation")]
        own = {os.environ.get(f"{p}_LOGIN_CUSTOMER_ID", "").replace("-", "") for p in ("MK", "DCL")} - {""}
        managers = [{"manager": r.customer_manager_link.manager_customer.split("/")[-1],
                     "is_jays_mcc": r.customer_manager_link.manager_customer.split("/")[-1] in own,
                     "status": r.customer_manager_link.status.name}
                    for r in search("SELECT customer_manager_link.manager_customer, customer_manager_link.status "
                                    "FROM customer_manager_link")]
        links = []
        for r in search("SELECT product_link.type, product_link.merchant_center.merchant_center_id FROM product_link"):
            p = r.product_link
            links.append({"type": p.type_.name, "merchant_center_id": p.merchant_center.merchant_center_id or None})
        return {"users": users, "pending_invitations": invites, "manager_links": managers, "product_links": links}
    out["security"] = safe("security", security)
    return out


def main():
    args = sys.argv[1:]
    only = None
    if "--accounts" in args:
        i = args.index("--accounts")
        only = [x.strip() for x in args[i + 1].split(",") if x.strip()]
        del args[i:i + 2]
    path = args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), "monthly_pull.json")
    result = {"accounts": [], "errors": [], "skipped": []}
    clients = {}
    for cid in (only or list(KNOWN)):
        if cid in NEVER_TOUCH or cid not in KNOWN:
            result["skipped"].append(cid)
            continue
        acct = KNOWN[cid]
        try:
            if acct["mcc"] not in clients:
                clients[acct["mcc"]] = client_for(acct["mcc"]).get_service("GoogleAdsService")
            result["accounts"].append(pull_account(clients[acct["mcc"]], acct))
        except Exception as e:
            result["errors"].append(clean(f"{acct['name']} {cid}: {type(e).__name__}: {getattr(e, 'failure', None) or e}")[:500])
    with open(path, "w") as f:
        json.dump(result, f, default=str)
    print(f"wrote {path}: {len(result['accounts'])} accounts, {len(result['errors'])} account errors, "
          f"{sum(len(a['errors']) for a in result['accounts'])} section errors, {len(result['skipped'])} skipped")


if __name__ == "__main__":
    main()
