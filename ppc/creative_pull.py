#!/usr/bin/env python3
"""Creative and assets pull for Jay's Creative and Assets task. Read only.

Per managed Google account (last 30 days): RSA ad strength and the headlines and
descriptions Google rates LOW, PMax and Demand Gen asset groups with missing or
thin asset types, Display and YouTube placements with spend and no conversions,
and the final URL domains (so copy can be checked against the client's website).

Usage: python3 ppc/creative_pull.py [output_path] [--accounts id,id,...]
"""

import datetime as dt
import json
import os
import sys
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daily_pull import ACCOUNTS, clean, client_for, d2s, q  # noqa: E402
from weekly_pull import M_STD, NEVER_TOUCH, std  # noqa: E402

KNOWN = {a["id"]: a for a in ACCOUNTS}
# Recommended counts per PMax asset group (Google's maximums are higher).
# Logo and business name are left out: with brand guidelines they live at campaign level.
PMAX_WANT = {"HEADLINE": 5, "LONG_HEADLINE": 2, "DESCRIPTION": 4, "MARKETING_IMAGE": 4,
             "SQUARE_MARKETING_IMAGE": 4, "PORTRAIT_MARKETING_IMAGE": 2, "YOUTUBE_VIDEO": 1}
JUNK_HINTS = ("mobileapp", "app", "kids", "cartoon", "nursery", "game", "minecraft", "roblox", "lullaby")


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

    info = list(search("SELECT customer.currency_code, customer.time_zone FROM customer"))[0].customer
    y = dt.datetime.now(ZoneInfo(info.time_zone)).date() - dt.timedelta(days=1)
    d30 = (y - dt.timedelta(days=29), y)
    rng = f"segments.date BETWEEN '{q(d30[0])}' AND '{q(d30[1])}'"
    out.update({"currency": info.currency_code, "period": f"{d2s(d30[0])} to {d2s(d30[1])}"})
    domains = set()

    def rsas():
        ads = {}
        for row in search("SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, "
                          "ad_group_ad.ad_strength, ad_group_ad.ad.final_urls, "
                          "ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, "
                          f"{M_STD} FROM ad_group_ad WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' "
                          "AND ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
                          f"AND campaign.status = 'ENABLED' AND {rng}"):
            ad = row.ad_group_ad
            for u in ad.ad.final_urls:
                domains.add(urlparse(u).netloc.lower())
            ads[str(ad.ad.id)] = dict(std(row.metrics), ad_id=str(ad.ad.id), ad_strength=ad.ad_strength.name,
                                      campaign_id=str(row.campaign.id), campaign=clean(row.campaign.name),
                                      ad_group_id=str(row.ad_group.id), ad_group=clean(row.ad_group.name),
                                      headlines=len(ad.ad.responsive_search_ad.headlines),
                                      descriptions=len(ad.ad.responsive_search_ad.descriptions),
                                      final_url=ad.ad.final_urls[0] if ad.ad.final_urls else None, low_assets=[])
        # Google no longer fills performance_label for RSA assets, so rate each asset on its own
        # 30 day numbers: LOW = rarely served (under 3% of the ad's asset impressions) or a CTR under
        # 60% of the ad's average, once the ad has enough impressions to judge.
        per_ad = {}
        for row in search("SELECT ad_group_ad.ad.id, ad_group_ad_asset_view.field_type, ad_group_ad_asset_view.pinned_field, "
                          "asset.id, asset.text_asset.text, metrics.impressions, metrics.clicks FROM ad_group_ad_asset_view "
                          "WHERE ad_group_ad_asset_view.enabled = TRUE AND ad_group_ad.status = 'ENABLED' "
                          "AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' AND campaign.status = 'ENABLED' "
                          f"AND {rng}"):
            v = row.ad_group_ad_asset_view
            a = per_ad.setdefault(str(row.ad_group_ad.ad.id), {})
            e = a.setdefault(str(row.asset.id), {"field": v.field_type.name, "asset_id": str(row.asset.id),
                                                 "text": row.asset.text_asset.text, "pinned": v.pinned_field.name,
                                                 "impressions": 0, "clicks": 0})
            e["impressions"] += row.metrics.impressions
            e["clicks"] += row.metrics.clicks
        for ad_id, assets in per_ad.items():
            a = ads.get(ad_id)
            if not a:
                continue
            for field in ("HEADLINE", "DESCRIPTION"):
                group = [x for x in assets.values() if x["field"] == field]
                imps = sum(x["impressions"] for x in group)
                clicks = sum(x["clicks"] for x in group)
                if imps < 2000 or not group:
                    continue
                avg_ctr = clicks / imps if imps else 0
                for x in group:
                    share = x["impressions"] / imps
                    ctr = x["clicks"] / x["impressions"] if x["impressions"] else 0
                    reason = None
                    if share < 0.03:
                        reason = f"rarely served, {share * 100:.1f}% of impressions"
                    elif x["impressions"] >= 300 and avg_ctr and ctr < 0.6 * avg_ctr:
                        reason = f"CTR {ctr * 100:.2f}% vs ad average {avg_ctr * 100:.2f}%"
                    if reason:
                        a["low_assets"].append(dict(x, reason=reason))
        weak = [a for a in ads.values() if a["ad_strength"] in ("POOR", "AVERAGE") or a["low_assets"]]
        return sorted(weak, key=lambda a: -a["cost"])[:60]
    out["rsas_needing_work"] = safe("rsas", rsas)

    def asset_groups():
        groups = {}
        for row in search("SELECT campaign.id, campaign.name, campaign.advertising_channel_type, asset_group.id, "
                          "asset_group.name, asset_group.ad_strength, asset_group.final_urls FROM asset_group "
                          "WHERE asset_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"):
            g = row.asset_group
            for u in g.final_urls:
                domains.add(urlparse(u).netloc.lower())
            groups[str(g.id)] = {"asset_group_id": str(g.id), "asset_group": clean(g.name), "ad_strength": g.ad_strength.name,
                                 "campaign_id": str(row.campaign.id), "campaign": clean(row.campaign.name),
                                 "channel": row.campaign.advertising_channel_type.name, "counts": {}, "low_assets": []}
        if groups:
            for row in search("SELECT asset_group.id, asset_group_asset.field_type, asset_group_asset.primary_status, "
                              "asset_group_asset.primary_status_reasons, asset.id, asset.text_asset.text FROM asset_group_asset "
                              "WHERE asset_group_asset.status = 'ENABLED' AND asset_group.status = 'ENABLED' "
                              "AND campaign.status = 'ENABLED'"):
                g = groups.get(str(row.asset_group.id))
                if not g:
                    continue
                f = row.asset_group_asset.field_type.name
                g["counts"][f] = g["counts"].get(f, 0) + 1
                st = row.asset_group_asset.primary_status.name
                if st in ("NOT_ELIGIBLE", "LIMITED"):
                    g["low_assets"].append({"field": f, "asset_id": str(row.asset.id), "text": row.asset.text_asset.text or None,
                                            "status": st, "reasons": [r.name for r in row.asset_group_asset.primary_status_reasons]})
        for g in groups.values():
            g["missing_or_thin"] = {k: f"{g['counts'].get(k, 0)} of {v}" for k, v in PMAX_WANT.items() if g["counts"].get(k, 0) < v}
        return [g for g in groups.values() if g["missing_or_thin"] or g["low_assets"]
                or g["ad_strength"] in ("POOR", "AVERAGE")]
    out["asset_groups_needing_work"] = safe("asset_groups", asset_groups)

    def demand_gen():
        res = []
        for row in search("SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, "
                          f"ad_group_ad.ad.type, {M_STD} FROM ad_group_ad WHERE campaign.advertising_channel_type = 'DEMAND_GEN' "
                          f"AND ad_group_ad.status = 'ENABLED' AND campaign.status = 'ENABLED' AND {rng}"):
            res.append(dict(std(row.metrics), ad_id=str(row.ad_group_ad.ad.id), type=row.ad_group_ad.ad.type_.name,
                            campaign=clean(row.campaign.name), campaign_id=str(row.campaign.id),
                            ad_group_id=str(row.ad_group.id)))
        return res
    out["demand_gen_ads"] = safe("demand_gen", demand_gen)

    def placements():
        res = []
        for row in search("SELECT detail_placement_view.display_name, detail_placement_view.group_placement_target_url, "
                          "detail_placement_view.placement, detail_placement_view.placement_type, campaign.id, campaign.name, "
                          f"campaign.advertising_channel_type, {M_STD} FROM detail_placement_view "
                          f"WHERE {rng} AND metrics.cost_micros > 0 AND metrics.conversions = 0 "
                          "ORDER BY metrics.cost_micros DESC LIMIT 200"):
            p = row.detail_placement_view
            name = clean(p.display_name)
            res.append(dict(std(row.metrics), placement=name, placement_id=p.placement, url=p.group_placement_target_url,
                            type=p.placement_type.name, campaign_id=str(row.campaign.id), campaign=clean(row.campaign.name),
                            channel=row.campaign.advertising_channel_type.name,
                            junk_hint=p.placement_type.name == "MOBILE_APPLICATION"
                            or any(h in (name + " " + p.group_placement_target_url).lower() for h in JUNK_HINTS)))
        return res
    out["placements_no_conversions"] = safe("placements", placements)
    out["website_domains"] = sorted(d for d in domains if d)
    return out


def main():
    args = sys.argv[1:]
    only = None
    if "--accounts" in args:
        i = args.index("--accounts")
        only = [x.strip() for x in args[i + 1].split(",") if x.strip()]
        del args[i:i + 2]
    path = args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), "creative_pull.json")
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
