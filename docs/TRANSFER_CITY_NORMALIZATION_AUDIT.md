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

## Four additional operator-requested rules

The following five exact aliases implement the four corrections requested by
the operator. They are applied to **both** `origin_city` and
`destination_city` in the existing Normalization system, and affect new
structured records immediately. Country-specific rules are scoped to the
matching ISO country; the two text-only rules work regardless of country.

| Country scope | Original city value | Canonical city value | Reason |
|---|---|---|---|
| RU | `Moscow/Naberezhnye Chelny` | `Moscow` | User chose Moscow as primary, drops secondary city from structured field |
| IR | `Tehran-Mashhad` | `Tehran` | User chose Tehran as primary, drops Mashhad |
| IR | `Tehran/Karaj` | `Tehran` | User chose Tehran as primary, drops Karaj |
| Any | `Бали` | `Bali` | Russian-to-English spelling only; **Bali is an island/province, not a specified city** |
| Any | `Air Tour` | SQL `NULL` | Airline/tour text is not a city |

The primary-city exceptions intentionally discard alternative cities
**only from structured city columns**. Raw source messages and cleaned
descriptions stay intact; the system must not silently apply these choices
to other composite city strings. Existing operator-managed aliases have
precedence, and the defaults use `INSERT OR IGNORE`.

Bali's English spelling does not make it a verified city, so do **not** infer
a municipality, geolocation or province from the text alone. This rule
does not enable Advertio API delivery for an unsupported or ambiguous
location.

To correct existing entries, back up SQLite, deploy and restart
Telclaw, review aliases under **Normalization → Cargo city/country rules**
(load a page as needed), then use **Cargo Location Normalization →
Preview Changes → Apply Normalization**. This unified operation updates
origin/destination cities and countries, and fills missing provinces
atomically, not raw messages, dates, descriptions or prior delivery
states. The rule for `Bali` never infers a more specific municipality.

## Additional operator-reviewed mappings (October 22)

The following exact aliases are applied to both cargo city fields, except
the country rule which applies only to the **destination country**:

| Scope | Existing value | Canonical value |
|---|---|---|
| IR city | `Tehran(Karaj)` | `Tehran` |
| IR city | `Karaj/Tehran` | `Tehran` |
| IR city | `Tehran-Karaj` | `Tehran` |
| IR city | `Tehran -   Mashhad` | `Mashhad` |
| destination_country only | `IR or TR` | `IR` |

The spaced `Tehran -   Mashhad` expression is intentionally different from
the existing `Tehran-Mashhad` alias, which continues to map to **Tehran**.
This is an explicit operator decision, not a generic policy for multiple-city
routes. The country rule does **not** rewrite `origin_country`.

After deploying and backing up the SQLite database, use **Normalization →
Cargo Location Normalization → Preview Changes → Apply Normalization**.
The operation updates only country/city fields and missing provinces, keeping
original messages and delivery states unchanged.

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
3. Use **Preview Changes** first, then **Apply Normalization** in the
   unified Cargo Location Normalization section. Country and city aliases
   plus missing-province inference update the six location fields in one
   SQLite transaction; transfer location cache remains synchronized.
4. Future structured extractions automatically use the seeded rules. Existing
   custom aliases remain untouched on re-seeding (`INSERT OR IGNORE`).
5. This does **not** edit source messages, already-posted Telegram content,
   remote Advertio leads or ingestion status. Replaying already successful
   API deliveries is not part of normalization.

Re-running unified cargo location normalization is idempotent.
Full `Re-normalize existing data` remains under **Advanced Normalization**;
it applies other categories/fields as well and should not be used for this
cargo-specific operation.
