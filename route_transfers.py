#!/usr/bin/env python3
"""Real airport-to-resort driving times, routed on GitHub's runners.

WHY THIS LIVES HERE AND NOT IN THE APP REPO'S TOOLS
---------------------------------------------------
The terrain script does this pass too, but it cannot complete on a Mac using Apple's
system Python: that ships LibreSSL 2.8.3, forked from OpenSSL 1.0.1, which has no TLS 1.3
support whatsoever. Every public OSRM instance now requires TLS 1.3, so the handshake
fails before any HTTP happens - SSLV3_ALERT_HANDSHAKE_FAILURE, on all 70 resorts, against
two independent servers. Overpass still accepts TLS 1.2, which is why the piste and
terrain fetches worked fine from the same script and made this look like a server problem.

Actions runners have a modern Python and unrestricted egress, so the routing just works
here. Output is published as a release asset the app repo can pull, rather than needing
anyone's laptop to be able to speak TLS 1.3.

WINTER
------
OSRM routes the road graph as it stands today, with no idea which of those roads spend the
ski season under four metres of snow. The first run of this script, in August, returned
Turin -> Val d'Isere as 148 km / 2h18 over Mont Cenis and the Col de l'Iseran; in January
that drive is nearer 300 km and four hours, because both are shut. So the route geometry
is now checked against winter_passes.CLOSED and a gateway reached over a closed pass is
not offered. To absorb the ones that get thrown away we route the six nearest airports and
keep the three fastest survivors, rather than routing three and hoping.
"""
import json, math, os, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from winter_passes import blocked_by, decode

# The project-OSRM demo server refuses TLS outright now. This instance is run by FOSSGIS
# for the OSM community; spot-checked Geneva->Verbier at 2h12 over 162km, which is right.
OSRM = "https://routing.openstreetmap.de/routed-car/route/v1/driving"
UA = "bluebird-transfers/1.0 (jacob.wise2@gmail.com)"
CANDIDATES = 6          # route the six nearest gateways...
KEEP = 3                # ...and keep the three fastest that are open in winter
GAP = 1.1               # seconds between requests - a free community server, be polite

AIRPORTS = {
    "GVA": ("Geneva", 46.2381, 6.1089),     "LYS": ("Lyon", 45.7256, 5.0811),
    "ZRH": ("Zurich", 47.4647, 8.5492),     "BSL": ("Basel", 47.5896, 7.5299),
    "INN": ("Innsbruck", 47.2602, 11.3439), "SZG": ("Salzburg", 47.7933, 13.0043),
    "MUC": ("Munich", 48.3538, 11.7861),    "MXP": ("Milan Malpensa", 45.6306, 8.7281),
    "TRN": ("Turin", 45.2008, 7.6496),      "VRN": ("Verona", 45.3957, 10.8885),
    "BZO": ("Bolzano", 46.4602, 11.3264),   "GRZ": ("Graz", 46.9911, 15.4396),
    "NCE": ("Nice", 43.6584, 7.2159),       "FRJ": ("Friedrichshafen", 47.6713, 9.5115),
    "GNB": ("Grenoble", 45.3629, 5.3294),   "VCE": ("Venice", 45.5053, 12.3519),
    "CMF": ("Chambery", 45.6381, 5.8800),   "LUG": ("Lugano", 46.0043, 8.9106),
}

FAILED = []
REJECTED = []           # (resort, code, minutes, pass name) - printed so it can be audited


def haversine(a, b, c, d):
    r = 6371.0088
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def drive(alat, alon, rlat, rlon, tries=4):
    """Minutes, km and the road geometry. Full geometry, not simplified: a simplified
    overview can drop the vertices either side of a summit, which is exactly where the
    closed-pass check needs points."""
    url = (f"{OSRM}/{alon},{alat};{rlon},{rlat}"
           "?overview=full&geometries=polyline")
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode())
            # A 200 with a non-Ok code is a routing failure, not a success. Check it.
            if d.get("code") != "Ok" or not d.get("routes"):
                raise RuntimeError(f"OSRM code={d.get('code')!r}")
            route = d["routes"][0]
            pts = decode(route.get("geometry") or "")
            # A route with no geometry cannot be checked for closed passes, and an
            # unchecked route is the thing this whole pass exists to avoid. Retry it.
            if not pts:
                raise RuntimeError("route came back without geometry")
            return (int(round(route["duration"] / 60.0)),
                    round(route["distance"] / 1000.0), pts)
        except Exception as ex:                                  # noqa: BLE001
            last = ex
            if attempt < tries - 1:
                time.sleep(4 * (attempt + 1))
    print(f"    failed: {last}", flush=True)
    return None, None, None


def main():
    resorts = json.load(open("resort_coords.json"))
    print(f"routing {len(resorts)} resorts x {CANDIDATES} gateways, "
          f"keeping the {KEEP} fastest open in winter\n", flush=True)
    out = {}
    for i, r in enumerate(resorts, 1):
        near = sorted(AIRPORTS.items(),
                      key=lambda kv: haversine(r["lat"], r["lon"], kv[1][1], kv[1][2]))
        opts = []
        for code, (nm, alat, alon) in near[:CANDIDATES]:
            mins, km, pts = drive(alat, alon, r["lat"], r["lon"])
            time.sleep(GAP)
            if not mins:
                continue
            shut = blocked_by(pts)
            if shut:
                REJECTED.append((r["name"], code, mins, shut))
                continue
            opts.append({"code": code, "airport": nm, "minutes": mins, "km": km})
        opts.sort(key=lambda o: o["minutes"])
        opts = opts[:KEEP]
        out[r["id"]] = opts
        if opts:
            b = opts[0]
            print(f"[{i}/{len(resorts)}] {r['name'][:24]:24s} "
                  f"{b['code']} {b['minutes']}min {b['km']}km"
                  + (f"  (+{len(opts)-1} more)" if len(opts) > 1 else ""), flush=True)
        else:
            FAILED.append(r["name"])
            print(f"[{i}/{len(resorts)}] {r['name'][:24]:24s} -- no winter route --", flush=True)

    # Every discarded gateway, so a wrong entry in winter_passes.CLOSED shows up as an
    # obviously silly rejection in the log instead of quietly deleting good routes.
    if REJECTED:
        print(f"\n{len(REJECTED)} gateways dropped for a closed pass:")
        for nm, code, mins, shut in REJECTED:
            print(f"    {nm[:24]:24s} {code}  {mins:4d}min  via {shut}")

    # A resort with no gateway would silently keep its old hand-written guess. Say so
    # loudly rather than letting a partial result look complete.
    if FAILED:
        share = 100.0 * len(FAILED) / len(resorts)
        print(f"\n{len(FAILED)} of {len(resorts)} resorts got no route ({share:.0f}%): {FAILED}")
        if share > 10.0:
            sys.exit("Too many failures to publish - the result would be mostly stale guesses.")

    json.dump(out, open("transfers.json", "w"), ensure_ascii=False, indent=1)
    got = sum(len(v) for v in out.values())
    print(f"\nwrote transfers.json - {got} routes across {len(out)} resorts")


if __name__ == "__main__":
    main()
