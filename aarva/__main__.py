"""Entry point so `python -m aarva ...` works.

The CLI lives in daily.py; this just re-exports.
"""
from aarva.daily import main

if __name__ == "__main__":
    main()
