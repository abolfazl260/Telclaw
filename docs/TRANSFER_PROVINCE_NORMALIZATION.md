# Transfer Cargo — Fill Missing Provinces / States

This feature extends the existing Back Office **Normalization** page and uses
the user-supplied `tranbs.csv` sample (3,655 transfer-route rows) as its
priority list. No separate province table or migration is required:
`transferlist.origin_province` and `transferlist.destination_province`
already exist.

## Standard naming and matching

The code in `storage/transfer_province_map.py` contains a vetted exact
**country ISO2 + city name → full English first-level administrative region**
mapping across countries present in the supplied dataset. Depending on
country, this is a province, state, federal city, region, governorate,
municipality or metropolitan county. Examples:

| Country | City after normalization | Province/state field |
|---|---|---|
| IR | Tehran | Tehran |
| IR | Mashhad | Razavi Khorasan |
| IR | Karaj | Alborz |
| CA | Toronto | Ontario |
| CA | Vancouver | British Columbia |
| CA | Montreal | Quebec |
| DE | Düsseldorf / Cologne | North Rhine-Westphalia |
| DE | Berlin | Berlin |
| DE | Frankfurt | Hesse |
| DE | Munich | Bavaria |
| CN | Guangzhou | Guangdong |
| CN | Beijing | Beijing |
| CN | Shanghai | Shanghai |
| RU | Kaliningrad | Kaliningrad Oblast |
| IT | Milan | Lombardy |
| IT | Rome | Lazio |
| AE | Dubai | Dubai |
| TR | Istanbul | Istanbul |
| US | Los Angeles | California |
| US | Dallas | Texas |
| AU | Sydney | New South Wales |

Full English names are used rather than short codes (e.g., `Ontario`, not
`ON`) to remain consistent with the existing `transferlist` schema and
the manually entered province values. Advertio API's separate canonical
`provinceCode` comes from its live location reference catalog and is
**not replaced by these descriptive names**.

Matching is **exact and country-scoped**. Existing bilingual city and
country aliases are applied to a temporary copy of each route before
matching. Unicode normalization handles accents/case; it does not perform
fuzzy geocoding. The map includes reviewed variants that occurred in the
CSV, including `تهران`, `تورنتو`, `Teheran`, `Guanzhou`, `Peking`,
`Köln`, and `Москва`.

No province is inferred when:
- the country is absent, invalid or inconsistent with the city,
- a city field contains a country (`Iran`, `Germany`, `China`),
- a city field lists multiple possibilities (`Tehran/Mashhad`,
  `Munich and Stuttgart`, `Toronto/Vancouver`),
- a location is ambiguous or unknown,
- a province already has a nonblank value.

Existing province values are **never overwritten**, even if apparently
incorrect. A preview count for conflicting values is available for human
review; repairing pre-existing wrong values is explicitly out of scope.

## New data and historical records

- **New AI-processed transferlist records:** Normalization automatically
  fills blank origin/destination provinces from confirmed country + city
  mappings, after city and country aliases are applied. Explicit existing
  province values remain unchanged.
- **Historical records:** in **Back Office → Normalization → Cargo Location
  Normalization**, click **Preview Changes** to see a read-only breakdown of
  country, city and missing-province corrections (and existing nonempty
  province mismatches). No scan is performed on the ordinary page load.
- Back up production SQLite and click **Apply Normalization**. One SQLite
  transaction normalizes the four origin/destination country and city fields
  **and** fills missing origin/destination provinces using the newly
  canonicalized route values. All six location fields are handled together;
  the process rolls back if any database write or transfer-location cache
  refresh fails.
- Other structured fields, raw source text, dates, prices, published posts
  and Advertio API delivery states remain unchanged. Re-running the operation
  is safe and idempotent. Existing nonempty province mismatches are counted
  for review but not overwritten.
- **Advanced Normalization** (collapsed by default) retains the broader
  all-categories operation. The previous route-only and province-only
  endpoints remain for backwards compatibility but no longer appear as
  separate primary controls.

The city/region reference map remains maintained in Python code, not guessed
by the Back Office UI. Extend it only after confirming the country+city
administrative relationship.

## Related material

- `docs/TRANSFER_CITY_NORMALIZATION_AUDIT.md` — city spelling corrections
  and unresolved ambiguities in the same CSV.
- `docs/ADVERTIO_CARGO_DELIVERY.md` — canonical city/province codes from
  the platform API and the differences from the local report.
