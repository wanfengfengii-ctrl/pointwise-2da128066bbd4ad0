from .temporal import build_timeline, changed_intervals, region_at, UNASSIGNED


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


def _desired_resolution(store, timelines, ident, fact):
    if fact["deleted"]:
        return None
    customer = fact["customer"]
    if customer not in timelines:
        timelines[customer] = store.timeline(customer)
    return {"id": ident, "day": fact["at"] // 86400,
            "region": region_at(timelines[customer], fact["at"]),
            "cents": fact["cents"]}


def apply_batch(store, batch, fail_at=None):
    validate(batch)
    store.examined.clear()
    db = store.db
    db.execute("BEGIN IMMEDIATE")
    try:
        affected = set()
        accepted_facts = set()

        # Apply source records.  Only a genuinely newer revision is accepted;
        # the point lookup used for the version decision is not a traced read.
        for event in batch["dimensions"]:
            old = db.execute("SELECT * FROM source_dim WHERE id=?",
                             (event["id"],)).fetchone()
            if store.accept("source_dim", event):
                affected.add(event["customer"])
                if old is not None:
                    affected.add(old["customer"])
        for fact in batch["facts"]:
            if store.accept("facts", fact):
                accepted_facts.add(fact["id"])

        # Rebuild the complete dimension timeline for every old and new
        # customer touched by an accepted dimension revision.  Remember the
        # before/after region functions to derive the changed time set D.
        changes = []
        for customer in sorted(affected):
            before = store.timeline(customer)
            after = build_timeline(store.dimension_events(customer))
            store.replace_timeline(customer, after)
            for lo, hi in changed_intervals(before, after):
                changes.append((customer, lo, hi))
        if fail_at == "after_intervals":
            raise RuntimeError("INJECTED_AFTER_INTERVALS")

        # Attribution must be recomputed for exactly: newly accepted fact
        # versions (wherever they moved) plus every fact whose final source
        # coordinates fall inside D.  facts_in is called per changed segment,
        # never across an unchanged gap or into an unrelated infinite tail.
        work = {}
        for ident in sorted(accepted_facts):
            work[ident] = store.fact(ident)
        for customer, lo, hi in changes:
            for fact in store.facts_in(customer, lo, hi):
                work[fact["id"]] = fact

        timelines = {}
        for ident in sorted(work):
            fact = work[ident]
            desired = _desired_resolution(store, timelines, ident, fact)
            previous = store.resolution(ident)
            if previous == desired:
                continue
            # Replace the old contribution instead of accumulating a new one.
            if previous is not None:
                store.add_daily(previous, -1)
            if desired is not None:
                store.save_resolution(desired)
                store.add_daily(desired, 1)
            else:
                db.execute("DELETE FROM resolved WHERE id=?", (ident,))
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
        "daily": [dict(r) for r in db.execute("SELECT * FROM daily ORDER BY day,region")],
    }
