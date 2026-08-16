# Advertio Ingest API — قرارداد سرویس مقصد برای Telclaw

**مخاطب:** توسعه‌دهنده‌ای که در انتهای pipeline خروجی Telclaw را به Advertio ارسال می‌کند.

**وضعیت سمت سرور:** ساخته و تست‌شده؛ این سند قرارداد integration است، نه پیشنهاد طراحی.

**آخرین به‌روزرسانی قرارداد:** ۲۰۲۶-۰۸-۰۶

> این integration در انتهای pipeline قرار دارد. Telclaw نباید قبل از تکمیل Collection، Cleaning، Normalization، Category/AI Processing و Validation، داده را به این API ارسال کند.

## 1. جایگاه Advertio در معماری Telclaw

```text
Telegram
   ↓
Collection
   ↓
Raw Storage
   ↓
Cleaning / Normalization
   ↓
Category / Rules
   ↓
AI Processing
   ↓
Validation
   ↓
Business Rules
   ↓
Advertio Ingest API
```

هدف این API دریافت **آگهی‌های پردازش‌شده و معتبر** از cohort کرال‌شده Telegram است.

هر آگهی این مسیر باید `supplySource = Crawled` داشته باشد. این cohort از آگهی‌های مستقیم کاربران جداست و نباید با مسیر `POST /api/leads` جایگزین شود.

### رفتار cohort

| | آگهی کاربر | آگهی Telclaw |
|---|---|---|
| تماس | تا ۲۰ سکه کسر می‌شود | هرگز سکه کسر نمی‌شود؛ کاربر به Telegram redirect می‌شود |
| متریک Gate A | شمرده می‌شود | کاملاً مستثنی |
| نمایش | بج تأیید | بج «از تلگرام» |

**NON-NEGOTIABLE:** Telclaw نباید آگهی واقعی ثبت‌شده توسط کاربر را از این integration ارسال کند.

---

## 2. احراز هویت

تمام درخواست‌ها باید هدر زیر را داشته باشند:

```http
X-Ingest-Key: <INGEST_API_KEY>
```

- کلید از تیم backend دریافت می‌شود.
- مقدار آن در سرور مقصد با `INGEST_API_KEY` نگهداری می‌شود.
- حداقل طول: ۳۲ کاراکتر.
- کلید نامعتبر یا نبود هدر: `401`.
- `404` در این integration به معنی این است که کلید روی سرور configure نشده و باید با تیم backend پیگیری شود.
- کلید نباید داخل source code، Git، log یا payload ذخیره شود.

این مسیر rate limit ندارد؛ برای جلوگیری از فشار غیرضروری، Telclaw باید concurrency ارسال را روی **۲ تا ۳** نگه دارد.

---

## 3. قرارداد سه‌مرحله‌ای

برای هر آگهی این ترتیب را رعایت کنید.

### Step 1 — Upload Media

اگر آگهی عکس دارد:

```http
POST /api/ingest/media?source=telegram-rent
Content-Type: multipart/form-data
X-Ingest-Key: <key>
```

فیلد multipart:

```text
file
```

پاسخ `200`:

```json
{
  "key": "leads/crawler-telegram-rent/9f2c1ab34d5e6f708192a3b4c5d6e7f8",
  "url": "https://cdn.advertio.ca/media/leads/crawler-telegram-rent/9f2c…/thumb.webp",
  "thumbUrl": "https://cdn.advertio.ca/media/leads/crawler-telegram-rent/9f2c…/thumb.webp"
}
```

فقط `key` را برای Step 2 نگه دارید. `url` و `thumbUrl` برای preview هستند و نباید به‌عنوان داده اصلی ذخیره شوند.

محدودیت‌ها:

| مورد | قانون |
|---|---|
| فرمت | JPEG / PNG / WebP؛ تشخیص بر اساس bytes سمت سرور |
| حجم | حداکثر ۸ MB |
| ابعاد | حداکثر ۵۰ MP |
| source | باید با `sourceName` در Step 2 یکسان باشد |
| EXIF/GPS | سمت سرور حذف می‌شود |

برای این cohort فقط thumbnail حدود ۴۸۰px ذخیره می‌شود؛ نسخه ۱۶۰۰px ساخته نمی‌شود.

### Step 2 — Create Lead

```http
POST /api/ingest/leads
Content-Type: application/json
X-Ingest-Key: <key>
```

نمونه:

```json
{
  "sourceName": "telegram-rent",
  "externalId": "4021",
  "sourceUrl": "https://t.me/some_channel/4021",
  "contactHandle": "@landlord",
  "title": "Bright 2-bed near Yonge & Eglinton",
  "description": "متن پست، تمیزشده",
  "categorySlug": "housing",
  "attributesJson": "{\"listing_type\":\"rent\",\"property_type\":\"apartment\",\"bedrooms\":\"2\",\"price\":2100}",
  "countryCode": "CA",
  "province": "Ontario",
  "city": "Toronto",
  "neighborhood": "Midtown",
  "mediaKeys": ["leads/crawler-telegram-rent/9f2c1ab34d5e6f708192a3b4c5d6e7f8"],
  "autoPublish": false
}
```

پاسخ‌ها:

| HTTP | وضعیت | معنی |
|---|---|---|
| `201` | `PendingReview` | ثبت شد و منتظر بررسی است |
| `201` | `Active` | ثبت و منتشر شد؛ فقط وقتی `autoPublish=true` باشد |
| `200` | `alreadyExisted=true` | قبلاً ثبت شده؛ این موفقیت است و نباید retry شود |
| `400` | validation error | خطای دائمی؛ retry نکنید |
| `401` | auth error | کلید نامعتبر/غایب |

### Step 3 — Remove Lead

وقتی پست اصلی دیگر موجود نیست، این مرحله **اجباری** است:

```http
DELETE /api/ingest/leads/telegram-rent/4021
X-Ingest-Key: <key>
```

پاسخ موفق: `204`؛ اگر وجود نداشته باشد `404` ممکن است برگردد.

برای غیرفعال‌کردن کل source:

```http
DELETE /api/ingest/sources/telegram-rent
X-Ingest-Key: <key>
```

پاسخ نمونه:

```json
{"deactivated":137}
```

---

## 4. Field Contract

| Field | Required | Rule |
|---|---|---|
| `sourceName` | Yes | regex `^[a-z0-9][a-z0-9-]{1,31}$`; 2–32 chars; باید ثابت بماند |
| `externalId` | Yes | شناسه پایدار پیام/پست؛ حداکثر ۲۰۰ کاراکتر؛ کلید idempotency |
| `sourceUrl` | Conditional | لینک پست اصلی؛ حداکثر ۵۰۰ کاراکتر |
| `contactHandle` | Conditional | Telegram handle؛ حداکثر ۶۴ کاراکتر |
| `title` | Yes | حداکثر ۲۰۰ کاراکتر |
| `description` | No | حداکثر ۲۰۰۰ کاراکتر |
| `categorySlug` | Yes | فعلاً `housing`; منبع زنده: `GET /api/categories` |
| `attributesJson` | Yes | **string حاوی JSON**، نه object |
| `countryCode` | Yes | فعلاً `CA` |
| `province` | Yes | نام یا کد استان |
| `city` | Yes | نام یا کد شهر |
| `neighborhood` | No | حداکثر ۱۰۰ کاراکتر |
| `mediaKeys` | No | حداکثر ۱۰ key؛ اولین key تصویر کارت فید است |
| `autoPublish` | No | پیش‌فرض `false` |

حداقل یکی از `sourceUrl` یا `contactHandle` باید وجود داشته باشد.

Redirect به این ترتیب انتخاب می‌شود:

1. `t.me/{contactHandle}` اگر `contactHandle` وجود داشته باشد.
2. در غیر این صورت `sourceUrl`.

### `autoPublish`

| `false` | `true` |
|---|---|
| `PendingReview` | `Active` |
| حالت پیش‌فرض و امن | فقط برای sourceای که کیفیتش قبلاً بررسی شده |

برای source جدید، crawler باید با `autoPublish=false` شروع کند.

---

## 5. Idempotency

کلید یکتایی:

```text
(sourceName, externalId)
```

`externalId` باید شناسه پایدار پیام Telegram باشد؛ **hash متن استفاده نکنید**.

اجرای دوباره crawler روی همان پیام:

```text
200
alreadyExisted: true
```

این پاسخ **موفقیت** است، نه error؛ لاگ error و retry نکنید.

Lead موجود به‌صورت خودکار update نمی‌شود و اگر قبلاً با Step 3 خاموش شده باشد، recrawl نباید دوباره آن را فعال کند.

---

## 6. Error Handling

### خطاهای دائمی — retry نکنید

تمام `400`ها دائمی محسوب می‌شوند. نمونه‌ها:

- `sourceName` نامعتبر
- `externalId` خالی
- نبود `sourceUrl` و `contactHandle`
- category ناشناخته
- location نامعتبر
- attribute ناشناخته
- attribute اجباری مفقود
- بیش از ۱۰ عکس
- media متعلق به source دیگر

### خطاهای قابل retry

`5xx`، timeout و خطاهای موقتی شبکه می‌توانند با backoff مناسب retry شوند.

برای delivery باید وضعیت هر job ثبت شود:

```text
PENDING → SENDING → SENT
                  ↘ FAILED → RETRY
```

Retry باید idempotent باشد تا ارسال مجدد باعث duplicate نشود.

---

## 7. `attributesJson`

این فیلد یک **string containing JSON** است.

درست:

```json
"attributesJson": "{\"listing_type\":\"rent\",\"price\":2100}"
```

غلط:

```json
"attributesJson": {
  "listing_type": "rent",
  "price": 2100
}
```

### Required attributes برای `housing`

| Key | Type | Values |
|---|---|---|
| `listing_type` | select | `rent`, `roommate` |
| `property_type` | select | `apartment`, `condo`, `basement`, `studio`, `room`, `house` |
| `bedrooms` | select | `0`, `1`, `2`, `3`, `4+` به‌صورت string |
| `price` | number | ماهانه CAD؛ ۱۰۰ تا ۱۰٬۰۰۰ |

اگر یک required attribute از متن قابل استخراج نیست، **حدس نزنید؛ رکورد را برای ارسال رد کنید.**

### Optional attributes

- `furnishing`: `furnished`, `unfurnished`, `partially`
- `rental_duration`: `daily`, `short_term`, `long_term`
- `area`: number؛ ۵ تا ۵۰۰ مترمربع
- `bathrooms_count`: number؛ ۱ تا ۱۰ با گام ۰.۵
- `floor_number`: number؛ ۰ تا ۱۰۰
- `pets_allowed`: boolean
- `smoking_allowed`: boolean
- `is_owner`: boolean
- `available_from`: `YYYY-MM-DD`
- `amenities`: array، مانند `elevator`, `parking`, `balcony`, `gym`
- `gender_preference`: برای roommate؛ `male`, `female`, `family`, `any`

فهرست زنده category/attributes مرجع است:

```text
GET /api/categories
GET /api/categories/{id}/attributes
```

اگر این سند و endpoint زنده اختلاف داشتند، **endpoint مرجع است**.

---

## 8. Location Contract

مکان از catalog مرجع resolve می‌شود:

```text
GET /api/locations/countries
GET /api/locations/provinces?countryCode=CA
GET /api/locations/cities?countryCode=CA&province=ON
```

قواعد:

- شهر باید زیر استان/کشور درست باشد.
- نام یا code قابل قبول است.
- مقدار ذخیره‌شده canonical است؛ مثلاً `toronto` و `Toronto` هر دو `Toronto` می‌شوند.
- location ناشناخته باعث `400` می‌شود و نباید retry شود.
- فعلاً seed روی Canada و تمرکز Toronto/اطراف آن است.

---

## 9. Volume & Concurrency

حجم مورد انتظار حدود ۵۰۰۰ آگهی در ماه و حدود ۲۰٬۰۰۰ درخواست با احتساب media است.

با اینکه API rate limit ندارد، Telclaw باید concurrency را روی **۲ تا ۳** محدود کند، چون upload عکس CPU/IO مصرف می‌کند و سرویس مقصد با database/storage مشترک است.

---

## 10. Implementation Checklist

- [ ] `sourceName` ثابت و مستند شده است.
- [ ] `externalId` از ID پایدار Telegram می‌آید، نه hash متن.
- [ ] حداقل یکی از `contactHandle` / `sourceUrl` وجود دارد.
- [ ] `alreadyExisted=true` به‌عنوان موفقیت پردازش می‌شود.
- [ ] `400` retry نمی‌شود.
- [ ] `5xx` و خطاهای transient با backoff retry می‌شوند.
- [ ] پس از حذف پست اصلی، DELETE اجرا می‌شود.
- [ ] required attributes حدس زده نمی‌شوند.
- [ ] concurrency روی ۲–۳ است.
- [ ] source جدید با `autoPublish=false` شروع می‌شود.
- [ ] API key فقط در environment/secret manager است.
- [ ] نتیجه delivery در SQLite قابل trace است.

---

## 11. Example

```bash
KEY="…"
BASE="https://api.advertio.ca"
SOURCE="telegram-rent"

# 1. Upload media
PHOTO=$(curl -sS -X POST "$BASE/api/ingest/media?source=$SOURCE" \
  -H "X-Ingest-Key: $KEY" \
  -F "file=@photo.jpg" | jq -r .key)

# 2. Create lead
curl -sS -X POST "$BASE/api/ingest/leads" \
  -H "X-Ingest-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg key "$PHOTO" '{
    sourceName: "telegram-rent",
    externalId: "4021",
    sourceUrl: "https://t.me/some_channel/4021",
    contactHandle: "@landlord",
    title: "Bright 2-bed near Yonge & Eglinton",
    description: "دو خوابه، طبقه ۵، از اول ماه",
    categorySlug: "housing",
    attributesJson: ({listing_type:"rent", property_type:"apartment", bedrooms:"2", price:2100} | tostring),
    countryCode: "CA", province: "Ontario", city: "Toronto", neighborhood: "Midtown",
    mediaKeys: [$key],
    autoPublish: false
  }')"

# 3. Remove after the source post disappears
curl -sS -X DELETE "$BASE/api/ingest/leads/$SOURCE/4021" \
  -H "X-Ingest-Key: $KEY"
```

## Related documentation

- Architecture and roadmap: `README.md`
- This document is the downstream delivery contract for Advertio.
- Backend implementation/ADR references remain on the Advertio side and are not part of Telclaw's source code.
