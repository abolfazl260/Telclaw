# Integrations

External services are documented and implemented behind the `delivery/` boundary.

## Advertio

- [Advertio Ingest API contract](advertio-ingest-api.md)

The Advertio API is the final delivery stage. It must receive only validated business data; Telegram collection and AI processing must not depend on Advertio implementation details.
