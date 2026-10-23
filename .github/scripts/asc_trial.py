"""Switch the yearly free trial on or off in every App Store territory.

The trial is not a single switch in App Store Connect: it is one
introductory offer per territory (175 of them). This script makes them match
a desired state, so turning the trial off or on is one command - or one edit
of `trial` in auralis-legal/remote-config.json, which a GitHub Action applies.

Usage:
  python3 asc_trial.py status
  python3 asc_trial.py apply --enabled false [--days 3] [--territory KNA] [--dry-run] [--force]
  python3 asc_trial.py from-config remote-config.json [--dry-run] [--force] [--pending-ok]

Environment:
  ASC_KEY_ID, ASC_ISSUER_ID       App Store Connect API key
  ASC_PRIVATE_KEY or ASC_KEY_PATH the .p8 contents, or a path to it
  ASC_APP_ID                      default 6795979324
  TRIAL_PRODUCT_ID                default auralis_pro_yearly

Safety: the paywall only reads the trial from the store since 1.0.2. Older
builds print "3-Day Free Trial" no matter what, so switching the trial OFF is
refused while the App Store still serves a version below 1.0.2 (--force
overrides). Trials already running are honoured by Apple either way.
"""
import argparse, json, os, sys, time, urllib.error, urllib.request

import jwt

APP_ID = os.environ.get("ASC_APP_ID", "6795979324")
PRODUCT_ID = os.environ.get("TRIAL_PRODUCT_ID", "auralis_pro_yearly")
BUNDLE_ID = "com.aytekinc.auralis"
FIRST_STORE_DRIVEN_VERSION = "1.0.2"
BASE = "https://api.appstoreconnect.apple.com"
DURATIONS = {3: "THREE_DAYS", 7: "ONE_WEEK", 14: "TWO_WEEKS", 30: "ONE_MONTH", 60: "TWO_MONTHS", 90: "THREE_MONTHS",
             180: "SIX_MONTHS", 365: "ONE_YEAR"}


def _key():
    if os.environ.get("ASC_PRIVATE_KEY"):
        return os.environ["ASC_PRIVATE_KEY"]
    with open(os.environ.get("ASC_KEY_PATH", "credentials/AuthKey.p8")) as f:
        return f.read()


KEY = _key()


def token():
    now = int(time.time())
    return jwt.encode({"iss": os.environ["ASC_ISSUER_ID"], "aud": "appstoreconnect-v1", "iat": now, "exp": now + 1100},
                      KEY, algorithm="ES256", headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"})


def call(method, path, body=None):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def get_all(path):
    out, url = [], path
    while url:
        st, js = call("GET", url)
        if st != 200:
            sys.exit(f"GET {url} -> {st} {json.dumps(js)[:300]}")
        out += js.get("data", [])
        url = js.get("links", {}).get("next")
    return out


def subscription_id():
    for group in get_all(f"/v1/apps/{APP_ID}/subscriptionGroups"):
        for sub in get_all(f"/v1/subscriptionGroups/{group['id']}/subscriptions"):
            if sub["attributes"]["productId"] == PRODUCT_ID:
                return sub["id"]
    sys.exit(f"subscription {PRODUCT_ID} not found")


def current_offers(sub_id):
    """territory -> (offer id, attributes)"""
    offers = {}
    for o in get_all(f"/v1/subscriptions/{sub_id}/introductoryOffers?limit=200&include=territory"):
        terr = (o["relationships"].get("territory", {}).get("data") or {}).get("id")
        offers[terr] = (o["id"], o["attributes"])
    return offers


def sold_territories(sub_id):
    terrs = set()
    for p in get_all(f"/v1/subscriptions/{sub_id}/prices?limit=200&include=territory"):
        t = (p["relationships"].get("territory", {}).get("data") or {}).get("id")
        if t:
            terrs.add(t)
    return terrs


def live_version():
    try:
        with urllib.request.urlopen(f"https://itunes.apple.com/lookup?bundleId={BUNDLE_ID}&t={int(time.time())}", timeout=15) as r:
            res = json.load(r).get("results", [])
            return res[0]["version"] if res else None
    except Exception:
        return None


def older(a, b):
    pa, pb = [int(x) for x in a.split(".")], [int(x) for x in b.split(".")]
    n = max(len(pa), len(pb))
    return (pa + [0] * (n - len(pa))) < (pb + [0] * (n - len(pb)))


def status():
    sub = subscription_id()
    offers = current_offers(sub)
    free = {t: a for t, (_, a) in offers.items() if a["offerMode"] == "FREE_TRIAL"}
    durations = sorted({a["duration"] for a in free.values()})
    print(f"{PRODUCT_ID}: free trial in {len(free)} territories {durations or ''}; other intro offers: {len(offers) - len(free)}")
    print(f"App Store version live: {live_version()}")
    return sub, offers


def apply(enabled, days, only=None, dry=False, force=False, pending_ok=False):
    if enabled and days not in DURATIONS:
        sys.exit(f"days must be one of {sorted(DURATIONS)}")
    if not enabled and not force:
        v = live_version()
        if v is None or older(v, FIRST_STORE_DRIVEN_VERSION):
            msg = (f"PENDING - trial stays ON for now: the App Store serves {v or 'an unknown version'}, whose paywall "
                   f"still says '3-Day Free Trial'. It switches off automatically once {FIRST_STORE_DRIVEN_VERSION}+ is live.\n"
                   f"BEKLIYOR - deneme simdilik ACIK: App Store'da {v or 'bilinmeyen surum'} yayinda. "
                   f"{FIRST_STORE_DRIVEN_VERSION} yayina girince otomatik kapanacak.")
            if pending_ok:
                print(msg)
                return 0
            sys.exit(msg + " (or pass --force)")
    sub, offers = status()
    want = DURATIONS.get(days)
    targets = {only} if only else (sold_territories(sub) | set(offers))
    deletes, creates = [], []
    for t in sorted(targets):
        have = offers.get(t)
        ok = have and have[1]["offerMode"] == "FREE_TRIAL" and have[1]["duration"] == want and have[1].get("numberOfPeriods") == 1
        if enabled and not ok:
            if have:
                deletes.append((t, have[0]))
            creates.append(t)
        if not enabled and have and have[1]["offerMode"] == "FREE_TRIAL":
            deletes.append((t, have[0]))
    print(f"plan: delete {len(deletes)}, create {len(creates)} ({'enable ' + want if enabled else 'disable'})")
    if dry:
        print("dry run - nothing changed")
        return 0
    failed = 0
    for t, oid in deletes:
        st, js = call("DELETE", f"/v1/subscriptionIntroductoryOffers/{oid}")
        if st not in (200, 204):
            failed += 1
            print(f"  delete {t} -> {st} {json.dumps(js)[:200]}")
    for t in creates:
        st, js = call("POST", "/v1/subscriptionIntroductoryOffers", {"data": {
            "type": "subscriptionIntroductoryOffers",
            "attributes": {"duration": want, "offerMode": "FREE_TRIAL", "numberOfPeriods": 1},
            "relationships": {"subscription": {"data": {"type": "subscriptions", "id": sub}},
                              "territory": {"data": {"type": "territories", "id": t}}}}})
        if st not in (200, 201):
            failed += 1
            print(f"  create {t} -> {st} {json.dumps(js)[:200]}")
    print(f"done, {failed} failed")
    status()
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status")
    a = sp.add_parser("apply")
    a.add_argument("--enabled", required=True, choices=["true", "false"])
    a.add_argument("--days", type=int, default=3)
    a.add_argument("--territory")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true")
    c = sp.add_parser("from-config")
    c.add_argument("path")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--force", action="store_true")
    c.add_argument("--pending-ok", action="store_true", help="a refused switch-off is reported, not a failure")
    args = ap.parse_args()
    if args.cmd == "status":
        status()
        return 0
    if args.cmd == "apply":
        return apply(args.enabled == "true", args.days, args.territory, args.dry_run, args.force)
    trial = json.load(open(args.path)).get("trial")
    if not isinstance(trial, dict) or not isinstance(trial.get("enabled"), bool):
        sys.exit("remote-config.json has no valid trial.enabled - nothing to do")
    return apply(trial["enabled"], int(trial.get("days", 3)), None, args.dry_run, args.force, args.pending_ok)


if __name__ == "__main__":
    sys.exit(main())
