"""Thin entry point so the Windows Scheduled Task can call one script."""
from mcb_digest.main import main
import sys

if __name__ == "__main__":
    sys.exit(main())
