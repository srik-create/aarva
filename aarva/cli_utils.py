"""Shared CLI utilities. Used by aarva.review, aarva.crosscut,
aarva.search, aarva.publish_articles for consistent terminal colors
and other small helpers. Web-side code does not import this — it has
no ANSI / TTY assumptions.
"""
from __future__ import annotations

import sys


def color(s: str, code: str) -> str:
    """Wrap `s` in an ANSI escape if stdout is a TTY; otherwise leave
    it bare. The CLI scripts pipe-friendly when redirected to a file."""
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


# Style helpers — short names because they're used inline a lot.
def BOLD(s: str) -> str:   return color(s, "1")
def DIM(s: str) -> str:    return color(s, "2")
def RED(s: str) -> str:    return color(s, "31")
def GREEN(s: str) -> str:  return color(s, "32")
def YELLOW(s: str) -> str: return color(s, "33")
def BLUE(s: str) -> str:   return color(s, "34")
def MAGENTA(s: str) -> str: return color(s, "35")
def CYAN(s: str) -> str:   return color(s, "36")
