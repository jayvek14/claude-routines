#!/usr/bin/env python3
"""Weekly PPC data pull for Jay's Weekly Optimiser.

Read only. Same env var auth as daily_pull.py. Writes one JSON file.

Usage: python3 ppc/weekly_pull.py [output_path] [--accounts id,id,...]

--accounts limits the run to the Google account IDs marked "Managed by agents = Yes"
in the Media King Account Targets sheet. Only IDs in daily_pull.ACCOUNTS can be
pulled, and the NEVER_TOUCH list is refused outright.
"""

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daily_pull import ACCOUNTS, clean, client_for, d2s, metrics_dict, q  # noqa: E402

NEVER_TOUCH = {
    "4511469172": "Eternal Lives",
    "8333577721": "Boyne Dental (M)",
    "5495912458": "Ready Steady Defend",
    "5180855297": "First Rate Tutors",
    "2993450360": "London Timber Supplies",
    "5803154725": "Test Account",
    "2730638594": "Ponsbourne Weddings",
}
KNOWN = {a["id"]: a for a in ACCOUNTS}

M = ("metrics.cost_micros, metrics.clicks, metrics.impressions, "
     "metrics.conversions_by_conversion_date, metrics.conversions_value_by_conversion_date")
# search_term_view, keyword_view and shopping_performance_view do not support the
# by_conversion_date metrics, so those use the standard conversion metrics.
M_STD = "metrics.cost_micros, metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value"


def std(m):
    cost = m.cost_micros / 1e6
    return {"cost": round(cost, 2), "clicks": m.clicks, "impressions": m.impressions,
            "conversions": round(m.conversions, 2), "conversion_value": round(m.conversions_value, 2),
            "cpc": round(cost / m.clicks, 2) if m.clicks else None,
            "cpa": round(cost / m.conversions, 2) if m.conversions else None,
            "roas": round(m.conversions_value / cost, 2) if cost else None}


def pct(a, b):
    return round((a / b - 1) * 100, 1) if a is not None and b else None


def check_url(url, timeout=10):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0 (MediaKing link check)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return None, clean(type(e).__name__ + " " + str(e))[:160]


def pull_account(svc, acct):
    cid = acct["id"]
    assert cid in KNOWN and cid not in NEVER_TOUCH
    search = lambda query: svc.search(customer_id=cid, query=query)
    out = {"id": cid, "name": acct["name"], "mcc": acct["mcc"], "kpi": acct["kpi"], "errors": []}

    def safe(label, fn):
        try:
            return fn()
        except Exception as e:
            out["errors"].append(clean(f"{label}: {type(e).__name__}: {getattr(e, 'failure', None) or e}")[:400])
            return None

    info = list(search("SELECT customer.currency_code, customer.time_zone, customer.status FROM customer"))[0].customer
    tz = ZoneInfo(info.time_zone)
    y = dt.datetime.now(tz).date() - dt.timedelta(days=1)
    tw = (y - dt.timedelta(days=6), y)                      # this week, last 7 complete days
    lw = (y - dt.timedelta(days=13), y - dt.timedelta(days=7))  # the 7 days before
    d30 = (y - dt.timedelta(days=29), y)
    d90 = (y - dt.timedelta(days=89), y)
    rng = lambda r: f"segments.date BETWEEN '{q(r[0])}' AND '{q(r[1])}'"
    out.update({"currency": info.currency_code, "account_status": info.status.name,
                "dates": {"this_week": f"{d2s(tw[0])} to {d2s(tw[1])}", "last_week": f"{d2s(lw[0])} to {d2s(lw[1])}",
                          "last_30_days": f"{d2s(d30[0])} to {d2s(d30[1])}"}})

    # Account totals week vs week.
    def totals():
        res = {}
        for label, r in (("this_week", tw), ("last_week", lw), ("last_30_days", d30)):
            rows = list(search(f"SELECT {M} FROM customer WHERE {rng(r)}"))
            res[label] = metrics_dict(rows[0].metrics) if rows else None
        tc, lc = (res["this_week"] or {}).get("cpc"), (res["last_week"] or {}).get("cpc")
        res["cpc_change_pct"] = pct(tc, lc)
        return res
    out["account_totals"] = safe("totals", totals)

    # Campaign impression share, lost IS budget and rank, CPC, this week vs last.
    def campaigns():
        camps = {}
        for label, r in (("this_week", tw), ("last_week", lw)):
            for row in search("SELECT campaign.id, campaign.name, campaign.advertising_channel_type, campaign.status, "
                              f"campaign.bidding_strategy_type, {M}, metrics.search_impression_share, "
                              "metrics.search_budget_lost_impression_share, metrics.search_rank_lost_impression_share "
                              f"FROM campaign WHERE campaign.status = 'ENABLED' AND {rng(r)}"):
                c = camps.setdefault(str(row.campaign.id), {
                    "id": str(row.campaign.id), "name": clean(row.campaign.name),
                    "channel": row.campaign.advertising_channel_type.name,
                    "bidding": row.campaign.bidding_strategy_type.name})
                mm = row.metrics
                c[label] = dict(metrics_dict(mm),
                                search_is=round(mm.search_impression_share, 3) if mm.search_impression_share else None,
                                lost_is_budget=round(mm.search_budget_lost_impression_share, 3) if mm.search_budget_lost_impression_share else None,
                                lost_is_rank=round(mm.search_rank_lost_impression_share, 3) if mm.search_rank_lost_impression_share else None)
        for c in camps.values():
            t, l = c.get("this_week") or {}, c.get("last_week") or {}
            c["cpc_change_pct"] = pct(t.get("cpc"), l.get("cpc"))
            for k in ("search_is", "lost_is_budget", "lost_is_rank"):
                if t.get(k) is not None and l.get(k) is not None:
                    c[f"{k}_change_pts"] = round((t[k] - l[k]) * 100, 1)
        return sorted(camps.values(), key=lambda c: -((c.get("this_week") or {}).get("cost") or 0))
    out["campaigns"] = safe("campaigns", campaigns)

    # Search terms, last 7 and 30 days, per ad group so negatives can be scoped.
    def terms(r, limit):
        res = []
        for row in search("SELECT search_term_view.search_term, search_term_view.status, campaign.id, campaign.name, "
                          f"ad_group.id, ad_group.name, segments.keyword.info.text, segments.keyword.info.match_type, {M_STD} "
                          f"FROM search_term_view WHERE {rng(r)} AND metrics.impressions > 0 "
                          f"ORDER BY metrics.cost_micros DESC LIMIT {limit}"):
            res.append(dict(std(row.metrics), term=clean(row.search_term_view.search_term),
                            status=row.search_term_view.status.name,
                            campaign_id=str(row.campaign.id), campaign=clean(row.campaign.name),
                            ad_group_id=str(row.ad_group.id), ad_group=clean(row.ad_group.name),
                            matched_keyword=clean(row.segments.keyword.info.text),
                            match_type=row.segments.keyword.info.match_type.name))
        return res
    out["search_terms_7d"] = safe("search_terms_7d", lambda: terms(tw, 1000))
    out["search_terms_30d"] = safe("search_terms_30d", lambda: terms(d30, 2000))

    # Keywords with 30 day performance.
    def keywords():
        res = []
        for row in search("SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_criterion.criterion_id, "
                          "ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
                          "ad_group_criterion.quality_info.quality_score, ad_group_criterion.effective_cpc_bid_micros, "
                          f"{M_STD}, metrics.search_impression_share FROM keyword_view "
                          "WHERE ad_group_criterion.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
                          f"AND campaign.status = 'ENABLED' AND {rng(d30)} ORDER BY metrics.cost_micros DESC LIMIT 2000"):
            k = row.ad_group_criterion
            res.append(dict(std(row.metrics), keyword=clean(k.keyword.text), match_type=k.keyword.match_type.name,
                            criterion_id=str(k.criterion_id), quality_score=k.quality_info.quality_score or None,
                            campaign_id=str(row.campaign.id), campaign=clean(row.campaign.name),
                            ad_group_id=str(row.ad_group.id), ad_group=clean(row.ad_group.name),
                            search_is=round(row.metrics.search_impression_share, 3) if row.metrics.search_impression_share else None))
        return res
    out["keywords_30d"] = safe("keywords", keywords)

    # Existing negatives, so proposed negatives can be checked against converting terms and keywords.
    def negatives():
        res = {"campaign": [], "ad_group": [], "shared_lists": []}
        for row in search("SELECT campaign.id, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
                          "FROM campaign_criterion WHERE campaign_criterion.negative = TRUE "
                          "AND campaign_criterion.type = 'KEYWORD' AND campaign.status = 'ENABLED'"):
            res["campaign"].append({"campaign_id": str(row.campaign.id), "text": clean(row.campaign_criterion.keyword.text),
                                    "match_type": row.campaign_criterion.keyword.match_type.name})
        for row in search("SELECT ad_group.id, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type "
                          "FROM ad_group_criterion WHERE ad_group_criterion.negative = TRUE "
                          "AND ad_group_criterion.type = 'KEYWORD' AND ad_group.status = 'ENABLED'"):
            res["ad_group"].append({"ad_group_id": str(row.ad_group.id), "text": clean(row.ad_group_criterion.keyword.text),
                                    "match_type": row.ad_group_criterion.keyword.match_type.name})
        lists = {}
        for row in search("SELECT shared_set.id, shared_set.name, campaign.id FROM campaign_shared_set "
                          "WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND campaign_shared_set.status = 'ENABLED' "
                          "AND campaign.status = 'ENABLED'"):
            l = lists.setdefault(str(row.shared_set.id), {"id": str(row.shared_set.id), "name": clean(row.shared_set.name),
                                                          "campaign_ids": [], "keywords": []})
            l["campaign_ids"].append(str(row.campaign.id))
        if lists:
            for row in search("SELECT shared_set.id, shared_criterion.keyword.text, shared_criterion.keyword.match_type "
                              f"FROM shared_criterion WHERE shared_set.id IN ({', '.join(lists)}) "
                              "AND shared_criterion.type = 'KEYWORD'"):
                lists[str(row.shared_set.id)]["keywords"].append(
                    {"text": clean(row.shared_criterion.keyword.text), "match_type": row.shared_criterion.keyword.match_type.name})
        res["shared_lists"] = list(lists.values())
        return res
    out["negatives"] = safe("negatives", negatives)

    # Shopping and PMax product performance, last 30 days.
    def products():
        res = []
        for row in search("SELECT campaign.id, campaign.name, campaign.advertising_channel_type, segments.product_item_id, "
                          f"segments.product_title, segments.product_brand, {M_STD} FROM shopping_performance_view "
                          f"WHERE {rng(d30)} AND metrics.impressions > 0 ORDER BY metrics.cost_micros DESC LIMIT 1000"):
            res.append(dict(std(row.metrics), item_id=clean(row.segments.product_item_id),
                            title=clean(row.segments.product_title), brand=clean(row.segments.product_brand),
                            campaign_id=str(row.campaign.id), campaign=clean(row.campaign.name),
                            channel=row.campaign.advertising_channel_type.name))
        return res
    out["products_30d"] = safe("products", products) if acct["kpi"] == "ROAS" else []

    # Conversion actions: status, primary flag, last conversion date over 90 days.
    def conv_actions():
        acts = {}
        for row in search("SELECT conversion_action.resource_name, conversion_action.id, conversion_action.name, "
                          "conversion_action.status, conversion_action.type, conversion_action.category, "
                          "conversion_action.primary_for_goal FROM conversion_action WHERE conversion_action.status != 'REMOVED'"):
            ca = row.conversion_action
            acts[ca.resource_name] = {"id": str(ca.id), "name": clean(ca.name), "status": ca.status.name,
                                      "type": ca.type_.name, "category": ca.category.name, "primary": ca.primary_for_goal,
                                      "last_conversion_date": None, "conv_7d": 0.0, "conv_30d": 0.0, "conv_90d": 0.0}
        for row in search("SELECT segments.conversion_action, segments.date, metrics.all_conversions FROM customer "
                          f"WHERE {rng(d90)} AND metrics.all_conversions > 0"):
            a = acts.get(row.segments.conversion_action)
            if not a:
                continue
            d = dt.date.fromisoformat(row.segments.date)
            a["conv_90d"] += row.metrics.all_conversions
            if d >= d30[0]:
                a["conv_30d"] += row.metrics.all_conversions
            if d >= tw[0]:
                a["conv_7d"] += row.metrics.all_conversions
            if not a["last_conversion_date"] or d2s(d) > a["last_conversion_date"]:
                a["last_conversion_date"] = d2s(d)
        for a in acts.values():
            for k in ("conv_7d", "conv_30d", "conv_90d"):
                a[k] = round(a[k], 2)
            a["days_since_last_conversion"] = ((y - dt.datetime.strptime(a["last_conversion_date"], "%Y/%m/%d").date()).days
                                               if a["last_conversion_date"] else None)
        return list(acts.values())
    out["conversion_actions"] = safe("conversion_actions", conv_actions)

    # Ads and assets flagged destination not working, plus a live check of final URLs.
    urls = {}

    def destinations():
        flagged = []
        for row in search("SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, "
                          "ad_group_ad.ad.final_urls, ad_group_ad.policy_summary.approval_status, "
                          "ad_group_ad.policy_summary.policy_topic_entries FROM ad_group_ad "
                          "WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            ad = row.ad_group_ad
            for u in ad.ad.final_urls:
                urls.setdefault(u, []).append(f"ad {ad.ad.id}")
            topics = [t.topic for t in ad.policy_summary.policy_topic_entries]
            if any("DESTINATION" in t.upper() for t in topics):
                flagged.append({"kind": "ad", "ad_id": str(ad.ad.id), "campaign": clean(row.campaign.name),
                                "campaign_id": str(row.campaign.id), "ad_group_id": str(row.ad_group.id),
                                "approval_status": ad.policy_summary.approval_status.name,
                                "topics": sorted({clean(t) for t in topics})})
        for row in search("SELECT campaign.name, asset_group.id, asset_group.name, asset_group.final_urls FROM asset_group "
                          "WHERE asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            for u in row.asset_group.final_urls:
                urls.setdefault(u, []).append(f"asset group {row.asset_group.id}")
        for res_name, q_ in (("ad_group_asset", "ad_group_asset.asset"), ("campaign_asset", "campaign_asset.asset"),
                             ("customer_asset", "customer_asset.asset")):
            for row in search(f"SELECT {q_}, asset.name, asset.type, asset.final_urls, {res_name}.status, "
                              f"asset.policy_summary.policy_topic_entries FROM {res_name} WHERE {res_name}.status = 'ENABLED'"):
                topics = [t.topic for t in row.asset.policy_summary.policy_topic_entries]
                if any("DESTINATION" in t.upper() for t in topics):
                    flagged.append({"kind": res_name, "asset": clean(row.asset.name or row.asset.type_.name),
                                    "asset_type": row.asset.type_.name, "topics": sorted({clean(t) for t in topics})})
                for u in row.asset.final_urls:
                    urls.setdefault(u, []).append(f"{res_name} {row.asset.type_.name}")
        return flagged
    out["destination_not_working"] = safe("destinations", destinations)

    def url_checks():
        broken, unreachable = [], 0
        for u in sorted(urls)[:150]:
            status, err = check_url(u)
            if status is None:
                unreachable += 1  # network or proxy failure, not proof the page is down
            elif status >= 400:
                broken.append({"url": u, "status": status, "used_by": sorted(set(urls[u]))[:10]})
        return {"checked": min(len(urls), 150), "total_urls": len(urls), "broken": broken,
                "could_not_check": unreachable,
                "note": "could_not_check means this environment could not reach the site; only rely on broken"}
    out["final_url_checks"] = safe("url_checks", url_checks)
    return out


def main():
    args = sys.argv[1:]
    only = None
    if "--accounts" in args:
        i = args.index("--accounts")
        only = [x.strip().replace("-", "") for x in args[i + 1].split(",") if x.strip()]
        del args[i:i + 2]
    path = args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), "weekly_pull.json")
    result = {"generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y/%m/%d %H:%M:%S"),
              "accounts": [], "errors": [], "skipped": []}
    ids = only if only is not None else list(KNOWN)
    clients = {}
    for cid in ids:
        if cid in NEVER_TOUCH:
            result["skipped"].append(f"{cid} {NEVER_TOUCH[cid]}: on the never touch list")
            continue
        if cid not in KNOWN:
            result["skipped"].append(f"{cid}: not in the script allowlist, add it to daily_pull.ACCOUNTS first")
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
