from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
# Produced by Gemini.
# Preserved as the reference parsing logic that the production script
# build_provider_v03_from_v02.py was rewritten from.

import re
import json

# Target dataset utilizing the updated schema with the nested carriers array
chat_healthy_records = [
    {
        "_id": {"$oid": "6a1d025d4cc71a8c0f56c278"},
        "npi": "1619974649",
        "taxonomies": [{"code": "225100000X", "primary": True}],
        "licenses": [{"state": "AK", "number": "833"}],
        "carriers": [
            {
                "carrier_name": "BLUE CROSS BLUE SHIELD MA",
                "carrier_id": "BCBSMA_001",
                "local_provider_network_id": "BC-99812A"
            },
            {
                "carrier_name": "AETNA HEALTH",
                "carrier_id": "AET_442",
                "local_provider_network_id": "AET-77162-P"
            }
        ],
        "other_identifiers": [
            {
                "identifier": "1029837",
                "type_code": "05",
                "type_description": "MEDICAID",
                "state": "AK"
            }
        ],
        "entity_type_code": "1",
        "provider_last_name_legal_name": "MCLEAN",
        "provider_first_name": "TINA"
    }
]

def clean_state_license(lic_number, state):
    """
    Applies conditional state-by-state parsing rules to clean license numbers
    and extract structural state professional prefixes.
    """
    clean_id = str(lic_number).strip()
    state = str(state).upper().strip()
    profession_prefix = "GENERIC"

    if state == "IL":
        if "." in clean_id:
            profession_prefix, clean_id = clean_id.split(".", 1)
        else:
            profession_prefix = "IL_GEN"

    elif state == "CA":
        match = re.match(r"^([a-zA-Z]+)(\d+)$", clean_id)
        if match:
            profession_prefix, clean_id = match.groups()

    elif state == "NY":
        clean_id = clean_id.zfill(6)
        profession_prefix = "NY_GEN"

    elif state == "TX":
        match = re.match(r"^([a-zA-Z])[- ]*(\d+)$", clean_id)
        if match:
            profession_prefix, clean_id = match.groups()
        else:
            profession_prefix = "TX_GEN"

    else:
        profession_prefix = f"{state}_GEN"

    return clean_id, profession_prefix

def parse_chat_healthy_provider_v2(provider_doc):
    """
    Parses a single provider document, mapping out licenses, carrier metrics,
    and fallback other_identifiers into flattened database rows.
    """
    npi = provider_doc.get("npi")
    last_name = provider_doc.get("provider_last_name_legal_name", "")
    first_name = provider_doc.get("provider_first_name", "")

    extracted_records = []
    seen_composite_keys = set()

    # Track 1: Process explicit licenses array
    for lic in provider_doc.get("licenses", []):
        state = lic.get("state")
        raw_number = lic.get("number")

        if state and raw_number:
            clean_num, prof_prefix = clean_state_license(raw_number, state)
            composite_key = f"{state}-{prof_prefix}-{clean_num}"
            seen_composite_keys.add(composite_key)

            extracted_records.append({
                "npi": npi,
                "provider_name": f"{last_name}, {first_name}",
                "record_source": "licenses_array",
                "identifier_type": "State License",
                "state": state,
                "cleaned_number": clean_num,
                "profession_prefix": prof_prefix,
                "composite_key": composite_key,
                "carrier_name": None,
                "carrier_id": None
            })

    # Track 2: Process new carriers array
    for carrier in provider_doc.get("carriers", []):
        c_name = carrier.get("carrier_name")
        c_id = carrier.get("carrier_id")
        net_id = carrier.get("local_provider_network_id")

        if net_id:
            composite_key = f"{c_id}-{net_id}"
            extracted_records.append({
                "npi": npi,
                "provider_name": f"{last_name}, {first_name}",
                "record_source": "carriers_array",
                "identifier_type": "Private Carrier Network ID",
                "state": "US",
                "cleaned_number": net_id,
                "profession_prefix": "CARRIER",
                "composite_key": composite_key,
                "carrier_name": c_name,
                "carrier_id": c_id
            })

    # Track 3: Process other_identifiers array with de-duplication rules
    for ident in provider_doc.get("other_identifiers", []):
        raw_id = ident.get("identifier")
        type_code = str(ident.get("type_code", "")).strip().zfill(2)
        desc = str(ident.get("type_description", "")).upper()
        state = ident.get("state", "US")

        if not raw_id:
            continue

        # Parse potential hidden licenses inside other_identifiers
        if type_code == "01" or "OTHER" in desc:
            clean_num, prof_prefix = clean_state_license(raw_id, state)
            composite_key = f"{state}-{prof_prefix}-{clean_num}"

            # De-duplication check against Track 1
            if composite_key in seen_composite_keys:
                continue
            seen_composite_keys.add(composite_key)

            extracted_records.append({
                "npi": npi,
                "provider_name": f"{last_name}, {first_name}",
                "record_source": "other_identifiers_array",
                "identifier_type": "State License (From Other)",
                "state": state,
                "cleaned_number": clean_num,
                "profession_prefix": prof_prefix,
                "composite_key": composite_key,
                "carrier_name": None,
                "carrier_id": None
            })

        # Parse explicit Medicaid listings
        elif type_code in ["02", "05"] or "MEDICAID" in desc:
            extracted_records.append({
                "npi": npi,
                "provider_name": f"{last_name}, {first_name}",
                "record_source": "other_identifiers_array",
                "identifier_type": "State Medicaid ID",
                "state": state,
                "cleaned_number": raw_id.strip(),
                "profession_prefix": "MEDICAID",
                "composite_key": f"{state}-MEDICAID-{raw_id.strip()}",
                "carrier_name": "State Medicaid Agency",
                "carrier_id": f"{state}_MED"
            })

        # Parse explicit Medicare UPIN codes
        elif type_code == "04" or "UPIN" in desc:
            extracted_records.append({
                "npi": npi,
                "provider_name": f"{last_name}, {first_name}",
                "record_source": "other_identifiers_array",
                "identifier_type": "Medicare UPIN",
                "state": "US",
                "cleaned_number": raw_id.strip(),
                "profession_prefix": "UPIN",
                "composite_key": f"US-UPIN-{raw_id.strip()}",
                "carrier_name": "CMS",
                "carrier_id": "CMS_UPIN"
            })

    return extracted_records

# Script Pipeline Entrypoint
if __name__ == "__main__":
    flat_spreadsheet_rows = []

    for doc in chat_healthy_records:
        parsed_rows = parse_chat_healthy_provider_v2(doc)
        flat_spreadsheet_rows.extend(parsed_rows)

    ChatHealthyLoggingService().info(json.dumps(flat_spreadsheet_rows, indent=2))
