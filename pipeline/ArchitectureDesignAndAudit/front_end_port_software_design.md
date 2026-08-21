# Software design — the front-end port converter

The software that reads the source list, parses each live file, and writes a
converted version of it. One run, no iteration, no commits.

Goal, unchanged: **the application live at https://chathealthy.ai is
preserved in every detail** against `Provider_v_4` and
`SpecialtyMetaData_v_4`.

---

## 1. Inputs and outputs

**Inputs**

| input | what it is |
|---|---|
| `front_end_port_scope.xlsx` | the source list — one row per file, with `Is live today`, `Provider direct`, `Specialty direct` and the breadcrumb |
| `front_end_port_map.json` | the field and query map (§4), a committed artefact separate from the code |
| the repository | read only; never written |

**Outputs**

| output | what it is |
|---|---|
| `_port_out/<original path>` | the converted version of every file it changes, in a shadow tree that mirrors the repository |
| `_port_out/report.xlsx` | one row per site: file, line, rule applied, before, after |
| `_port_out/unmapped.xlsx` | one row per site that matches a changed field and no rule — the completeness failure list |
| exit code | `0` only when `unmapped` is empty |

**It never writes into the repository.** The converted files land in
`_port_out/`, so the whole run is reviewable as a diff and nothing can be
committed by accident. Promotion into the tree is a separate, deliberate act.

## 2. Why it parses instead of matching text

Rule-065-ENF-006 forbids regular expressions in executable `.py`, `.ts` and
`.tsx`. That rules out the usual codemod shortcut, and it is the right
constraint here anyway: `addresses` appears in comments, docstrings, log
messages and unrelated variables, and a text match would rewrite all of them.

- **Python** — `ast` locates every site precisely, and `ast` node positions
  (`lineno`, `col_offset`, `end_lineno`, `end_col_offset`) give the exact
  character span to replace. The rewrite is a slice assignment on the source
  text, so formatting, comments and blank lines elsewhere are untouched.
- **TypeScript / TSX** — no AST library is available without adding a
  dependency, so these files are handled by **exact-span replacement with a
  declared expected count**: the map states how many occurrences each rule
  must find, and a mismatch aborts the file rather than guessing. Three `.tsx`
  files are in scope and they are read by a person as part of the run.

## 3. What it recognises

Python sites, located by AST node type rather than by spelling:

| site | node | example |
|---|---|---|
| dictionary key read | `Subscript` with a constant string | `p["addresses"]` |
| `.get()` read | `Call` to attribute `get` with a constant first argument | `p.get("addresses")` |
| dict literal key | `Dict` with a constant key | `{"addresses": {"$elemMatch": ...}}` |
| Mongo dotted path | `Constant` string containing a dot whose head is a mapped field | `"addresses.state"` |
| element predicate | `Compare` against a constant | `a.get("address_type") == "practice"` |
| aggregation stage | `Dict` whose single key is `$unwind`, `$match`, `$vectorSearch` | pipeline stages |

Each site carries its file, line, column span and the rule that claimed it.
A site that matches a mapped field but no rule goes to `unmapped.xlsx` — it
is never silently left alone.

## 4. The map

A committed JSON artefact, not code, so it can be reviewed on its own and
diffed when it changes.

```json
{
  "provider": {
    "field_reads": {
      "addresses": {"practice": "practice_addresses",
                    "business": "business_address"},
      "load_id": "run_id",
      "insurance[].brand": "insurance[].payer_name",
      "insurance[].raw_value": "insurance[].issuer_raw"
    },
    "derived": {
      "taxonomies[].primary": {"equals": ["code", "primary_taxonomy_code"]}
    },
    "query_paths": {
      "addresses.state": "practice_addresses.state",
      "addresses.city": "practice_addresses.city",
      "addresses.zip": "practice_addresses.zip",
      "addresses.county.name": "practice_addresses.county.name",
      "addresses.county.fips": "practice_addresses.county.fips"
    },
    "drop_predicates": ["address_type == 'practice'"],
    "delete": ["lat", "lng", "active", "taxonomies[].group"],
    "unconvertible": ["embedding", "is_disqualified"]
  },
  "specialty": {}
}
```

`delete` is the §6 list — capabilities that do not exist in the live data and
are removed rather than carried forward. `unconvertible` is the §7 list — the
converter refuses these and reports them; it does not invent a substitute.

The specialty map is empty: every field its readers touch is unchanged in
name and type, so the specialty port is the manifest binding alone.

## 5. The four query rewrites it will not do automatically

These change the *shape* of a query, not a name, and a wrong result looks
plausible rather than failing:

1. `{"addresses": {"$elemMatch": {"address_type": "practice", ...}}}` →
   `{"practice_addresses": {"$elemMatch": {...}}}` with the type clause gone
2. `$unwind: "$addresses"` + `$match: {"addresses.address_type": "practice"}`
   → `$unwind: "$practice_addresses"` with the type clause gone
3. `$vectorSearch.filter` on `addresses.state` / `addresses.address_type` →
   `practice_addresses.state`, **and the index must declare that filter path**
4. `{"taxonomies": {"$elemMatch": {"code": X, "primary": true}}}` →
   `{"primary_taxonomy_code": X}` — an element match becomes a document
   equality

The converter **locates all four, writes them to the report, and leaves the
source unchanged**, marking the file as requiring a hand edit. Guaranteeing
none is missed is the machine's job; choosing the replacement is not.

## 6. Capabilities deleted, not ported

Verified against the live collection before this design was written:

| apparent capability | evidence |
|---|---|
| `addr["lat"]`, `addr["lng"]` returned on every search result | 600 address elements sampled from `provider_v03`: neither key exists. Always `None` in production today. |
| `active[]` provider history | present on 0 of 4,000 sampled live documents |
| `taxonomies[].group` | on 48% of live Type 2 records; read by nothing |

Porting these forward would preserve the appearance of a capability that has
never worked. The converter removes the read and reports each removal.

## 7. What the converter refuses

- `embedding` — absent from every `Provider_v_4` document. Provider vector
  search cannot work until the pipeline produces vectors and the index is
  built. The converter reports each site and changes nothing.
- `is_disqualified` — absent from the provider, present on the specialty.
  Which record is authoritative is a decision, not a conversion.

A refusal is a row in the report, not a silent pass and not a guess.

## 8. Safety properties

| property | how it holds |
|---|---|
| the repository is never modified | output goes to `_port_out/`; the converter opens repository files read-only |
| nothing is committed | the run makes no git call of any kind |
| a partial run leaves nothing behind | files are written only after every rule for that file has been applied without error |
| every change is attributable | the report names file, line, rule, before and after for every edit |
| completeness is mechanical | a non-empty `unmapped.xlsx` fails the run, whatever the report says |
| re-running is safe | reading the repository and writing a fresh `_port_out/` each time; no state carried between runs |

## 9. Structure

```
_oneshots/port/
    run_port.py            entry point: --scope, --map, --out
    sites.py               AST location: source text -> [Site]
    rules.py               map -> rule objects; Site -> replacement or refusal
    rewrite.py             apply spans to source text, right to left
    report.py              the two workbooks
```

`rewrite.py` applies spans **right to left within each file**, so earlier
spans keep their offsets while later ones are replaced. Applying left to
right invalidates every subsequent position, which is the classic way a
codemod corrupts a file.

## 10. The run

```
python _oneshots/port/run_port.py \
    --scope pipeline/ArchitectureDesignAndAudit/front_end_port_scope.xlsx \
    --map   pipeline/ArchitectureDesignAndAudit/front_end_port_map.json \
    --out   _port_out
```

Acts on the **31 rows where `Is live today` is true**. Tests are converted in
a second pass by the same map once the source conversion is accepted — they
assert the old shape, so converting them first would hide whether the source
conversion is right.

After the run:

1. `unmapped.xlsx` empty, or the map is incomplete and the run does not count
2. `report.xlsx` read end to end — every edit is one row
3. `diff -r` the repository against `_port_out/` — the whole change, in one
   place, before anything moves
4. the four query rewrites (§5) done by hand against the report

No commits until you have signed off on that diff.
