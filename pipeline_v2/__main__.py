"""python -m pipeline_v2 run | status | warm

    run      the nightly run (the monthly steps on the 1st by themselves); see run.py
    status   what the pipeline is doing (--fast skips the heights queue count); see status.py
    warm     rebuild the app's picker indexes and clear its stale pools; then reload the app
"""
import sys


def main() -> int:
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "run":
        from pipeline_v2 import run
        return run.main(rest)
    if cmd == "status":
        from pipeline_v2 import status
        return status.main(rest)
    if cmd == "warm":
        from pipeline_v2 import run
        return run.warm_main(rest)
    print(__doc__.strip(), file=sys.stderr)
    return 2


sys.exit(main())
