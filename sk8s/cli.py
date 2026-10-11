"""Console entrypoint. Subcommands land in later plans; version works now."""
import argparse

from sk8s import __version__


def main() -> int:
    parser = argparse.ArgumentParser(prog="sk8s")
    parser.add_argument("--version", action="version", version=f"sk8s {__version__}")
    parser.parse_args()
    parser.print_help()
    return 0
