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
