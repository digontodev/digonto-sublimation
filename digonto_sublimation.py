#!/usr/bin/env python3
"""Digonto DTFRIP - launcher.

    digonto-dtfrip                 open the program window
    digonto-dtfrip --keepalive     no window: keep the white ink moving (used for autostart)
    digonto-dtfrip print a.png -w 120 -q high      any other words: the command line tool (-h for help)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _qt_ok():
    try:
        import PySide6.QtWidgets  # noqa: F401
        return True
    except Exception:
        return False


def main():
    try:
        import digonto_ui
        from dtfrip.flavor import TITLE
        digonto_ui.mac_app_name(TITLE)
    except Exception:
        pass
    args = sys.argv[1:]
    if args and args[0] == "--version":
        from dtfrip import __version__
        print(__version__)
        return 0
    if args and args[0] == "--keepalive":
        from dtfrip.backend import keepalive_forever
        keepalive_forever()
        return 0
    if args:
        from dtfrip.cli import main as cli
        return cli(args)
    if not _qt_ok():
        print("The program window needs PySide6 - please reinstall from Digonto Studio.")
        return 1
    from dtfrip.gui import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
