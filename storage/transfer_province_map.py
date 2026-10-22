"""Curated country+city -> first-level administrative region for transferlist.

Seeded from the 2026-10-22 user-supplied 3,655-record route CSV. English
administrative names use recognizable full province/state/region names.
Mappings are exact and country-scoped. No fuzzy match, geocoding guess, or
inference from a country name in the city column. Review changes to this
table before applying them to historical records.

Rows with missing/incorrect/ambiguous country or multiple-city locations
are intentionally not mapped. Existing nonblank province values are never
overwritten by this feature.
"""

# Country code -> province/state/region -> canonical cities (pipe separated).
# Cities listed here are actual cities from the CSV; variations in spelling
# and script are handled first by the existing Normalization alias system.
PROVINCE_CITY_GROUPS = {
    "IR": {
        "Tehran": "Tehran",
        "Razavi Khorasan": "Mashhad",
        "Fars": "Shiraz",
        "Isfahan": "Isfahan|Kashan",
        "East Azerbaijan": "Tabriz",
        "Alborz": "Karaj",
        "Gilan": "Rasht",
        "Kerman": "Kerman",
        "Markazi": "Arak",
        "Kermanshah": "Kermanshah",
        "Golestan": "Gorgan",
        "Khuzestan": "Ahvaz|Abadan",
        "Ardabil": "Ardabil",
        "Zanjan": "Zanjan",
        "West Azerbaijan": "Urmia",
        "Qazvin": "Qazvin",
        "Hormozgan": "Kish",
        "Kurdistan": "Sanandaj",
        "Hamadan": "Hamedan|Nahavand",
    },
    "CA": {
        "Ontario": "Toronto|Ottawa|London|Richmond Hill",
        "British Columbia": "Vancouver|North Vancouver",
        "Quebec": "Montreal|Quebec City",
        "Alberta": "Calgary",
        "Manitoba": "Winnipeg",
        "Saskatchewan": "Saskatoon",
        "Nova Scotia": "Halifax",
    },
    "DE": {
        "Berlin": "Berlin",
        "Hamburg": "Hamburg",
        "Hesse": "Frankfurt|Darmstadt",
        "North Rhine-Westphalia": "Düsseldorf|Cologne|Dortmund|Bochum|Wuppertal|Essen|Bergisch Gladbach|Kleve",
        "Baden-Württemberg": "Stuttgart|Karlsruhe|Tübingen|Tuttlingen|Rottweil|Biberach",
        "Bavaria": "Munich|Nuremberg|Regensburg|Hof",
        "Lower Saxony": "Hannover|Oldenburg|Wilhelmshaven",
        "Saxony": "Leipzig|Dresden|Meissen",
        "Schleswig-Holstein": "Kiel",
        "Mecklenburg-Western Pomerania": "Rostock",
        "Saarland": "Saarbrücken",
    },
    "CN": {
        "Beijing": "Beijing",
        "Shanghai": "Shanghai",
        "Guangdong": "Guangzhou|Shenzhen|Foshan",
        "Liaoning": "Dalian",
        "Zhejiang": "Hangzhou|Yiwu|Zhuji",
        "Henan": "Zhengzhou",
        "Hubei": "Wuhan",
        "Sichuan": "Chengdu",
        "Hainan": "Sanya",
        "Ningxia": "Yinchuan",
        "Gansu": "Lanzhou",
        "Anhui": "Hefei",
        "Jilin": "Jilin",
        "Shanxi": "Datong",
        "Shaanxi": "Xi'an",
    },
    "RU": {
        "Moscow": "Moscow",
        "Saint Petersburg": "St. Petersburg",
        "Kaliningrad Oblast": "Kaliningrad",
        "Novosibirsk Oblast": "Novosibirsk",
        "Irkutsk Oblast": "Irkutsk",
        "Krasnodar Krai": "Krasnodar|Sochi",
        "Sverdlovsk Oblast": "Yekaterinburg",
        "Krasnoyarsk Krai": "Krasnoyarsk",
        "Republic of Tatarstan": "Kazan",
        "Chelyabinsk Oblast": "Chelyabinsk",
        "Belgorod Oblast": "Gubkin",
        "Tyumen Oblast": "Tyumen",
    },
    "IT": {
        "Lazio": "Rome",
        "Lombardy": "Milan|Pavia|Bergamo",
        "Piedmont": "Turin",
        "Emilia-Romagna": "Bologna|Ferrara|Parma",
        "Veneto": "Padua|Venice",
        "Campania": "Naples",
        "Tuscany": "Florence",
        "Liguria": "Genoa",
    },
    "US": {
        "California": "Los Angeles|San Diego|San Francisco|Sacramento|Riverside",
        "Texas": "Dallas|San Antonio|Houston",
        "Washington": "Seattle",
        "New York": "New York",
        "Massachusetts": "Boston",
        "District of Columbia": "Washington, D.C.",
    },
    "AE": {"Dubai": "Dubai", "Abu Dhabi": "Abu Dhabi"},
    "TR": {
        "Istanbul": "Istanbul", "Ankara": "Ankara",
        "Antalya": "Antalya", "Izmir": "Izmir", "Van": "Van",
    },
    "AU": {
        "Victoria": "Melbourne", "New South Wales": "Sydney",
        "Queensland": "Brisbane", "South Australia": "Adelaide",
    },
    "AT": {"Vienna": "Vienna"},
    "GB": {
        "Greater London": "London",
        "Greater Manchester": "Manchester",
        "West Midlands": "Birmingham",
        "South Yorkshire": "Sheffield",
        "Hampshire": "Portsmouth",
        "Wales": "Cardiff",
    },
    "DK": {
        "Capital Region of Denmark": "Copenhagen|Rødovre",
        "Central Denmark Region": "Aarhus",
        "Region of Southern Denmark": "Odense",
        "North Denmark Region": "Aalborg",
    },
    "FR": {"Île-de-France": "Paris", "Nouvelle-Aquitaine": "Pau"},
    "ES": {
        "Community of Madrid": "Madrid",
        "Catalonia": "Barcelona",
        "Andalusia": "Malaga",
    },
    "NL": {"North Holland": "Amsterdam"},
    "SE": {"Stockholm County": "Stockholm"},
    "NO": {"Oslo": "Oslo"},
    "HU": {"Budapest": "Budapest"},
    "RS": {"City of Belgrade": "Belgrade|Novi Beograd", "Vojvodina": "Novi Sad"},
    "ME": {
        "Podgorica Municipality": "Podgorica",
        "Tivat Municipality": "Tivat",
        "Budva Municipality": "Budva",
        "Bar Municipality": "Bar",
        "Herceg Novi Municipality": "Herceg Novi",
    },
    "KG": {"Bishkek": "Bishkek", "Osh": "Osh"},
    "UZ": {"Tashkent": "Tashkent"},
    "KZ": {"Almaty": "Almaty", "Astana": "Astana"},
    "MY": {"Kuala Lumpur": "Kuala Lumpur"},
    "QA": {"Doha Municipality": "Doha"},
    "OM": {"Muscat Governorate": "Muscat"},
    "IQ": {
        "Basra Governorate": "Basra",
        "Sulaymaniyah Governorate": "Sulaimaniyah",
    },
    "UA": {"Odesa Oblast": "Odessa|Izmail"},
    "RO": {
        "Constanța County": "Constanta",
        "Bucharest": "Bucharest",
        "Galați County": "Galați",
    },
    "PL": {"Pomeranian Voivodeship": "Gdansk"},
    "BG": {
        "Varna Province": "Varna",
        "Burgas Province": "Burgas|Sunny Beach",
    },
    "AM": {"Yerevan": "Yerevan"},
    "AZ": {"Baku": "Baku"},
    "GE": {"Tbilisi": "Tbilisi"},
    "BY": {"Minsk": "Minsk", "Brest Region": "Brest"},
    "MD": {"Chișinău": "Kishinev"},
    "KR": {"Seoul": "Seoul", "Busan": "Busan", "Gwangju": "Gwangju"},
    "IN": {"Delhi": "Delhi"},
    "TH": {"Bangkok": "Bangkok"},
    "GR": {"Attica": "Athens"},
    "CH": {"Zürich": "Zürich"},
    "MX": {"Guanajuato": "Guanajuato"},
    "AF": {"Kabul": "Kabul"},
}

# Extra safe spelling variants visible in the CSV that are not necessarily
# covered by already configured custom aliases.
EXTRA_CITY_VARIANTS = {
    ("IR", "تهران"): "Tehran",
    ("IR", "Teheran"): "Tehran",
    ("IR", "TEH"): "Tehran",
    ("CN", "Guanzhou"): "Guangzhou",
    ("CN", "گوانژوو"): "Guangzhou",
    ("CN", "Peking"): "Beijing",
    ("CN", "Pekin"): "Beijing",
    ("CN", "Bei jing"): "Beijing",
    ("CN", "Shenzen"): "Shenzhen",
    ("CN", "Ханчжоу"): "Hangzhou",
    ("DE", "Hanover"): "Hannover",
    ("DE", "Köln"): "Cologne",
    ("DE", "Koln"): "Cologne",
    ("DE", "کلن"): "Cologne",
    ("DE", "Nurnberg"): "Nuremberg",
    ("DE", "Norneberg"): "Nuremberg",
    ("DE", "هانوفر"): "Hannover",
    ("DE", "هامبورگ"): "Hamburg",
    ("DE", "اشتوتگارت"): "Stuttgart",
    ("RU", "Kalininigrad"): "Kaliningrad",
    ("RU", "Москва"): "Moscow",
    ("RU", "SPB"): "St. Petersburg",
    ("RU", "Saint Petersburg"): "St. Petersburg",
    ("RU", "Sanct-Petersburg"): "St. Petersburg",
    ("RU", "Ekaterinburg"): "Yekaterinburg",
    ("RU", "Tumen"): "Tyumen",
    ("CA", "تورنتو"): "Toronto",
    ("CA", "Toronto North York"): "Toronto",
    ("CA", "Montréal"): "Montreal",
    ("TR", "Stambul"): "Istanbul",
    ("GB", "لندن"): "London",
    ("US", "Washington D.C."): "Washington, D.C.",
    ("IT", "naples"): "Naples",
    ("RO", "Galati"): "Galați",
    ("RO", "Galatz"): "Galați",
    ("UZ", "Toshkent"): "Tashkent",
    ("AT", "Wien"): "Vienna",
    ("PL", "Gdańsk"): "Gdansk",
    ("DE", "Saarbrucken"): "Saarbrücken",
}

def _normal_key(value):
    import unicodedata
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())

def _build_lookup():
    lookup = {}
    for country, subdivisions in PROVINCE_CITY_GROUPS.items():
        for province, city_string in subdivisions.items():
            for city in city_string.split("|"):
                key = (country, _normal_key(city))
                if key in lookup and lookup[key] != province:
                    raise ValueError("Conflicting city/province map: " + repr(key))
                lookup[key] = province
    for (country, spelling), canonical in EXTRA_CITY_VARIANTS.items():
        key = (country, _normal_key(canonical))
        if key not in lookup:
            raise ValueError("Alias refers to unmapped city: " + repr(key))
        lookup[(country, _normal_key(spelling))] = lookup[key]
    return lookup

PROVINCE_BY_COUNTRY_AND_CITY = _build_lookup()

def lookup_province(country, city):
    """Return a confidently mapped English province/state name, or None."""
    code = str(country or "").strip().upper()
    return PROVINCE_BY_COUNTRY_AND_CITY.get((code, _normal_key(city)))
