# Telclaw

## Project Structure

```text
Telclaw/
├── .github/
│   └── workflows/
│       └── branch-review.yml
├── ai/
│   ├── prompts/
│   │   ├── housinglist.txt
│   │   ├── joblist.txt
│   │   └── transferlist.txt
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── cloudflare.py
│   │   ├── factory.py
│   │   └── groq.py
│   ├── __init__.py
│   ├── ai_service.py
│   ├── category_classifier.py
│   ├── category_schemas.py
│   ├── classification_service.py
│   ├── database_analyst.py
│   ├── extraction_service.py
│   ├── extractor.py
│   ├── groq_connection_test.py
│   ├── groq_token_budget.py
│   ├── prompt_loader.py
│   ├── provider_cooldown.py
│   ├── provider_failover.py
│   ├── provider_manager.py
│   ├── rate_limiter.py
│   ├── run_ai_processing.py
│   ├── structured_output.py
│   └── text_source.py
├── collection/
│   ├── __init__.py
│   ├── collector_service.py
│   ├── crawler.py
│   └── media_downloader.py
├── delivery/
│   ├── __init__.py
│   ├── advertio_cargo.py
│   ├── advertio_client.py
│   ├── advertio_jobs.py
│   ├── advertio_service.py
│   ├── advertio_transport.py
│   ├── housing_retry.py
│   ├── jobs_discovery.py
│   ├── telegram_transfer_publisher.py
│   └── telegram_transfer.py
├── docs/
│   ├── integrations/
│   │   ├── advertio-ingest-api.md
│   │   └── README.md
│   ├── ADMIN_FORWARDED_IMPORT.md
│   ├── ADVERTIO_CARGO_DELIVERY.md
│   ├── ADVERTIO_INGEST_INTEGRATION.md
│   ├── ADVERTIO_JOBS_DELIVERY.md
│   ├── ADVERTIO_MANUAL_DELIVERY.md
│   ├── AI_CODING_CONTEXT.md
│   ├── AI_STRUCTURED_EXTRACTION.md
│   ├── ai-processing.md
│   ├── architecture.md
│   ├── BACKOFFICE_AI_ASSISTANT.md
│   ├── backoffice.md
│   ├── development-roadmap.md
│   ├── MCP_DATABASE.md
│   ├── REPOSITORY_INITIALIZATION_PERFORMANCE.md
│   ├── SYSTEM_STRUCTURE_MAP.md
│   ├── TEXT_LIFECYCLE_AUDIT.md
│   ├── TRANSFER_CITY_NORMALIZATION_AUDIT.md
│   └── TRANSFER_PROVINCE_NORMALIZATION.md
├── integrations/
│   ├── __init__.py
│   ├── database_reader.py
│   └── mcp_server.py
├── monitoring/
│   ├── aboutus.py
│   ├── admin_forward_ingest.py
│   ├── error_alerts.py
│   ├── source_formatter.py
│   ├── telegram_monitor.py
│   └── transfer_live.py
├── processing/
│   ├── __init__.py
│   ├── classifier.py
│   ├── cleaner.py
│   ├── contracts.py
│   ├── normalizer.py
│   ├── processing_service.py
│   ├── property_extractor.py
│   ├── run_processing.py
│   └── stages.py
├── scripts/
│   ├── audit_message_text_columns.py
│   ├── rebuild_transfer_locations.py
│   └── reconcile_message_text.py
├── services/
│   ├── __init__.py
│   ├── account_service.py
│   ├── channel_service.py
│   ├── crawl_job_service.py
│   ├── crawler_service.py
│   ├── health_metrics.py
│   ├── message_service.py
│   ├── processing_runner.py
│   ├── processing_service.py
│   ├── scheduler_service.py
│   └── stage_control.py
├── storage/
│   ├── __init__.py
│   ├── advertio_jobs_queue.py
│   ├── content_duplicate.py
│   ├── data_normalizer.py
│   ├── database.py
│   ├── location_normalizer.py
│   ├── message_repository.py
│   ├── transfer_city_aliases.py
│   └── transfer_province_map.py
├── TelegramProcessor/
│   ├── config/
│   │   └── categories.json
│   ├── modules/
│   │   ├── classifier.py
│   │   ├── cleaner.py
│   │   ├── duplicate_detector.py
│   │   ├── exporter.py
│   │   └── logger.py
│   ├── main.py
│   ├── Property Information.txt
│   └── requirements.txt
├── tests/
│   ├── fixtures/
│   │   └── advertio_jobs_schema.json
│   ├── conftest.py
│   ├── test_admin_error_notifications.py
│   ├── test_admin_forward_ingest.py
│   ├── test_advertio_automatic_retry.py
│   ├── test_advertio_cargo.py
│   ├── test_advertio_contract.py
│   ├── test_advertio_jobs.py
│   ├── test_advertio_media_cleanup.py
│   ├── test_advertio_retry_media.py
│   ├── test_ai_advertio_media_cleanup.py
│   ├── test_ai_category_classification.py
│   ├── test_ai_delivery_progress.py
│   ├── test_ai_extraction_fallback.py
│   ├── test_ai_extraction_normalization.py
│   ├── test_ai_media_downloader_wiring.py
│   ├── test_ai_null_normalization.py
│   ├── test_ai_provider_config.py
│   ├── test_ai_provider_failover_manager.py
│   ├── test_ai_provider_manager.py
│   ├── test_ai_shared_cooldown.py
│   ├── test_ai_status_migration.py
│   ├── test_ai_text_source.py
│   ├── test_album_media.py
│   ├── test_assistant_query_rows.py
│   ├── test_backoffice_account_registration.py
│   ├── test_backoffice_active_cargo.py
│   ├── test_backoffice_ai_provider_status.py
│   ├── test_backoffice_data.py
│   ├── test_backoffice_filtered_export.py
│   ├── test_backoffice_health.py
│   ├── test_backoffice_import.py
│   ├── test_backoffice_normalization.py
│   ├── test_backoffice_operations.py
│   ├── test_backoffice_rules.py
│   ├── test_backoffice_settings.py
│   ├── test_backoffice_text_edit_safety.py
│   ├── test_category_specific_extraction.py
│   ├── test_cloudflare_provider.py
│   ├── test_content_duplicate.py
│   ├── test_crawl_raw_text_contract.py
│   ├── test_crawler_forwarded_channel.py
│   ├── test_crawler_minimum_text.py
│   ├── test_crawler_reconnect.py
│   ├── test_data_normalization.py
│   ├── test_database_analyst.py
│   ├── test_database_mcp.py
│   ├── test_delivery_text_compatibility.py
│   ├── test_groq_429_recovery.py
│   ├── test_groq_token_budget.py
│   ├── test_health_metrics_consistency.py
│   ├── test_housing_retry_after.py
│   ├── test_jobs_compensation_policy.py
│   ├── test_jobs_discovery.py
│   ├── test_legacy_text_interfaces.py
│   ├── test_message_text_audit.py
│   ├── test_message_text_reconciliation.py
│   ├── test_processing_text_mirror.py
│   ├── test_provider_failover.py
│   ├── test_queue_recovery.py
│   ├── test_random_channel_crawl.py
│   ├── test_recover_missing_jobs.py
│   ├── test_repository_bootstrap_performance.py
│   ├── test_requeue_disabled_jobs.py
│   ├── test_source_rich_text.py
│   ├── test_stage_control.py
│   ├── test_structured_extraction.py
│   ├── test_telegram_aboutus.py
│   ├── test_telegram_database_download.py
│   ├── test_telegram_monitor_access.py
│   ├── test_terminal_q.py
│   ├── test_text_lifecycle_integration.py
│   ├── test_transfer_city_aliases.py
│   ├── test_transfer_country.py
│   ├── test_transfer_live_display.py
│   ├── test_transfer_province_fill.py
│   ├── test_transfer_title_publishing.py
│   └── test_transferlist_schema.py
├── .env.example
├── .gitignore
├── advertio_client.py
├── backoffice_active_cargo.py
├── backoffice_ai_providers_ui.py
├── backoffice_ai_providers.py
├── backoffice_assistant.py
├── backoffice_cargo_page.py
├── backoffice_data.py
├── backoffice_health.py
├── backoffice_import.py
├── backoffice_jobs.py
├── backoffice_layout.py
├── backoffice_operations_ui.py
├── backoffice_operations.py
├── backoffice_settings.py
├── backoffice_web.py
├── channels.json
├── config.py
├── database.py
├── error_handler.py
├── main.py
├── README.md
├── requirements-mcp.txt
├── requirements.txt
├── routed_publisher.py
├── routing_rules.py
├── scheduler.py
├── sessions_manager.py
├── system_ui.py
├── terminal_input.py
└── ui.py
```
