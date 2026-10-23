"""PyInstaller entry point for the portable Windows build."""
from __future__ import annotations

import runpy
import sys


def main():
    arguments = sys.argv[1:]
    if arguments[:1] == ["--fpv-worker"]:
        worker_args = arguments[1:]
        if len(worker_args) < 2 or worker_args[0] != "-m":
            raise SystemExit("Invalid internal worker command")
        module, *module_args = worker_args[1:]
        sys.argv = [module, *module_args]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return

    from fpv_matcher.web import main as web_main

    web_main()


if __name__ == "__main__":
    main()
