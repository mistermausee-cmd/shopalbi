"""shopalbi depth_capture — passive Albion market order-book capture agent.

Reads the game client's own network traffic (Photon over UDP), decodes the
marketplace responses the server sends to the client, and ships the full order
books to a shopalbi backend. Read-only: it never sends, injects, or modifies a
single packet — the same passive market-data technique the public Albion Data
Project client uses, which Albion's developers permit.
"""

__version__ = "1.0.0"
