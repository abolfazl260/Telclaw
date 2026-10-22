"""Country-scoped, non-destructive transferlist normalization seed rules.

Reviewed against a 3,655-row origin/destination CSV exported 2026-10-22.
Only exact, unambiguous translations/transliterations and known spelling
variants are included. Never infer a city from a country/region, a multiple-
city string, a vague airport name, or a mismatched country.
Existing Back Office user overrides take precedence (INSERT OR IGNORE).
"""

# (country ISO2, source spelling, canonical city, explanation)
# Applied to *both* origin_city and destination_city; keep country scope.
TRANSFER_CITY_ALIASES = (
    ("IR", "Teheran", "Tehran", "German-style romanization"),
    ("IR", "تهران", "Tehran", "Persian endonym"),
    ("IR", "TEH", "Tehran", "Unambiguous Tehran code in IR-scoped city fields"),
    ("IR", "tehran", "Tehran", "Standardized ASCII capitalization"),
    ("IR", "Imam Khomeini", "Tehran", "Imam Khomeini International Airport in Tehran"),
    # The operator explicitly chose Tehran as the single canonical city.
    ("IR", "Tehran-Mashhad", "Tehran",
     "Operator-selected primary city; composite also mentions Mashhad"),
    ("IR", "Tehran/Karaj", "Tehran",
     "Operator-selected primary city; composite also mentions Karaj"),
    ("CN", "Guanzhou", "Guangzhou", "Misspelling"),
    ("CN", "گوانژوو", "Guangzhou", "Persian transliteration"),
    ("CN", "Peking", "Beijing", "Historical English name"),
    ("CN", "Pekin", "Beijing", "Alternate transliteration"),
    ("CN", "Bei jing", "Beijing", "Spacing variant"),
    ("CN", "Ханчжоу", "Hangzhou", "Russian transliteration"),
    ("CN", "Hangingzhou", "Hangzhou", "Misspelling"),
    ("CN", "Shenzen", "Shenzhen", "Misspelling"),
    ("DE", "Hanover", "Hannover", "German canonical spelling"),
    ("DE", "هانوفر", "Hannover", "Persian transliteration"),
    ("DE", "Köln", "Cologne", "German endonym"),
    ("DE", "Koln", "Cologne", "ASCII variant"),
    ("DE", "کلن", "Cologne", "Persian transliteration"),
    ("DE", "Nurnberg", "Nuremberg", "German ASCII transliteration"),
    ("DE", "Norneberg", "Nuremberg", "Misspelling"),
    ("DE", "Tubingen", "Tübingen", "German spelling"),
    ("DE", "هامبورگ", "Hamburg", "Persian transliteration"),
    ("DE", "اشتوتگارت", "Stuttgart", "Persian transliteration"),
    ("DE", "BERLIN", "Berlin", "Standard ASCII capitalization"),
    ("RU", "Kalininigrad", "Kaliningrad", "Misspelling"),
    ("RU", "Москва", "Moscow", "Russian endonym"),
    ("RU", "Sanct-Petersburg", "St. Petersburg", "Alternate spelling"),
    ("RU", "Saint Petersburg", "St. Petersburg", "Full spelling"),
    ("RU", "SPB", "St. Petersburg", "Local abbreviation within Russia"),
    ("RU", "Ekaterinburg", "Yekaterinburg", "Alternate romanization"),
    ("RU", "Tumen", "Tyumen", "Romanization variant"),
    # Operator-reviewed: choose Moscow as primary city for this composite.
    # This intentionally discards the second city for the structured field.
    ("RU", "Moscow/Naberezhnye Chelny", "Moscow",
     "Operator-selected primary city; composite also mentions Naberezhnye Chelny"),
    ("CA", "تورنتو", "Toronto", "Persian transliteration"),
    ("CA", "TORONTO", "Toronto", "Standard capitalization"),
    ("CA", "Montréal", "Montreal", "Accent/spelling variant"),
    ("CA", "Toronto North York", "Toronto", "North York is a district of Toronto"),
    ("TR", "Stambul", "Istanbul", "Historic transliteration"),
    ("TR", "istanbul", "Istanbul", "Standard capitalization"),
    ("GB", "لندن", "London", "Persian transliteration"),
    ("IT", "naples", "Naples", "Standard capitalization"),
    ("AE", "DUBAI", "Dubai", "Standard capitalization"),
    ("US", "Washington D.C.", "Washington, D.C.", "Punctuation variant"),
    # Russian text for Bali island/province (not an exact municipality).
    # Transliteration is still useful but must not infer a city/province code.
    ("", "Бали", "Bali", "Russian to English transliteration; Bali is an island/region"),
    # Not a geographic place: invalidate only the city fields.
    ("", "Air Tour", None, "Airline/travel phrase, not a city; map to SQL NULL"),
)

# This data has non-ISO country values too. Normalize the country BEFORE
# matching the scoped city rule so Persian city+country combinations work.
# Fields: transferlist.origin_country and .destination_country.
TRANSFER_COUNTRY_ALIASES = (
    ("Iran", "IR"), ("ایران", "IR"),
    ("Germany", "DE"), ("آلمان", "DE"),
    ("China", "CN"), ("چین", "CN"),
    ("Canada", "CA"), ("کانادا", "CA"),
    ("Turkey", "TR"), ("Türkiye", "TR"), ("ترکیه", "TR"),
    ("Russia", "RU"), ("روسیه", "RU"),
    ("Italy", "IT"), ("ایتالیا", "IT"),
    ("France", "FR"), ("فرانسه", "FR"),
    ("United States", "US"), ("USA", "US"), ("America", "US"),
    ("United Kingdom", "GB"), ("UK", "GB"),
    ("UAE", "AE"), ("United Arab Emirates", "AE"),
)

# Other multi-city values such as "Tehran/Mashhad", "Turin/Milan",
# "Munich and Stuttgart", and "Düsseldorf, Bochum" still require manual review.
# Exceptions ("Tehran-Mashhad", "Tehran/Karaj", "Moscow/Naberezhnye Chelny")
# deliberately keep only the operator-selected primary city for structured
# reporting; the original message text is retained for context.
# Never convert a country-as-city like "Iran" to Tehran, "China" to Beijing,
# "Germany" to Berlin, "Canada" to Toronto or "Maryland" to Washington.
# "Бали" normalizes its script to Bali, but Bali is an island/province, not
# a municipality. Do not infer a finer city from this spelling.
