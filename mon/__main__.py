"""`python -m mon [panel|dlwatch] …` — same as the installed commands, for running from a checkout.

Absolute import on purpose: PyInstaller runs this file as the top-level script of the one-file binary."""
from mon.cli import main

main()
