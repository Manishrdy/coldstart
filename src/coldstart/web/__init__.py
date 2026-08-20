"""Module 21 — the live dashboard.

scope.md §1 and §13 listed "no UI" as a v1 non-goal; that was reversed
deliberately (see §15). Everything here is read-only: the web layer opens
`mode=ro` SQLite connections and never writes a row.
"""
