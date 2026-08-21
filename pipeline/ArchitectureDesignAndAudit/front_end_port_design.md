# Design — porting the live front end onto the new data structures

One job: convert the live front-end code base so that every capability it has
today is preserved against `Provider_v_4` and `SpecialtyMetaData_v_4`.

Not new capability. Not data quality. Not an adapter that hides the new shape.
The data is the data; the code changes to read it.

**Nothing is preserved that does not work today.** A read that returns `None`
against the live collection is not a capability — it is a line of code, and
porting it forward carries the mistake into the new structure.

---

## 1. The method: map, then convert, then prove

**Map.** Every read the live code makes, expressed as `old path → new path`.
The map is data, exhaustive, and reviewable on its own — one table, not
prose.

**Convert.** Each mapped read is a mechanical edit at a known file and line.
The map drives the edit; the edit is not invented per site.

**Prove.** For each capability, a check that it produces the same *kind* of
answer against the new structure that it produces today against the old one.
Not the same values — the pipelines disagree on purpose — the same behaviour.

The unit of work is the **capability**, not the file. A capability that is
half-ported is broken, and files do not divide cleanly along capability lines.

## 2. The map

### 2.1 Provider — document level

| live code reads | new structure | note |
|---|---|---|
| `p["addresses"]` filtered `address_type == "practice"` | `p["practice_addresses"]` | every element is a practice address; the filter disappears |
| `p["addresses"]` filtered `address_type == "business"` | `p["business_address"]` | an object, not a list element |
| `p["addresses"]` unfiltered | `[p["business_address"], *p["practice_addresses"]]` | only where the code genuinely wants both |
| `t["primary"]` within `p["taxonomies"]` | `t["code"] == p["primary_taxonomy_code"]` | the flag moved up to the document |
| `p["insurance"][i]["brand"]` | `["payer_name"]` | |
| `p["insurance"][i]["raw_value"]` | `["issuer_raw"]` | |
| `p["load_id"]` | `p["run_id"]` | |
| `p["is_disqualified"]` | — | absent; see §5 |
| `p["embedding"]` | — | absent; see §5 |
| `p["chunk_id"]`, `p["row_index_in_chunk"]` | — | staging artefacts, nothing user-facing |
| `p["active"]` | — | never populated in the live data either; see §4 |
| `addr["lat"]`, `addr["lng"]` | — | **never existed**; see §4 |

Identity fields are unchanged and need no mapping: `npi`,
`entity_type_code`, `provider_first_name`, `provider_middle_name`,
`provider_last_name_legal_name`, `provider_name_prefix_text`,
`provider_credential_text`, `provider_organization_name_legal_business_name`.

Address sub-keys are unchanged: `line1`, `line2`, `city`, `state`, `zip`,
`phone`, `fax`, `country`, `county`.

### 2.2 Provider — query level

Query shape changes, not just field names. These are the edits most likely to
be wrong, because a mis-ported filter returns plausible providers rather than
failing.

| live query | new query |
|---|---|
| `{"addresses": {"$elemMatch": {"address_type": "practice", "state": S}}}` | `{"practice_addresses": {"$elemMatch": {"state": S}}}` |
| `$unwind: "$addresses"` then `$match: {"addresses.address_type": "practice", ...}` | `$unwind: "$practice_addresses"` then `$match` without the type clause |
| `$vectorSearch.filter: {"addresses.state": S, "addresses.address_type": "practice"}` | `{"practice_addresses.state": S}` — **and the index must declare that filter path** |
| `{"taxonomies": {"$elemMatch": {"code": ..., "primary": True}}}` | `{"primary_taxonomy_code": ...}` — an element match becomes a document-level equality |

### 2.3 Specialty

| live code reads | new structure |
|---|---|
| `Code`, `Display Name`, `Definition`, `Notes`, `Section`, `Grouping`, `Classification`, `Specialization`, `embedding` | unchanged, same names, same types |
| `record_number`, `version` | absent — read by nothing (verified repo-wide) |

The specialty port is therefore **the manifest binding and nothing else**.
Every reader already works against the new collection.

## 3. The conversion software

A codemod that applies §2 to the repository, rather than 84 hand edits.

```
port_front_end.py --map front_end_port_map.json --apply | --check
```

- **`--check`** reports every site the map matches, with file, line and the
  proposed replacement, and exits non-zero if any site matches no rule. That
  exit code is the completeness test: an unmapped read of a changed field is
  a hole in the map, not a judgement call for the person running it.
- **`--apply`** performs the edits.
- The map is a committed artefact. Re-running `--check` after `--apply` must
  report zero remaining sites.

**Why a tool rather than edits.** The same read appears in seven files in
slightly different spellings. A tool applies one decision everywhere; hands
apply seven decisions that agree until they don't. And `--check` is
re-runnable, so the port is verifiable at any point rather than at the end.

**What the tool will not do.** Rewrite a query pipeline (§2.2). Those four
shapes are structural rewrites, not substitutions; the tool flags each site
and a person changes it. The tool's job is to guarantee none is missed, not
to guess the replacement.

## 4. Capabilities that do not exist and are not ported

Verified against the live collection, not assumed:

| apparent capability | reality |
|---|---|
| provider latitude/longitude | `addr["lat"]` / `addr["lng"]` are read in `_format_provider` and returned on **every search result**. 600 address elements sampled from `provider_v03`: neither key exists. Always `None`, in production, today. |
| provider active/inactive history | `active[]` is read in three files. Present on 0 of 4,000 sampled live documents. |
| `taxonomies[].group` | present on 48% of live Type 2 records; read by nothing. |

These are deleted, not mapped. Carrying them forward would preserve the
appearance of a capability that has never worked.

## 5. Capabilities that exist today and cannot be preserved yet

These are the whole risk of the port, and none is solved by code:

| capability | why it stops |
|---|---|
| **provider vector search** | `Provider_v_4` has no `embedding` on any document, and no `provider_vector_index`. The pipeline must produce the vectors; the index must then be built with a `practice_addresses.state` filter path. |
| **disqualification filtering** | `is_disqualified` is absent from the provider and present on the specialty. Which record is authoritative is a decision. |
| **insurance display** | derives from `other_identifiers`, present on 1% of new records against 26% of live. The code ports cleanly; it will show less. |

The port can be finished and proven for everything else while these are
resolved. They are named here so that "the port is done" is never confused
with "search works".

## 6. Proving no capability was lost

Three checks, in order of strength.

**6.1 The map is complete.** `--check` exits zero: no read of a changed field
remains unmapped anywhere in the repository. Mechanical.

**6.2 Every capability answers.** For each user-facing capability — provider
search by state, by city, by county, by ZIP; specialty search; Provider
Detail render; insurance display; prescriber filtering — run it against the
new collections and assert it returns a well-formed answer of the right
shape. Not the same providers; the same *kind* of result.

**6.3 The tests port with the code.** The existing suites assert the live
shape (12 files). They are converted by the same map and must pass. A test
that cannot be mapped names a capability the map missed.

## 7. Files this port changes

Serving path — provider:

| file | lines touching changed structure |
|---|---|
| `FindCare/ProviderManagement/provider_search_service.py` | 23 |
| `FindCare/ProviderDetail/provider_detail_models.py` | 23 |
| `Code/.../find_care/provider_record_sync.py` | 35 |
| `FindCare/ProviderDetail/provider_detail_service.py` | 2 |
| `sharedServices/.../provider_detail_tool.py` | 5 |
| `Code/.../frontend/src/components/ProviderDetailWidget.tsx` | 5 |
| `ChatHealthyLib/src/chathealthy_lib/provider_embedding.py` | 10 |

Specialty: no code change. The binding moves in the manifest.

## 8. Dead code and dead declarations

Where the port finds a file whose only purpose is a capability that does not
exist (§4), the file is deleted rather than ported, and its declaration is
removed from `deployment_architecture.json` in the same change — so the
manifest stops shipping it to every target.

No file is deleted on suspicion. The test is: every read it performs returns
nothing against the live collection, and no other file calls it. Both are
checkable before anything is removed.

## 9. Order

1. Write the map (§2) as a committed artefact
2. Build the codemod with `--check` only (§3) — no edits, just the report
3. Close the map until `--check` reports zero unmapped sites
4. `--apply`, then hand-port the four query shapes (§2.2)
5. Port the tests by the same map (§6.3)
6. Delete what §4 found dead, and its manifest declarations (§8)
7. Move the specialty binding in the manifest — it needs nothing else
8. Provider binding follows once §5's embedding work lands

Steps 1–3 change no code and produce the evidence that the map is complete
before a single line is edited.
