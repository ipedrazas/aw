#!/usr/bin/env bash
# Step 1 of the claim-support experiment (plans/claim-support-experiment.md).
#
# Question: can we reach Jev through OpenRouter, and does its probability come back?
# The whole comparison leans on Jev's calibrated probabilities, so if the gateway
# drops them we call TypeSafe's API directly instead.
#
# First run (2026-09-24) asked through chat/completions, the way every other model is
# asked, and the gateway refused: "typesafe/jev-1.13 is a decisions model and cannot
# be used with the chat/completions endpoint. Use the /api/alpha/decisions endpoint
# instead." Jev takes no prompt and writes no text, so it has its own endpoint: we send
# the state (claim and page) and typed questions, and get typed answers back.
#
# What it does, in order:
#   1. Reads what OpenRouter says about Jev: its input limit, its price, and which
#      request parameters it passes through.
#   2. Asks Jev about the four claims in common.sh, one for each verdict we can write
#      by hand: supported, partly supported, not supported, contradicted.
#   3. Prints each answer: the verdict, the probability of every verdict, and the
#      yes/no probability that the page supports the claim.
#
# Every raw response is kept in ./out/ so the article can quote what actually came
# back rather than what we expected.
#
# Usage:
#   export OPENROUTER_API_KEY=sk-or-...
#   ./01-jev-through-openrouter.sh
#   JEV_MODEL=typesafe/jev-1.14 ./01-jev-through-openrouter.sh   # another version
#
# Needs: curl, jq, perl.

set -euo pipefail
source "$(dirname "$0")/common.sh"

JEV_MODEL="${JEV_MODEL:-typesafe/jev-1.13}"
# The Decisions endpoint sits beside v1, not under it.
DECISIONS_URL="${BASE_URL%/v1}/alpha/decisions"

# ---------------------------------------------------------------------------
# 1. What the gateway says about Jev
# ---------------------------------------------------------------------------
# Jev is not in the general model list (checked 2026-09-24), but its own endpoint
# page is public. It lists no supported parameters, because none of the chat
# parameters apply: Jev is asked through the Decisions endpoint, below.

say "1. What OpenRouter says about $JEV_MODEL"

curl -sS "$BASE_URL/models/$JEV_MODEL/endpoints" \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  > "$OUT/jev-endpoints.json"

jq '.data | {id, name, endpoints: [.endpoints[]
     | {provider_name, context_length, max_completion_tokens,
        supported_parameters, pricing}]}' "$OUT/jev-endpoints.json"

# ---------------------------------------------------------------------------
# 2. Ask Jev about four claims
# ---------------------------------------------------------------------------
# The shape the check_support step will use. The state is what Jev looks at: the
# claim and the page. The questions (questions.json) are typed, and all are answered
# in parallel without seeing each other's answers:
#   verdict   a choice among our five verdicts, with a probability for each
#   supports  a yes/no ("noul"), answered with the probability of yes

ask() {
  local name="$1" claim="$2"
  local base="$OUT/jev-$name"
  local body
  body="$(jq -n \
    --arg model "$JEV_MODEL" \
    --arg claim "$claim" \
    --arg page "$PASSAGE" \
    --slurpfile q "$QUESTIONS" '
    {
      model: $model,
      state: { claim: $claim, page: $page },
      questions: {
        verdict:  ({ type: "choice" } + $q[0].verdict),
        supports: ({ type: "noul" }   + $q[0].supports)
      }
    }')"

  echo "$body" > "$base.request.json"

  local started ended
  started="$(now_ms)"
  curl -sS "$DECISIONS_URL" \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$body" \
    > "$base.response.json"
  ended="$(now_ms)"

  say "2. Expected: $name"
  echo "Claim:      $claim"
  echo "Round trip: $(( ended - started )) ms"

  if jq -e '.error' "$base.response.json" > /dev/null; then
    echo "The gateway answered with an error:"
    jq '.error' "$base.response.json"
    return
  fi

  # -------------------------------------------------------------------------
  # 3. What came back
  # -------------------------------------------------------------------------
  jq --argjson ms "$(( ended - started ))" '{
      arm: "jev",
      model,
      verdict: .answers.verdict.choice,
      probabilities: .answers.verdict.probabilities,
      supports: .answers.supports.noul,
      ms: $ms,
      cost: .usage.cost
    }' "$base.response.json" > "$base.answer.json"

  jq -r '
    "Model:      \(.model)",
    "Verdict:    \(.answers.verdict.choice) (confidence \(.answers.verdict.confidence))",
    "  by verdict: \(.answers.verdict.probabilities | to_entries
                     | map("\(.key) \(.value)") | join(", "))",
    "Supports:   P(yes) = \(.answers.supports.noul)",
    "Usage:      \(.usage | tojson)"' "$base.response.json"
}

for i in "${!CASES[@]}"; do
  ask "${CASES[$i]}" "${CLAIMS[$i]}"
done

say "Done. Raw requests and responses are in $OUT/"
