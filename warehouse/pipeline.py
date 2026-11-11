from .temporal import build_timeline, region_at, UNASSIGNED


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def validate(batch):
    if not isinstance(batch, dict) or set(batch) != {"dimensions", "facts"}:
        raise ValueError("INVALID_BATCH")
    for name, field in (("dimensions", "region"), ("facts", "cents")):
        items = batch[name]
        if not isinstance(items, list):
            raise ValueError("INVALID_BATCH")
        seen = set()
        for row in items:
            required = {"id", "rev", "customer", "at", field, "deleted"}
            if not isinstance(row, dict) or set(row) != required:
                raise ValueError("INVALID_RECORD")
            if any(not isinstance(row[k], str) or not row[k] for k in ("id", "customer")):
                raise ValueError("INVALID_RECORD")
            if row["id"] in seen:
                raise ValueError("DUPLICATE_ID")
            seen.add(row["id"])
            if not _integer(row["rev"], 1, 2**63 - 1) or not _integer(row["at"], 0, 2**53):
                raise ValueError("INVALID_RECORD")
            if type(row["deleted"]) is not bool:
                raise ValueError("INVALID_RECORD")
            if field == "region":
                if not isinstance(row[field], str) or not row[field] or row[field] == UNASSIGNED:
                    raise ValueError("INVALID_RECORD")
            elif not _integer(row[field], -10**9, 10**9):
                raise ValueError("INVALID_RECORD")


def apply_batch(store, batch, fail_at=None):
    validate(batch)
    store.examined.clear()
    db = store.db
    db.execute("BEGIN IMMEDIATE")
    try:
        dirty = {}
        explicit = set()
        for event in batch["dimensions"]:
            if store.accept("source_dim", event):
                customer = event["customer"]
                dirty[customer] = min(dirty.get(customer, event["at"]), event["at"])
        for fact in batch["facts"]:
            if store.accept("facts", fact):
                explicit.add(fact["id"])
        for customer in sorted(dirty):
            store.replace_timeline(customer, build_timeline(store.dimension_events(customer)))
        if fail_at == "after_intervals":
            raise RuntimeError("INJECTED_AFTER_INTERVALS")
        # Original loader was deployed for ordered, append-only source exports.
        work = {ident: store.fact(ident) for ident in sorted(explicit)}
        for customer, lo in sorted(dirty.items()):
            for fact in store.facts_in(customer, lo):
                work[fact["id"]] = fact
        timelines = {}
        for ident in sorted(work):
            fact = work[ident]
            if fact["deleted"]:
                db.execute("DELETE FROM resolved WHERE id=?", (ident,))
                continue
            customer = fact["customer"]
            if customer not in timelines:
                timelines[customer] = store.timeline(customer)
            row = {"id": ident, "day": fact["at"] // 86400,
                   "region": region_at(timelines[customer], fact["at"]), "cents": fact["cents"]}
            previous = store.resolution(ident)
            if previous != row:
                store.save_resolution(row)
                store.add_daily(row, 1)
        if fail_at == "after_resolved":
            raise RuntimeError("INJECTED_AFTER_RESOLVED")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return {"examined_fact_ids": sorted(store.examined), **snapshot(store)}


def snapshot(store):
    db = store.db
    return {
        "resolved": [dict(r) for r in db.execute("SELECT * FROM resolved ORDER BY id")],
        "daily": [dict(r) for r in db.execute("SELECT * FROM daily ORDER BY day,region")]
    }
