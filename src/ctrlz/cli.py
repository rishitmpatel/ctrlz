"""Console-script entry point.  Kept tiny: `ctrlz snap --hook` runs after every agent tool call, so that
path must not import rich/argparse machinery.  Everything else lives in :mod:`ctrlz.clicore`."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["snap"] and "--hook" in args and "--" not in args:
        from .hooks import hook_main

        return hook_main(args)
    if len(args) == 2 and args[0] == "_daemon":  # internal: the detached watcher process (no rich needed)
        from .store import Store, StoreError
        from .watch import run_daemon

        try:
            return run_daemon(Store.open(args[1]))
        except StoreError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    from .clicore import main as _main

    return _main(args)


def __getattr__(name: str):  # re-export the implementation for tests / embedding
    if name.startswith("__"):  # e.g. __path__ probed by the import system: never load the heavy module for that
        raise AttributeError(name)
    from . import clicore

    try:
        return getattr(clicore, name)
    except AttributeError:
        raise AttributeError(f"module 'ctrlz.cli' has no attribute {name!r}") from None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
