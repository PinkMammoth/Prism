"""Prism perps co-pilot: a human decision-support CONSUMER of Strategy Lab evidence.

One-way dependency: this package reads Lab records (strategies, evidence profiles, forward
summaries) and writes only its own ``copilot_*`` tables. No Lab module imports it, so a
co-pilot policy can never change a screen, an FDR analysis, an evidence tier, forward
evidence or validation. Nothing here places, sizes or approves orders.
"""
