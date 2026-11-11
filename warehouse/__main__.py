import argparse
import json
from pathlib import Path
from .pipeline import apply_batch, snapshot
from .store import Store


def reject_constant(value):
    raise ValueError("INVALID_JSON:" + value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("input")
    sub.add_parser("read")
    args = parser.parse_args()
    store = Store(args.db)
    try:
        if args.command == "apply":
            batch = json.loads(Path(args.input).read_text(encoding="utf-8"),
                               parse_constant=reject_constant)
            result = apply_batch(store, batch)
        else:
            result = snapshot(store)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
