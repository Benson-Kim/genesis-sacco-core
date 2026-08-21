#!/usr/bin/env bash
# Mobile guard sweeps — the P17 §9 gates, in one script so the GitLab and
# GitHub pipelines cannot drift apart on what "green" means.
#
# Four sweeps, each with a falsifiability fixture under mobile/scripts/
# fixtures/ proving the sweep actually catches what it claims to catch. A gate
# that never fires is indistinguishable from no gate.
#
#   1. no client money math      (gate 1.1 / P15)
#   2. no staff API paths        (FM-H, principal confusion)
#   3. no tokens in logs         (FM-A)
#   4. import boundaries         (§3.1 layering)
#   5. presentation names no transport type (3.1 / gate 1.6)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MOBILE="${REPO_ROOT}/mobile"
FIXTURES="${MOBILE}/scripts/fixtures"
failures=0

# Source files only: fixtures are deliberate violations, and tests name the
# forbidden things in order to assert they are rejected.
sources() {
  find "${MOBILE}" -name '*.dart' \
    -not -path '*/test/*' \
    -not -path '*/scripts/fixtures/*' \
    -not -path '*/lib/src/generated/*'
}

report() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

# --- 1. No client money math (gate 1.1) -------------------------------------
# Money is a server-rendered decimal string, rendered verbatim. A money-named
# identifier next to an arithmetic operator is the bug this gate exists for.
# Mirrors the web's no-money-math test.
MONEY_WORDS='amount|balance|principal|interest|penalty|deposit|share|installment|total_due|paid_amount|outstanding'
# A line carrying BOTH a money-named identifier and an arithmetic operator.
# Deliberately not an adjacency test: `double.parse(a) + double.parse(b)` puts
# eleven characters between the money word and the operator, and that is the
# exact bug -- the first draft of this sweep missed its own fixture for that
# reason. Comments are in scope on purpose (house rule 7); the remedy in prose
# is to drop the hyphen, e.g. "shared host" rather than "shared-host".
money_math() {
  # Test the CONTENT of the line, never grep's "file:line:" prefix -- the
  # repository path itself contains slashes, so testing the raw grep output
  # matched every line and silently turned this gate into a no-op. The
  # comment leader is stripped for the same reason; the comment TEXT stays in
  # scope, per house rule 7.
  grep -nHEi "(${MONEY_WORDS})" "$@" 2>/dev/null     | awk '{
        content = $0;
        sub(/^[^:]*:[0-9]+:/, "", content);
        # A directive is a PATH, and a path cannot be arithmetic.
        # Without this, exporting gp_balance_hero.dart trips the gate on
        # the slashes in its own filename: a false positive that teaches
        # people to rename files to appease a grep, which is how a gate
        # stops being believed.
        if (content ~ /^[[:space:]]*(import|export|part)[[:space:]]/)
          { next }
        sub(/^[[:space:]]*\/\/+/, "", content);
        if (content ~ /[-+*\/]/) { print }
      }'
}

mapfile -t SOURCE_FILES < <(sources)
hits="$(money_math "${SOURCE_FILES[@]}")"
if [[ -n "${hits}" ]]; then
  echo "${hits}" >&2
  report "client money math (gate 1.1): money values are server-rendered strings, never operands."
fi

# --- 2. No staff API paths (FM-H) -------------------------------------------
# The member app names the /member surface and nothing else — not /members/*,
# not /me/permissions, not /auth/*. The server returns 403 either way; this
# catches it before a build ships.
STAFF_PATHS="['\"]/(auth|me|members|member-exits|users|dashboard|branches|dividends|corrections|recovery|loans|transactions)(/|['\"])"
hits="$(sources | xargs grep -nE "${STAFF_PATHS}" 2>/dev/null || true)"
if [[ -n "${hits}" ]]; then
  echo "${hits}" >&2
  report "staff API path literal in the member app (FM-H)."
fi

# --- 3. No tokens in logs (FM-A) --------------------------------------------
# Tokens must never reach a log sink or a crash reporter.
hits="$(sources | xargs grep -nE '(print|debugPrint|log)\(.*(accessToken|refreshToken|access_token|refresh_token|Bearer)' 2>/dev/null || true)"
if [[ -n "${hits}" ]]; then
  echo "${hits}" >&2
  report "a token reaches a log sink (FM-A)."
fi

# --- 4. Import boundaries (§3.1) --------------------------------------------
# presentation -> domain <- data -> gp_api_client. A presentation layer that
# imports the transport has skipped its repository.
hits="$(find "${MOBILE}" -path '*/presentation/*' -name '*.dart' -print0 2>/dev/null \
  | xargs -0 grep -nE "import 'package:gp_api_client" 2>/dev/null || true)"
if [[ -n "${hits}" ]]; then
  echo "${hits}" >&2
  report "a presentation layer imports gp_api_client directly (§3.1)."
fi

# --- 5. Presentation names no transport type --------------------------------
# The import check above is necessary and not sufficient: core/providers.dart
# already imports gp_api_client, so a screen that imports the composition root
# can name GpHttpClient or ApiError with no import line of its own. A widget
# that switches on a status code or a server category has turned the server's
# private vocabulary into UI (gate 1.6) and breaks when it is renamed.
# Controllers translate; screens render what they are handed.
TRANSPORT_TYPES='\b(GpHttpClient|ApiError|ApiFailureKind|CertificatePinning|TokenStore|TokenStorage)\b'
presentation_transport() {
  # Comment leaders are stripped before testing, so a doc comment may still
  # SAY "this layer never sees an ApiError" -- which several of them do, and
  # which is worth being able to write down.
  grep -nHE "${TRANSPORT_TYPES}" "$@" 2>/dev/null \
    | awk '{
        content = $0;
        sub(/^[^:]*:[0-9]+:/, "", content);
        if (content !~ /^[[:space:]]*\/\//) { print }
      }'
}

mapfile -t PRESENTATION_FILES < <(
  find "${MOBILE}" -path '*/presentation/*' -name '*.dart' 2>/dev/null
)
if [[ "${#PRESENTATION_FILES[@]}" -gt 0 ]]; then
  hits="$(presentation_transport "${PRESENTATION_FILES[@]}")"
  if [[ -n "${hits}" ]]; then
    echo "${hits}" >&2
    report "a presentation layer names a transport type (3.1 / gate 1.6)."
  fi
fi

# gp_api_client holds transport, not UI.
hits="$(find "${MOBILE}/gp_api_client" -name '*.dart' -not -path '*/test/*' -print0 2>/dev/null \
  | xargs -0 grep -nE "import 'package:flutter/(widgets|material|cupertino).dart'" 2>/dev/null || true)"
if [[ -n "${hits}" ]]; then
  echo "${hits}" >&2
  report "gp_api_client imports Flutter UI (§3.1)."
fi

# --- Falsifiability: the sweeps must reject the fixtures --------------------
# Without this, a typo in a pattern above silently disables the gate and every
# pipeline stays green.
if [[ -d "${FIXTURES}" ]]; then
  for fixture in "${FIXTURES}"/*.dart.fixture; do
    [[ -e "${fixture}" ]] || continue
    case "$(basename "${fixture}")" in
      money_math.dart.fixture)
        [[ -n "$(money_math "${fixture}")" ]] \
          || report "the money-math sweep no longer catches its own fixture." ;;
      staff_path.dart.fixture)
        grep -qE "${STAFF_PATHS}" "${fixture}" \
          || report "the staff-path sweep no longer catches its own fixture." ;;
      presentation_transport.dart.fixture)
        [[ -n "$(presentation_transport "${fixture}")" ]] \
          || report "the presentation-transport sweep no longer catches its own fixture." ;;
      token_log.dart.fixture)
        grep -qE '(print|debugPrint|log)\(.*(accessToken|refreshToken|access_token|refresh_token|Bearer)' "${fixture}" \
          || report "the token-in-log sweep no longer catches its own fixture." ;;
    esac
  done
else
  report "falsifiability fixtures are missing: the sweeps prove nothing."
fi

if [[ "${failures}" -gt 0 ]]; then
  echo >&2
  echo "${failures} guard sweep(s) failed." >&2
  exit 1
fi

echo "All mobile guard sweeps passed, and each rejected its own fixture."
