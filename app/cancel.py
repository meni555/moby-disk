"""Gemeinsame Ausnahme für abgebrochene Aufträge (ohne Abhängigkeiten, damit Kern und Ziele sie teilen können)."""


class Cancelled(Exception):
    """Der Auftrag wurde auf Wunsch des Benutzers abgebrochen."""
