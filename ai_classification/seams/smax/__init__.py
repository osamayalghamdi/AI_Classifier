"""SMAX — the real ticketing system adapter.

Everything that knows about SMAX lives here and only here: the HTTP client,
the external payload models, and the port implementation. The rest of the
codebase sees only the TicketSource interface from seams.port.

Files:
- client.py     — raw SMAX HTTP client (auth, endpoints, timeouts)
- models.py     — external SMAX payloads + translation to/from seams.port models
- real_source.py— TicketSource implementation over the client
"""
