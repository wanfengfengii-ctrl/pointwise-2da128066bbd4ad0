UNASSIGNED = "__UNASSIGNED__"


def build_timeline(events):
    # Different source event ids can compete at the same effective timestamp.
    winners = {}
    for event in events:
        at = event["at"]
        prev = winners.get(at)
        if prev is None or (event["rev"], event["id"]) > (prev["rev"], prev["id"]):
            winners[at] = event
    points = sorted(winners)
    rows = []
    for i, at in enumerate(points):
        region = winners[at]["region"]
        hi = points[i + 1] if i + 1 < len(points) else None
        if rows and rows[-1]["region"] == region:
            rows[-1]["hi"] = hi
        else:
            rows.append({"lo": at, "hi": hi, "region": region})
    return rows


def region_at(rows, at):
    for row in rows:
        if row["lo"] <= at and (row["hi"] is None or at < row["hi"]):
            return row["region"]
    return UNASSIGNED


def changed_intervals(before, after):
    """Return the minimal [lo, hi) segments where the region function differs.

    Only region values matter: boundaries and winning (rev, id) that leave the
    region unchanged are not changes.  A missing assignment is compared as
    __UNASSIGNED__.  Adjacent changed segments are merged.
    """
    points = set()
    for rows in (before, after):
        for row in rows:
            points.add(row["lo"])
            if row["hi"] is not None:
                points.add(row["hi"])
    points = sorted(points)
    segments = []
    for i, lo in enumerate(points):
        hi = points[i + 1] if i + 1 < len(points) else None
        if region_at(before, lo) != region_at(after, lo):
            segments.append([lo, hi])
    merged = []
    for lo, hi in segments:
        if merged and merged[-1][1] == lo:
            merged[-1][1] = hi
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]
