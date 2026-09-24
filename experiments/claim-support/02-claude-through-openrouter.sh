#!/usr/bin/env bash
# Step 1b of the claim-support experiment (plans/claim-support-experiment.md).
#
# Question: asked exactly what Jev was asked in 01, what do Haiku and Sonnet say, how
# long do they take, and what does it cost?
#
# Claude is a text model, so the question goes in a prompt and the answer comes back
# as JSON held to a schema. The prompt and the schema are both built from
# questions.json, the file Jev's typed questions come from, so every model reads the
# same verdicts defined in the same words.
#
# One difference that cannot be removed: Claude's API gives no token probabilities
# (OpenRouter lists no "logprobs" for either model), so Claude's probabilities are the
# ones it states in its answer. Jev's come from the model itself. Whether a stated
# probability means as much as a measured one is what the calibration part of the
# experiment is for.
#
# Arms:
#   haiku         Haiku 4.5, reasoning off
#   sonnet        Sonnet 5, reasoning off
#   sonnet-think  Sonnet 5, reasoning on (high effort). Included because the vendor's
#                 demo compared Jev against reasoning-off models only.
#
# Sonnet 5's reasoning is adaptive: switched on, the model still decides whether to
# think, and effort only sets how far it may go. It cannot be made to think. On the
# first run (2026-09-24, effort medium) it thought about none of the four claims: 0
# reasoning tokens, the same ~70 output tokens and the same latency as reasoning off.
# So the script records reasoning tokens for every answer and says when a reasoning
# arm did not reason, rather than letting "sonnet-think" stand for thinking it did
# not do.
#
# At the end it lays every answer in ./out/ side by side, Jev's included if 01 has
# been run.
#
# Usage:
#   export OPENROUTER_API_KEY=sk-or-...
#   ./02-claude-through-openrouter.sh
#   ARMS="haiku sonnet" ./02-claude-through-openrouter.sh   # a subset
#
# Needs: curl, jq, perl.

set -euo pipefail
source "$(dirname "$0")/common.sh"

ARMS="${ARMS:-haiku sonnet sonnet-think}"

model_for() {
  case "$1" in
    haiku)               echo "anthropic/claude-haiku-4.5" ;;
    sonnet|sonnet-think) echo "anthropic/claude-sonnet-5" ;;
    *) echo "unknown arm: $1" >&2; exit 1 ;;
  esac
}

# OpenRouter's reasoning switch. Off is said out loud rather than left to each
# model's default, so a model that reasons by default does not do it unasked.
reasoning_for() {
  case "$1" in
    sonnet-think) echo '{"effort": "high"}' ;;
    *)            echo '{"enabled": false}' ;;
  esac
}

# ---------------------------------------------------------------------------
# 1. The prompt and the schema, from questions.json
# ---------------------------------------------------------------------------

SYSTEM="$(jq -r '
  "You check citations in research reports. You get a claim from a report and the text",
  "of the page the report cites for it. Judge only from the page text given.",
  "",
  "Question 1, verdict: \(.verdict.instructions) Pick one:",
  (.verdict.criteria | to_entries[] | "- \(.key): \(.value)"),
  "Give a probability for every verdict. They sum to 1.",
  "",
  "Question 2, supports: \(.supports.instructions)",
  "- yes: \(.supports.criteria.true)",
  "- no: \(.supports.criteria.false)",
  "Give the probability of yes.",
  "",
  "Answer each question on its own."' "$QUESTIONS")"

SCHEMA="$(jq '
  (.verdict.criteria | keys_unsorted) as $verdicts
  | {
      type: "object",
      properties: {
        verdict: { type: "string", enum: $verdicts },
        probabilities: {
          type: "object",
          properties: ($verdicts | map({ (.): { type: "number" } }) | add),
          required: $verdicts,
          additionalProperties: false
        },
        supports: { type: "number", description: "Probability of yes, 0 to 1." }
      },
      required: ["verdict", "probabilities", "supports"],
      additionalProperties: false
    }' "$QUESTIONS")"

echo "$SYSTEM" > "$OUT/claude-system-prompt.txt"
echo "$SCHEMA" > "$OUT/claude-schema.json"

# ---------------------------------------------------------------------------
# 2. Ask each arm about the four claims
# ---------------------------------------------------------------------------

ask() {
  local arm="$1" name="$2" claim="$3"
  local base="$OUT/$arm-$name"
  local body
  body="$(jq -n \
    --arg model "$(model_for "$arm")" \
    --arg system "$SYSTEM" \
    --arg claim "$claim" \
    --arg page "$PASSAGE" \
    --argjson schema "$SCHEMA" \
    --argjson reasoning "$(reasoning_for "$arm")" '
    {
      model: $model,
      messages: [
        { role: "system", content: $system },
        { role: "user",
          content: ("<claim>\n" + $claim + "\n</claim>\n\n<page>\n" + $page + "\n</page>") }
      ],
      response_format: {
        type: "json_schema",
        json_schema: { name: "claim_support", strict: true, schema: $schema }
      },
      reasoning: $reasoning,
      max_tokens: 4000,
      provider: { require_parameters: true }
    }')"
  # require_parameters: refuse rather than quietly drop the schema or the reasoning
  # switch, so an answer that comes back was asked the way we meant.

  echo "$body" > "$base.request.json"

  local started ended
  started="$(now_ms)"
  curl -sS "$BASE_URL/chat/completions" \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$body" \
    > "$base.response.json"
  ended="$(now_ms)"

  say "2. $arm, expected: $name"
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
  jq --arg arm "$arm" --argjson ms "$(( ended - started ))" '
    (.choices[0].message.content | fromjson) as $a
    | {
        arm: $arm,
        model,
        verdict: $a.verdict,
        probabilities: $a.probabilities,
        supports: $a.supports,
        ms: $ms,
        cost: .usage.cost,
        reasoning_tokens: (.usage.completion_tokens_details.reasoning_tokens // 0)
      }' "$base.response.json" > "$base.answer.json"

  jq -r '
    "Model:      \(.model)",
    "Verdict:    \(.choices[0].message.content | fromjson | .verdict)",
    "  by verdict: \(.choices[0].message.content | fromjson | .probabilities
                     | to_entries | map("\(.key) \(.value)") | join(", "))",
    "Supports:   P(yes) = \(.choices[0].message.content | fromjson | .supports)",
    "Reasoning:  \(.usage.completion_tokens_details.reasoning_tokens // 0) tokens",
    "Usage:      \(.usage | tojson)"' "$base.response.json"

  if [[ "$arm" == *-think ]] && jq -e '.reasoning_tokens == 0' "$base.answer.json" > /dev/null; then
    echo "Note:       reasoning was on, and the model chose not to think about this one."
  fi
}

for arm in $ARMS; do
  for i in "${!CASES[@]}"; do
    ask "$arm" "${CASES[$i]}" "${CLAIMS[$i]}"
  done
done

# ---------------------------------------------------------------------------
# 4. Side by side
# ---------------------------------------------------------------------------
# Every answer in out/, whichever script or run wrote it, not only the arms asked
# this time. "ok" when the verdict is the one the claim was written for.

say "4. Side by side"
printf '%-14s %-13s %-14s %-3s %7s %8s %11s %9s\n' \
  "expected" "arm" "verdict" "" "P(yes)" "ms" "cost \$" "thought"
for i in "${!CASES[@]}"; do
  name="${CASES[$i]}"
  for arm in jev haiku sonnet sonnet-think; do
    f="$OUT/$arm-$name.answer.json"
    [[ -f "$f" ]] || continue
    jq -r --arg expected "$name" '
      [ $expected, .arm, .verdict,
        (if .verdict == $expected then "ok" else "--" end),
        (.supports | tostring), (.ms | tostring),
        (if .cost == null then "?" else (.cost | tostring) end),
        (if .arm == "jev" then "-" else (.reasoning_tokens // 0 | tostring) end) ]
      | @tsv' "$f" |
    while IFS=$'\t' read -r e a v ok p ms c t; do
      printf '%-14s %-13s %-14s %-3s %7s %8s %11s %9s\n' "$e" "$a" "$v" "$ok" "$p" "$ms" "$c" "$t"
    done
  done
done

say "Done. Raw requests and responses are in $OUT/"
