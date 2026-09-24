# shellcheck shell=bash disable=SC2034  # sourced: the scripts use these
# Shared by every script in this folder, so every model is asked the same thing:
# the same passage, the same claims, and the same verdicts defined in the same words
# (questions.json). Sourced, not run.

: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY first}"
BASE_URL="${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUESTIONS="$HERE/questions.json"
OUT="$HERE/out"
mkdir -p "$OUT"

say() { printf '\n== %s\n' "$*"; }
# Milliseconds since the epoch. macOS date has no %N, so ask perl, which both have.
now_ms() { perl -MTime::HiRes=time -e 'printf "%d\n", time * 1000'; }

# Written for this test, not taken from a real page.
PASSAGE='In its 2025 annual report the operator said average journey times on the
line fell from 42 to 35 minutes after the new signalling system went live in March,
while ridership rose by 12% over the same period.'

# One claim per verdict a person can write by hand. CASES[i] is the verdict we expect
# for CLAIMS[i]. (Parallel arrays: macOS ships bash 3.2, which has no maps.)
CASES=(supports partly not_supported contradicts)
CLAIMS=(
  "Journey times on the line fell by about 7 minutes after the new signalling went live."
  "Journey times fell by 7 minutes after the new signalling went live, and fares were cut."
  "The new signalling system was supplied by a French manufacturer."
  "Journey times on the line rose after the new signalling went live."
)

# Every arm writes its answer to out/<arm>-<case>.answer.json in one shape, so the
# answers can be laid side by side whichever model gave them:
#   { arm, model, verdict, probabilities: {verdict: p}, supports: p, ms, cost }
