"""L6 — the HTTP read layer.

Every endpoint is a projection of stored rows. Nothing here runs an engine,
a scan or an ingestion: see docs/09-api-contracts.md section 1. An endpoint
that computes inline can time out, can disagree with yesterday's answer for
the same date, and can show two users different numbers for the same query.
"""
