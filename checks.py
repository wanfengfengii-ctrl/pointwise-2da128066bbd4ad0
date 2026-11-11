"""Business checks. No third-party packages or external services."""
import argparse
import json
from pathlib import Path
from warehouse import Store, apply_batch, snapshot


def dim(ident, rev=1, customer="A", at=0, region="east", deleted=False):
    return dict(id=ident, rev=rev, customer=customer, at=at, region=region, deleted=deleted)


def fact(ident, rev=1, customer="A", at=0, cents=100, deleted=False):
    return dict(id=ident, rev=rev, customer=customer, at=at, cents=cents, deleted=deleted)


def batch(dimensions=(), facts=()):
    return dict(dimensions=list(dimensions), facts=list(facts))


def require(ok, detail):
    if not ok:
        raise AssertionError(detail)


def expect(store, resolved, daily):
    require(snapshot(store) == {"resolved": resolved, "daily": daily}, repr(snapshot(store)))


def r(ident, region, cents, day=0):
    return dict(id=ident, day=day, region=region, cents=cents)


def a(region, cents, n, day=0):
    return dict(day=day, region=region, cents=cents, n=n)


def all_tables(store):
    return {table: [tuple(row) for row in store.db.execute(
        "SELECT * FROM " + table + " ORDER BY 1,2")]
        for table in ("source_dim", "intervals", "facts", "resolved", "daily")}


def normal():
    s = Store()
    initial = json.loads(Path("samples/initial.json").read_text(encoding="utf-8"))
    apply_batch(s, initial)
    rows = [r("f0", "east", 100), r("f1", "east", 250),
            r("f2", "west", -30), r("fb", "north", 700)]
    sums = [a("east", 350, 2), a("north", 700, 1), a("west", -30, 1)]
    expect(s, rows, sums)
    require(apply_batch(s, initial)["examined_fact_ids"] == [], "replay read facts")
    apply_batch(s, batch(facts=[fact("f3", at=86400, cents=12)]))
    expect(s, sorted(rows + [r("f3", "west", 12, 1)], key=lambda row: row["id"]),
           sums + [a("west", 12, 1, 1)])
    before = all_tables(s)
    try:
        apply_batch(s, batch([dim("d0", region="other")]))
    except ValueError as exc:
        require(str(exc).startswith("REVISION_CONFLICT:"), str(exc))
    else:
        raise AssertionError("same-revision conflict accepted")
    require(all_tables(s) == before, "conflict was not atomic")
    for point in ("after_intervals", "after_resolved"):
        try:
            apply_batch(s, batch([dim("late", at=200, region="south")],
                                 [fact("f4", at=210)]), fail_at=point)
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing fault injection")
        require(all_tables(s) == before, "injected failure leaked state")
    # An unmapped customer and a zero/negative amount remain real fact rows.
    t = Store()
    apply_batch(t, batch(facts=[fact("z", customer="Z", cents=0),
                               fact("neg", customer="Z", cents=-5, at=86400)]))
    expect(t, [r("neg", "__UNASSIGNED__", -5, 1), r("z", "__UNASSIGNED__", 0)],
           [a("__UNASSIGNED__", 0, 1), a("__UNASSIGNED__", -5, 1, 1)])
    t.close()
    s.close()
    print("NORMAL_REGRESSION_OK")


class Observed(Store):
    def __init__(self):
        super().__init__()
        self.windows = []

    def facts_in(self, customer, lo, hi=None):
        self.windows.append((customer, lo, hi))
        return super().facts_in(customer, lo, hi)


def seed(dimensions, facts):
    s = Observed()
    apply_batch(s, batch(dimensions, facts))
    s.windows.clear()
    return s


def locality(s, ids, allowed):
    require(s.examined == set(ids), "unexpected fact reads: " + repr(sorted(s.examined)))
    # Each requested range must fit entirely inside a semantically changed interval.
    for customer, lo, hi in s.windows:
        require(any(customer == c and lo >= left and
                    (right is None or (hi is not None and hi <= right))
                    for c, left, right in allowed),
                "range crosses unchanged history: " + repr((customer, lo, hi)))


def moved_boundary():
    s = seed([dim("base"), dim("turn", at=100, region="west")],
             [fact("before", at=10), fact("inside", at=60, cents=250),
              fact("edge", at=100, cents=-30), fact("after", at=200)])
    apply_batch(s, batch([dim("turn", rev=2, at=50, region="west")]))
    expect(s, [r("after", "west", 100), r("before", "east", 100),
               r("edge", "west", -30), r("inside", "west", 250)],
           [a("east", 100, 1), a("west", 320, 3)])
    locality(s, ["inside"], [("A", 50, 100)])
    s.close()


def relocated_event():
    s = seed([dim("base"), dim("move", at=50, region="west"),
              dim("bbase", customer="B", region="north")],
             [fact("a0", at=10), fact("a1", at=60),
              fact("b0", customer="B", at=10), fact("b1", customer="B", at=60)])
    apply_batch(s, batch([dim("move", rev=2, customer="B", at=50, region="south")]))
    expect(s, [r("a0", "east", 100), r("a1", "east", 100),
               r("b0", "north", 100), r("b1", "south", 100)],
           [a("east", 200, 2), a("north", 100, 1), a("south", 100, 1)])
    locality(s, ["a1", "b1"], [("A", 50, None), ("B", 50, None)])
    s.close()


def winner_retraction():
    s = seed([dim("base"), dim("a", at=50, region="west"),
              dim("z", at=50, region="south"), dim("end", at=100, region="east")],
             [fact("before", at=49), fact("first", at=50), fact("last", at=99),
              fact("after", at=100)])
    apply_batch(s, batch([dim("z", rev=2, at=50, region="south", deleted=True)]))
    expect(s, [r("after", "east", 100), r("before", "east", 100),
               r("first", "west", 100), r("last", "west", 100)],
           [a("east", 200, 2), a("west", 200, 2)])
    locality(s, ["first", "last"], [("A", 50, 100)])
    # Revise the losing event with an identical semantic winner: zero fact reads.
    s.windows.clear()
    apply_batch(s, batch([dim("a", rev=2, at=50, region="west")]))
    locality(s, [], [])
    s.close()


def fact_reassignment():
    s = seed([dim("base"), dim("b", customer="B", region="north")],
             [fact("keep", at=5), fact("move", at=10, cents=250),
              fact("remove", at=20, cents=-30), fact("zero", at=30, cents=0)])
    patch = batch(facts=[fact("move", rev=2, customer="B", at=86401, cents=450),
                         fact("remove", rev=2, at=20, cents=-30, deleted=True)])
    apply_batch(s, patch)
    expect(s, [r("keep", "east", 100), r("move", "north", 450, 1), r("zero", "east", 0)],
           [a("east", 100, 2), a("north", 450, 1, 1)])
    locality(s, ["move", "remove"], [])
    s.windows.clear()
    apply_batch(s, patch)
    locality(s, [], [])
    apply_batch(s, batch(facts=[fact("remove", rev=3, at=86401, cents=0)]))
    expect(s, [r("keep", "east", 100), r("move", "north", 450, 1),
               r("remove", "east", 0, 1), r("zero", "east", 0)],
           [a("east", 100, 2), a("east", 0, 1, 1), a("north", 450, 1, 1)])
    s.close()


def mixed_batch():
    s = seed([dim("base"), dim("turn", at=100, region="west")],
             [fact("stay", at=10), fact("x", at=60, cents=250), fact("y", at=70, cents=50)])
    before = all_tables(s)
    patch = batch([dim("turn", rev=2, at=50, region="west")],
                  [fact("x", rev=2, at=86401, cents=90), fact("y", rev=2, at=70, cents=50, deleted=True)])
    for point in ("after_intervals", "after_resolved"):
        try:
            apply_batch(s, patch, fail_at=point)
        except RuntimeError:
            pass
        else:
            raise AssertionError("fault injection bypassed")
        require(all_tables(s) == before, "failed correction leaked state")
    s.windows.clear()
    apply_batch(s, patch)
    expect(s, [r("stay", "east", 100), r("x", "west", 90, 1)],
           [a("east", 100, 1), a("west", 90, 1, 1)])
    locality(s, ["x", "y"], [("A", 50, 100)])
    s.close()


def disjoint_history():
    points = (10, 20, 30, 40, 49, 50, 99, 100)
    s = seed([dim("base", at=20), dim("turn", at=50, region="west"),
              dim("end", at=100)],
             [fact("t%03d" % at, at=at) for at in points])
    apply_batch(s, batch([dim("base", rev=2, at=40),
                          dim("turn", rev=2, at=50, region="west", deleted=True)]))
    expect(s, [r("t%03d" % at, "__UNASSIGNED__" if at < 40 else "east", 100)
               for at in points],
           [a("__UNASSIGNED__", 300, 3), a("east", 500, 5)])
    locality(s, ["t020", "t030", "t050", "t099"],
             [("A", 20, 40), ("A", 50, 100)])
    s.close()


def repair():
    failures = []
    for case in (moved_boundary, relocated_event, winner_retraction,
                 fact_reassignment, mixed_batch, disjoint_history):
        try:
            case()
        except AssertionError as exc:
            failures.append(case.__name__ + ": " + str(exc))
    if failures:
        print("SCD_REPAIR_MISMATCH")
        for failure in failures:
            print(failure)
        raise SystemExit(1)
    print("CORRECTION_REGRESSION_OK")


def demo():
    s = Store()
    apply_batch(s, json.loads(Path("samples/initial.json").read_text(encoding="utf-8")))
    print(json.dumps({"before": snapshot(s)}, sort_keys=True))
    print(json.dumps({"after": apply_batch(s, batch([
        dim("d1", rev=2, at=50, region="west")]))}, sort_keys=True))
    s.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["normal", "repair", "verify", "demo"])
    mode = parser.parse_args().mode
    if mode in ("normal", "verify"):
        normal()
    if mode in ("repair", "verify"):
        repair()
    if mode == "demo":
        demo()
