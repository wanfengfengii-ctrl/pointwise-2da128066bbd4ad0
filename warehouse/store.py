import sqlite3
from .schema import DDL

DIM = ("id", "rev", "customer", "at", "region", "deleted")
FACT = ("id", "rev", "customer", "at", "cents", "deleted")


class Store:
    """One local warehouse. examined is a business read trace, not a query counter."""
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        self.examined = set()

    def close(self):
        self.db.close()

    def accept(self, table, record):
        columns = DIM if table == "source_dim" else FACT
        old = self.db.execute("SELECT * FROM " + table + " WHERE id=?", (record["id"],)).fetchone()
        if old and record["rev"] < old["rev"]:
            return False
        if old and record["rev"] == old["rev"]:
            if any(old[k] != record[k] for k in columns):
                raise ValueError("REVISION_CONFLICT:" + table + ":" + record["id"])
            return False
        marks = ",".join("?" for _ in columns)
        updates = ",".join(k + "=excluded." + k for k in columns if k != "id")
        self.db.execute("INSERT INTO " + table + " VALUES(" + marks +
                        ") ON CONFLICT(id) DO UPDATE SET " + updates,
                        tuple(record[k] for k in columns))
        return True

    def dimension_events(self, customer):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM source_dim WHERE customer=? AND deleted=0", (customer,))]

    def timeline(self, customer):
        return [dict(r) for r in self.db.execute(
            "SELECT lo,hi,region FROM intervals WHERE customer=? ORDER BY lo", (customer,))]

    def replace_timeline(self, customer, rows):
        self.db.execute("DELETE FROM intervals WHERE customer=?", (customer,))
        self.db.executemany("INSERT INTO intervals VALUES(?,?,?,?)",
                            [(customer, r["lo"], r["hi"], r["region"]) for r in rows])

    def fact(self, ident):
        row = self.db.execute("SELECT * FROM facts WHERE id=?", (ident,)).fetchone()
        if row:
            self.examined.add(row["id"])
        return dict(row) if row else None

    def facts_in(self, customer, lo, hi=None):
        sql = "SELECT * FROM facts INDEXED BY facts_by_customer_time WHERE customer=? AND at>=?"
        args = [customer, lo]
        if hi is not None:
            sql += " AND at<?"
            args.append(hi)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        self.examined.update(r["id"] for r in rows)
        return rows

    def resolution(self, ident):
        row = self.db.execute("SELECT * FROM resolved WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else None

    def save_resolution(self, row):
        self.db.execute("INSERT INTO resolved VALUES(:id,:day,:region,:cents) "
                        "ON CONFLICT(id) DO UPDATE SET day=excluded.day,"
                        "region=excluded.region,cents=excluded.cents", row)

    def add_daily(self, row, sign):
        self.db.execute("INSERT INTO daily VALUES(?,?,?,?) ON CONFLICT(day,region) "
                        "DO UPDATE SET cents=cents+excluded.cents,n=n+excluded.n",
                        (row["day"], row["region"], sign * row["cents"], sign))
        self.db.execute("DELETE FROM daily WHERE day=? AND region=? AND n=0",
                        (row["day"], row["region"]))
