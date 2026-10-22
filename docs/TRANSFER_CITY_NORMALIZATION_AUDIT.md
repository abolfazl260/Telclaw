# Transferlist city/country normalization audit — 2026-10-22

Source: a user-supplied CSV excerpt with **3,655 rows** and the six route columns
(`origin_city`, `origin_province`, `origin_country`,
`destination_city`, `destination_province`, `destination_country`).
This is a static sample, not an exhaustive audit of the production database.

## Seeded aliases

Rules are registered in the existing Back Office **Normalization** interface,
as `transferlist.origin_city` / `transferlist.destination_city` with
a matching `country_iso2` scope. Country-language variants are seeded
separately for `origin_country` / `destination_country`, so that city
matches can work after the country field becomes ISO2.

Examples, with observations across the two city columns of the CSV:

| Variants in the data | Canonical city | Observations |
|---|---|---:|
| Teheran, تهران, TEHRAN | Tehran (IR) | 82 |
| Guanzhou, گوانژوو | Guangzhou (CN) | 28 |
| Peking, Pekin | Beijing (CN) | 24 |
| Hanover | Hannover (DE) | 13 |
| Kalininigrad | Kaliningrad (RU) | 13 |
| Köln, Koln, کلن | Cologne (DE) | 8 |
| Nurnberg | Nuremberg (DE) | 6 |
| تورنتو | Toronto (CA) | 5 |
| Москва | Moscow (RU) | 1 |

Rules also cover other high-confidence examples, including `SPB` →
`St. Petersburg` (RU), `Ханчжоу` → `Hangzhou` (CN),
`هامبورگ` → `Hamburg` (DE), `لندن` → `London` (GB),
`Montréal` → `Montreal` (CA), `Toronto North York` →
`Toronto` (CA), and `Washington D.C.` → `Washington, D.C.` (US).

## Values excluded from automatic city mapping

- **A country in a city field**: `Iran`, `China`, `Germany`,
  `Turkey`, `Canada`, `Italy`, `USA`, etc. The country is known,
  but the city is not: `Iran` must never be rewritten as `Tehran`.
- **Several destinations in one field**: `Tehran/Mashhad`,
  `Karaj/Tehran`, `Munich and Stuttgart`, `Düsseldorf, Bochum`,
  `Turin/Milan`, `Toronto/Vancouver`, etc. Needs human selection
  or multi-city support, not a guessed single canonical city.
- **Places that are not cities**, e.g. `Maryland`, `Bavaria`,
  `California`, `Europe`, `Orange County`; do not substitute
  a nearby city.
- **Ambiguous, unsupported or apparently incorrect values**, including
  cross-country city/country combinations and blank/multi-country scopes.

A key operational finding is that `Iran`, `China`, and `Germany`
alone account for over a thousand city-field occurrences. Aliases do not
repair this semantic data-quality problem; it requires re-extraction or
manual correction based on original source messages. Do not map these
records to the country's capital by default.

## Rollout and reversibility

1. Back up the live SQLite database.
2. Deploy and restart Telclaw, then open **Back Office → Normalization →
   Cargo city/country rules**. Review country-scoped aliases before applying.
3. To update existing data, click **Apply cargo city/country rules to existing
   transferlist**. This uses `renormalize_transfer_cities_only()` and
   only updates the four country/city route fields and
   `transfer_locations` cache, in one transaction.
4. Future structured extractions automatically use the seeded rules. Existing
   custom aliases remain untouched on re-seeding (`INSERT OR IGNORE`).
5. This does **not** edit source messages, already-posted Telegram content,
   remote Advertio leads or ingestion status. Replaying already successful
   API deliveries is not part of normalization.

Re-running the cargo route-only backfill is idempotent. Full
`Re-normalize existing data` applies **all** existing alias categories/fields
and is a different, broader operation; use the route-only action for this audit.
