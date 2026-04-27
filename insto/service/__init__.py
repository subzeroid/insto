"""Service layer: facade, exporter, history, analytics.

Sits between command-layer (`insto.commands`) and backend-layer
(`insto.backends`). Commands talk only to `OsintFacade`; the facade
composes backend calls with analytics, history, and exporter helpers.
"""
