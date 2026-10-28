"""R1 (legacy S1-T6): offline SIG-to-Polymarket/Kalshi contract mapping workflow.

Everything here is offline. No module in this package opens a network connection; it
reads committed SIG fixtures, committed files under ``markets/`` and, optionally, a frozen
S1-T5 venue output that the operator supplies by path.
"""
