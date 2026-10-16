#!/usr/bin/env python3
"""Tripwire for Jay's managed Google Ads accounts. Alert only, never changes anything.

Checks per account: today's spend vs the last 7 day daily average, campaigns or
users created in the last 24 hours, pending user invitations, account status and
billing setup. Prints exactly "All clear" when nothing is wrong, otherwise one
alert block per issue.

Usage: python3 ppc/tripwire.py [--accounts id,id,...] [--json path]
"""

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daily_pull import ACCOUNTS, clean, client_for  # noqa: E402
from weekly_pull import NEVER_TOUCH  # noqa: E402

KNOWN = {a["id"]: a for a in ACCOUNTS}
SPEND_RATIO = 1.5


def check(svc, acct):
    cid = acct["id"]
    search = lambda q: list(svc.search(customer_id=cid, query=q))
    alerts, notes = [], []

    c = search("SELECT customer.status, customer.currency_code, customer.time_zone FROM customer")[0].customer
    cur = c.currency_code
    if c.status.name != "ENABLED":
        alerts.append(f"Account status is {c.status.name}")

    rows = search("SELECT metrics.cost_micros FROM customer WHERE segments.date DURING TODAY")
    today = sum(r.metrics.cost_micros for r in rows) / 1e6
    rows = search("SELECT metrics.cost_micros FROM customer WHERE segments.date DURING LAST_7_DAYS")
    l7_avg = sum(r.metrics.cost_micros for r in rows) / 1e6 / 7
    if l7_avg and today > l7_avg * SPEND_RATIO:
        alerts.append(f"Spend today {cur} {today:,.2f} is {today / l7_avg * 100:.0f}% of the L7 daily average "
                      f"{cur} {l7_avg:,.2f}, and the day is not over")
    elif not l7_avg and today > 0:
        alerts.append(f"Spend today {cur} {today:,.2f} with no spend in the last 7 days")

    statuses = [r.billing_setup.status.name for r in search("SELECT billing_setup.status FROM billing_setup")]
    if "APPROVED" not in statuses:
        alerts.append(f"No approved billing setup (statuses: {', '.join(statuses) or 'none'})")

    # change_event needs an explicit time window inside the last 30 days.
    now = dt.datetime.now(dt.timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    tz_now = dt.datetime.now(dt.timezone.utc).astimezone()
    try:
        from zoneinfo import ZoneInfo
        tz_now = dt.datetime.now(ZoneInfo(c.time_zone))
    except Exception:
        pass
    start = (tz_now - dt.timedelta(hours=24)).strftime(fmt)
    for r in search("SELECT change_event.change_date_time, change_event.change_resource_type, "
                    "change_event.resource_change_operation, change_event.user_email, change_event.campaign, "
                    "change_event.client_type FROM change_event "
                    f"WHERE change_event.change_date_time >= '{start}' AND change_event.change_date_time <= '{tz_now.strftime(fmt)}' "
                    "AND change_event.change_resource_type = 'CAMPAIGN' "
                    "AND change_event.resource_change_operation = 'CREATE' LIMIT 100"):
        e = r.change_event
        name = ""
        if e.campaign:
            got = search(f"SELECT campaign.name, campaign.status FROM campaign WHERE campaign.resource_name = '{e.campaign}'")
            if got:
                name = f"{clean(got[0].campaign.name)} ({got[0].campaign.status.name})"
        alerts.append(f"New campaign created {e.change_date_time.replace('-', '/')} by {e.user_email or 'unknown'} "
                      f"via {e.client_type.name}: {name or e.campaign}")

    since = (now - dt.timedelta(hours=24))
    try:
        for r in search("SELECT customer_user_access.email_address, customer_user_access.access_role, "
                        "customer_user_access.access_creation_date_time, customer_user_access.inviter_user_email_address "
                        "FROM customer_user_access"):
            u = r.customer_user_access
            created = u.access_creation_date_time
            if created:
                try:
                    t = dt.datetime.fromisoformat(created[:19]).replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    t = None
                if t and t >= since - dt.timedelta(hours=14):  # account time zone slack
                    alerts.append(f"User access granted {created[:16].replace('-', '/')}: {u.email_address} "
                                  f"as {u.access_role.name}, invited by {u.inviter_user_email_address or 'unknown'}")
        for r in search("SELECT customer_user_access_invitation.email_address, customer_user_access_invitation.access_role, "
                        "customer_user_access_invitation.creation_date_time, customer_user_access_invitation.invitation_status "
                        "FROM customer_user_access_invitation"):
            i = r.customer_user_access_invitation
            if i.invitation_status.name == "PENDING":
                alerts.append(f"Pending user invitation: {i.email_address} as {i.access_role.name}, "
                              f"sent {i.creation_date_time[:16].replace('-', '/')}")
    except Exception as e:
        notes.append(f"user check unavailable: {type(e).__name__}")
    return {"id": cid, "name": acct["name"], "currency": cur, "today": round(today, 2),
            "l7_daily_avg": round(l7_avg, 2), "alerts": alerts, "notes": notes}


def main():
    args = sys.argv[1:]
    ids, out_path = list(KNOWN), None
    if "--accounts" in args:
        i = args.index("--accounts")
        ids = [x.strip() for x in args[i + 1].split(",") if x.strip()]
    if "--json" in args:
        out_path = args[args.index("--json") + 1]
    clients, results, problems = {}, [], []
    for cid in ids:
        if cid in NEVER_TOUCH or cid not in KNOWN:
            continue
        acct = KNOWN[cid]
        try:
            if acct["mcc"] not in clients:
                clients[acct["mcc"]] = client_for(acct["mcc"]).get_service("GoogleAdsService")
            results.append(check(clients[acct["mcc"]], acct))
        except Exception as e:
            problems.append(clean(f"{acct['name']} {cid}: check failed, {type(e).__name__}: "
                                  f"{getattr(e, 'failure', None) or e}")[:300])
    if out_path:
        with open(out_path, "w") as f:
            json.dump({"results": results, "problems": problems}, f)
    flagged = [r for r in results if r["alerts"]]
    if not flagged and not problems:
        print("All clear")
        return
    print("TRIPWIRE ALERT")
    for r in flagged:
        print(f"{r['name']} {r['id']}:")
        for a in r["alerts"]:
            print(f"  {clean(a)}")
    for p in problems:
        print(f"  {p}")


if __name__ == "__main__":
    main()
