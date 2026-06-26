// Fixture: violates REQ-B-003 - non-React .js carries business logic by
// branching on domain-vocabulary fields and reading domain-specific values.

function decide(payload) {
  if (payload.specialty && payload.specialty.code === "207R00000X") {
    return payload.provider.NPI;
  }
  if (payload.clinical_trial) {
    return payload.clinical_trial.id;
  }
  return null;
}
