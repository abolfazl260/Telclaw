# Integrations

External services are documented and implemented behind the `delivery/` boundary.

## Advertio

- [Advertio Ingest API — complete upstream contract (2026-11-24)](CRAWLER-INTEGRATION.md)
- [Advertio upgrade roadmap — 18 changes, 6 issues](ADVERTIO_UPGRADE_2026-11-24.md)
- [Historical Telclaw integration notes (2026-08-06)](advertio-ingest-api.md)

The Advertio API is the final delivery stage. It must receive only validated business data; Telegram collection and AI processing must not depend on Advertio implementation details.
