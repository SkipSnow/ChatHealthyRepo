# Embedding Design — 2026-03-24

## Context

ChatHealthy FindCare — healthcare provider search index.
~8.4M provider records from the NPPES NPI Registry (CMS), enriched with county FIPS codes
by the FindCare DataPipelines county enrichment job.

Target store: MongoDB Atlas Vector Search.

Primary users: network managers and healthcare administrators.

## Implementation

- `Code/DataPipelines/provider_embedding.py` — `should_embed()`, `project()`, `render()`
- `Code/DataPipelines/tests/test_provider_embedding.py` — 41 unit tests, all passing
- `Code/Schemas/ChatHealthy.Providers.json` — source of truth for the provider record schema

---

## Record Filter — What to Embed

Embed all records **except**:

| Exclusion criterion | Field | Reason |
|---|---|---|
| No address at all | `county.reason = 'no_address'` | Nothing to anchor a location query |
| Inconsistent address | `county.reason = 'zip_state_mismatch'` | ZIP and state disagree — address is untrustworthy |

All other records embed regardless of county enrichment status.
County is one attribute among many — not a gate for inclusion.

**Included record types:**

| county.source / reason | Notes |
|---|---|
| `crosswalk_pass1` | Full geo, highest confidence |
| `geocoder_pass2_batch` | Full geo, Census Geocoder |
| `geocoder_pass3_billing` | Full geo, billing address fallback |
| `geocoder_pass4_maps` | Full geo, Google Maps |
| `geocoder_pass5_maps_billing` | Full geo, Google Maps billing fallback |
| `geocoder_failed` | Valid US address — county just couldn't be resolved |
| `out_of_scope / deactivated` | Valid address — NPI is inactive but record is searchable |
| `out_of_scope / foreign_provider` | Valid address — non-US provider |
| `out_of_scope / legacy` | Valid address — reason predates reason field |

---

## Field Projection

Embed **all content fields** except four system/pipeline internals:

| Excluded field | Reason |
|---|---|
| `_id` | MongoDB ObjectId — system |
| `load_id` | Pipeline run identifier — system |
| `record_id` | Worker sequence number — system |
| `worker_id` | Worker index — system |

---

## Derived Fields

Computed by `project()` before rendering. Not stored in the provider record.

| Field | Values | Description |
|---|---|---|
| `display_name` | string | Individual: `[prefix] first [middle] last [suffix], credential`. Org: legal business name. |
| `entity_type_label` | `individual` / `organization` | Human label for `entity_type_code`. |
| `primary_taxonomy_code` | string | Taxonomy code where `primary: true`. |
| `all_taxonomy_codes` | space-joined string | All taxonomy codes on the record. |
| `location_display` (practice) | string | `line1[, line2], city, state zip` |
| `mailing_location_display` | string / omitted | Same format. Omitted if identical to practice address. |
| `county_resolution_status` | `resolved` / `unresolved` / `excluded` | Whether `county.fips` was set. |
| `county_trust_tier` | `high` / `medium` / `low` / `excluded` | crosswalk = high; geocoder/maps = medium; geocoder_failed = low; out_of_scope = excluded. |
| `county_source_summary` | string | Human-readable enrichment method (e.g. "ZIP crosswalk (res_ratio: 0.99)"). |
| `npi_status` | `active` / `inactive` | Inactive when `npi_deactivation_date` set and `npi_reactivation_date` absent. |
| `foreign_provider_flag` | `yes` / `no` | `yes` when `practice_address.country` is not `US`. |
| `sole_proprietor_flag` | `yes` / `no` | Mapped from `is_sole_proprietor` Y/N. |
| `organization_subpart_flag` | `yes` / `no` | Mapped from `is_organization_subpart` Y/N. |
| `authorized_official` | string | Assembled: `first [middle] last[, credential] [— title]`. |
| `licenses` | string | Comma-joined: `GA #G12345, FL #F67890`. |
| `other_identifiers` | string | Semicolon-joined with type/state/issuer context. |

---

## Embedding Text Templates

Stable field order. Absent fields omitted entirely. No blank lines. No raw JSON.
Placeholder values (`<UNAVAIL>`) stripped before rendering.

### Individual (entity_type_code = '1')

```
record_type: provider
entity_type: individual
display_name: Dr. Jane A. Smith, M.D.
npi: 1144247073
npi_status: active
credential: M.D.
sex: F
sole_proprietor: no
primary_taxonomy_code: 207R00000X
all_taxonomy_codes: 207R00000X 207RC0000X
practice_address: 123 Peachtree St, Suite 400, Atlanta, GA 30301
mailing_address: PO Box 999, Atlanta, GA 30302
county_name: Fulton County
county_fips: 13121
county_resolution_status: resolved
county_trust_tier: high
county_source: ZIP crosswalk (res_ratio: 0.99)
foreign_provider: no
licenses: GA #G12345, FL #F67890
other_identifiers: G123 (04, GA); M456 (05, GA)
enumeration_date: 07/16/2006
last_update_date: 01/15/2024
certification_date: 01/15/2024
```

### Organization (entity_type_code = '2')

```
record_type: provider
entity_type: organization
display_name: MAYO CLINIC
organization_name: MAYO CLINIC
other_organization_name: Mayo Foundation
other_organization_name_type: 5
npi: 1215954557
npi_status: active
ein: 41-6011702
is_organization_subpart: yes
parent_organization_name: Mayo Foundation for Medical Education and Research
parent_organization_tin: 41-6011702
authorized_official: Robert T. Johnson, M.D. — CEO
primary_taxonomy_code: 282N00000X
all_taxonomy_codes: 282N00000X
practice_address: 200 First St SW, Rochester, MN 55905
county_name: Olmsted County
county_fips: 27109
county_resolution_status: resolved
county_trust_tier: medium
county_source: Census geocoder — practice address
foreign_provider: no
licenses: MN #MN001
enumeration_date: 04/01/2005
last_update_date: 03/01/2024
```

### Unresolved county (geocoder_failed)

```
record_type: provider
entity_type: individual
display_name: Carlos Rivera, D.O.
npi: 1234567890
npi_status: active
credential: D.O.
primary_taxonomy_code: 207P00000X
all_taxonomy_codes: 207P00000X
practice_address: 456 Rural Rd, Smalltown, TX 79901
county_resolution_status: unresolved
county_trust_tier: low
county_source: unresolved — all passes exhausted
foreign_provider: no
```

### Foreign provider (out_of_scope / foreign_provider — still embeds)

```
record_type: provider
entity_type: individual
display_name: Pierre Dubois
npi: 1987654321
npi_status: active
primary_taxonomy_code: 207R00000X
all_taxonomy_codes: 207R00000X
practice_address: 12 Rue de Paris, Paris, FR 75001
county_resolution_status: excluded
county_trust_tier: excluded
county_source: excluded (foreign_provider)
county_reason: foreign_provider
foreign_provider: yes
```

---

## Non-Goals (Alpha/MVP)

- No multiple embeddings (one projection, all users)
- No consumer vs. enterprise split
- No schema changes to the provider record
- No raw JSON embedding
- `authorized_official_telephone_number` excluded — contact info, not a search signal

---

## Open Questions for GPT

1. What embedding model do you recommend for this use case and scale (~8.4M records)?
2. Any fields worth repeating for stronger signal (e.g. NPI, taxonomy code)?
3. Should `npi_status: inactive` and `foreign_provider: yes` records be in a separate
   Atlas Search index or filtered at query time?
