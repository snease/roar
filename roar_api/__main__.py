"""Run the desktop application with python -m roar_api."""


def main():
    import argparse
    import sys
    from . import Design, Session
    parser = argparse.ArgumentParser(description="ROAR desktop / Python API")
    parser.add_argument("design", nargs="?", help="Saved .roar or design JSON file")
    parser.add_argument("--home", help="Checkout/assets root (defaults to ROAR_HOME)")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check-setup", action="store_true",
                        help="Check desktop dependencies, display access and configured ALIGN without opening ROAR")
    checks.add_argument("--check-gui", action="store_true",
                        help="Check desktop dependencies and display access without checking ALIGN or opening ROAR")
    options = parser.parse_args()
    from .floorplan.doctor import check_setup
    if options.check_setup:
        return check_setup()
    if options.check_gui:
        return check_setup(gui_only=True)
    if check_setup(gui_only=True, quiet=True, prepare_launch=True, output=sys.stderr):
        return 1
    design = Design.load(options.design) if options.design else None
    session = Session(design, home=options.home)
    if options.design:
        session.app._record_design_document(options.design)
    return session.run()


if __name__ == "__main__":
    raise SystemExit(main())
