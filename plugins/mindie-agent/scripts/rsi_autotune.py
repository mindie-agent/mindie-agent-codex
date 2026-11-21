from mindie_rsi import rsi_autotune as _impl
from mindie_rsi.rsi_autotune import *  # noqa: F401,F403


def main(argv=None):
    _impl.AutotuneController = globals().get("AutotuneController", _impl.AutotuneController)
    return _impl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
