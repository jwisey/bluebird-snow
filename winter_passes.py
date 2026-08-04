"""Alpine road passes that close for the ski season.

WHY THIS FILE EXISTS
--------------------
OSRM routes on the current road graph and knows nothing about seasonal closures. The first
transfers run happened in August and returned Turin -> Val d'Isere as 148 km / 2h18, which
is only achievable over Mont Cenis and the Col de l'Iseran. Both are shut from roughly
November to late June. In ski season that drive is nearer 300 km and four hours, so the
"fastest gateway" the app would have shown was a summer answer to a winter question.

So: every route is checked against these summits, and one that goes over a closed pass is
not offered. Only passes that genuinely shut are listed. Roads that are ploughed all winter
because a resort or a valley depends on them - Lautaret, Montgenevre, Julier, Maloja,
Bernina, Simplon, Arlberg, Fern, Reschen, Brenner, Gerlos, Foscagno, and the Dolomite cols
- are deliberately absent, because excluding them would throw away perfectly good routes.

RADII, AND THE THREE PASSES THAT ARE NOT LISTED
-----------------------------------------------
3 km is enough to catch a route crossing a summit without snagging a valley road below it.

Gotthard, San Bernardino and Great St Bernard are deliberately absent even though all three
summit roads do shut. Each has a year-round road tunnel running almost directly underneath
it - the Great St Bernard tunnel passes within about 1.2 km of the hospice, San Bernardino's
closer still - so no radius reliably separates the closed pass from the open tunnel. Getting
that wrong would delete Geneva as a gateway for Cervinia and Zurich for the Ticino, which is
a far worse answer than the one it would be protecting against: the tunnel is how everyone
actually goes, and routing over the summit instead costs twenty or thirty minutes, not the
ninety-plus that Iseran or Timmelsjoch would.
"""

# name: (lat, lon, radius_km)
CLOSED = {
    # France
    "Col de l'Iseran":            (45.4175, 7.0308, 3.0),
    "Col du Mont-Cenis":          (45.2547, 6.9022, 3.0),
    # 1.2 km, not 3: the Col du Lautaret is 3.26 km away and is ploughed all winter -
    # it is the road Grenoble, Chambery and Lyon all use to reach Briancon. A 3 km
    # circle here came within 260 m of rejecting it, and did cost Serre Chevalier
    # every gateway except Turin. The Galibier road hairpins across its own summit,
    # so anything actually going over it passes within a few hundred metres.
    "Col du Galibier":            (45.0639, 6.4078, 1.2),
    "Col d'Izoard":               (44.8203, 6.7350, 3.0),
    "Col Agnel":                  (44.6853, 6.9781, 3.0),
    "Col de la Bonette":          (44.3214, 6.8069, 3.0),
    "Col de la Croix de Fer":     (45.2278, 6.1892, 3.0),
    "Col du Glandon":             (45.2350, 6.1725, 3.0),
    "Col de la Madeleine":        (45.4297, 6.3878, 3.0),
    "Col du Petit St-Bernard":    (45.6797, 6.8836, 3.0),
    "Cormet de Roselend":         (45.6753, 6.6800, 3.0),
    "Col de la Cayolle":          (44.2617, 6.7444, 3.0),
    "Col d'Allos":                (44.2814, 6.5928, 3.0),
    # Switzerland
    "Furkapass":                  (46.5722, 8.4153, 3.0),
    "Grimselpass":                (46.5614, 8.3378, 3.0),
    "Sustenpass":                 (46.7264, 8.4497, 3.0),
    "Nufenenpass":                (46.4775, 8.3856, 3.0),
    "Klausenpass":                (46.8697, 8.8564, 3.0),
    "Oberalppass":                (46.6564, 8.6708, 3.0),
    "Lukmanierpass":              (46.5658, 8.8017, 3.0),
    "Splugenpass":                (46.5053, 9.3306, 3.0),
    "Albulapass":                 (46.5836, 9.8331, 3.0),
    "Fluelapass":                 (46.7492, 9.9508, 3.0),
    "Umbrailpass":                (46.5406, 10.4319, 3.0),
    # Austria
    "Timmelsjoch":                (46.9083, 11.0958, 3.0),
    "Grossglockner Hochalpenstr": (47.0783, 12.8358, 3.0),
    "Hahntennjoch":               (47.2900, 10.6567, 3.0),
    "Silvretta Hochalpenstrasse": (46.9167, 10.0917, 3.0),
    "Nockalmstrasse":             (46.9000, 13.7833, 3.0),
    "Solkpass":                   (47.2750, 13.9200, 3.0),
    "Staller Sattel":             (46.8875, 12.2264, 3.0),
    # Italy
    "Passo dello Stelvio":        (46.5286, 10.4533, 3.0),
    "Passo di Gavia":             (46.3428, 10.4936, 3.0),
    "Colle del Nivolet":          (45.4744, 7.1361, 3.0),
}


def decode(polyline, precision=5):
    """Google encoded-polyline -> [(lat, lon)]. OSRM returns this by default."""
    factor = float(10 ** precision)
    lat = lng = index = 0
    out = []
    while index < len(polyline):
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(polyline[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else (result >> 1)
            if _ == 0:
                lat += d
            else:
                lng += d
        out.append((lat / factor, lng / factor))
    return out


def blocked_by(points):
    """Name of the first closed pass this route goes over, or None.

    Cheap latitude/longitude box first, haversine only for the handful that survive it -
    420 routes times 35 passes times thousands of vertices is otherwise minutes of pure
    Python for a check that should be instant.
    """
    import math
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lo_a, hi_a, lo_o, hi_o = min(lats), max(lats), min(lons), max(lons)
    for name, (plat, plon, rad) in CLOSED.items():
        m = rad / 111.0 + 0.02
        if not (lo_a - m <= plat <= hi_a + m and lo_o - m <= plon <= hi_o + m):
            continue
        cosf = math.cos(math.radians(plat))
        for la, lo in points:
            dy = (la - plat) * 111.0
            dx = (lo - plon) * 111.0 * cosf
            if dy * dy + dx * dx <= rad * rad:
                return name
    return None


# Roads over the same ranges that are kept clear all winter. These are NOT rejected - they
# are here so the module can prove, every time it loads, that no rejection circle has grown
# far enough to swallow one. Getting that wrong deletes a perfectly good gateway and looks
# exactly like a resort simply being hard to reach, which is why it went unnoticed for a
# whole routing run.
OPEN_ALL_WINTER = {
    "Col du Lautaret":            (45.0347, 6.4048),
    "Col de Montgenevre":         (44.9317, 6.7250),
    "Col de Vars":                (44.5378, 6.7017),
    "Colle della Maddalena":      (44.4222, 6.8869),
    "Frejus tunnel (FR portal)":  (45.1281, 6.6981),
    "Mont Blanc tunnel (FR)":     (45.8836, 6.8747),
    "Julierpass":                 (46.4728, 9.7203),
    "Malojapass":                 (46.4022, 9.6947),
    "Berninapass":                (46.4108, 10.0219),
    "Ofenpass":                   (46.6417, 10.2911),
    "Simplonpass":                (46.2500, 8.0292),
    "Vereina car train":          (46.8686, 9.8811),
    "Arlbergpass":                (47.1289, 10.2278),
    "Fernpass":                   (47.3603, 10.8375),
    "Reschenpass":                (46.8383, 10.5083),
    "Brennerpass":                (47.0044, 11.5069),
    "Gerlospass":                 (47.2333, 12.1167),
    "Felbertauern tunnel":        (47.1333, 12.5167),
    "Katschbergpass":             (47.0667, 13.6167),
    "Turracher Hohe":             (46.9139, 13.8722),
    "Passo di Foscagno":          (46.4914, 10.2261),
    "Passo del Tonale":           (46.2578, 10.5867),
    "Passo Pordoi":               (46.4878, 11.8128),
    "Passo Sella":                (46.5122, 11.7594),
    "Passo Giau":                 (46.4794, 12.0553),
    "Passo Campolongo":           (46.5203, 11.8556),
    "Passo Falzarego":            (46.5194, 12.0089),
    "Grodner Joch":               (46.5464, 11.7736),
    "Passo Rolle":                (46.2975, 11.7869),
}

# Under this many kilometres of clearance and the entry is not trustworthy: a summit
# coordinate typed from a map is good to a few hundred metres, so a circle that comes
# within twice its own radius of an open road is one small error away from rejecting it.
MIN_CLEARANCE = 2.0


def _clearance():
    """(closed pass, open road, km) for every pair too close for comfort."""
    import math
    bad = []
    for name, (plat, plon, rad) in CLOSED.items():
        for oname, (olat, olon) in OPEN_ALL_WINTER.items():
            cosf = math.cos(math.radians(plat))
            dy = (olat - plat) * 111.0
            dx = (olon - plon) * 111.0 * cosf
            d = math.hypot(dy, dx)
            if d < rad * MIN_CLEARANCE:
                bad.append((name, oname, round(d, 2), rad))
    return bad


_too_close = _clearance()
if _too_close:
    raise AssertionError(
        "A closed-pass circle is close enough to an open winter road to reject it: "
        + "; ".join(f"{c} ({r} km radius) is {d} km from {o}" for c, o, d, r in _too_close)
    )
