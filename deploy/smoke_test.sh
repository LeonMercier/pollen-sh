#!/usr/bin/env bash
# End-to-end check against a running stack. Exits non-zero on the first failure.
#
#   bash deploy/smoke_test.sh [base_url]
#
# Defaults to http://localhost:$HTTP_PORT (8080), i.e. through Caddy, so this
# exercises the reverse proxy and the static file serving as well as the API.

set -euo pipefail

BASE="${1:-http://localhost:${HTTP_PORT:-8080}}"
CITY="${SMOKE_CITY:-Helsinki}"
# Ähtäri: an accented name, searched by its accented and plain prefixes.
ACCENT_TERM="${SMOKE_ACCENT_TERM:-äht}"
ACCENT_PLAIN="${SMOKE_ACCENT_PLAIN:-aht}"
# Loviisa / Lovisa: the same bilingual town under its Finnish and Swedish names.
BILINGUAL_A="${SMOKE_BILINGUAL_A:-Loviisa}"
BILINGUAL_B="${SMOKE_BILINGUAL_B:-Lovisa}"
failures=0

check() {
	local name="$1"
	shift
	if "$@" >/dev/null 2>&1; then
		printf '  ok    %s\n' "$name"
	else
		printf '  FAIL  %s\n' "$name"
		failures=$((failures + 1))
	fi
}

json() { curl -fsS --max-time 30 "$1"; }

printf 'Smoke testing %s\n' "$BASE"

check "static frontend served" curl -fsS --max-time 10 "$BASE/"

printf '  ....  health\n'
health=$(curl -sS --max-time 10 "$BASE/health" || true)
printf '        %s\n' "$health"
case "$health" in
*'"healthy"'*) printf '  ok    health reports healthy\n' ;;
*) printf '  FAIL  health is not healthy\n'; failures=$((failures + 1)) ;;
esac

printf '  ....  city autocomplete\n'
cities=$(json "$BASE/api/cities?q=${CITY:0:3}" || true)
if [ "$(printf '%s' "$cities" | grep -c '"name"')" -ge 1 ]; then
	printf '  ok    autocomplete returned matches\n'
else
	printf '  FAIL  autocomplete returned nothing: %s\n' "${cities:0:200}"
	failures=$((failures + 1))
fi

# Accent-insensitive prefix search: typing the accented or the plain form of a
# name must both find it. Regression guard -- autocomplete used to match only
# ascii_name, so "äht" found nothing while "ähtäri" worked.
printf '  ....  accent-insensitive search\n'
for term in "$ACCENT_TERM" "$ACCENT_PLAIN"; do
	got=$(json "$BASE/api/cities?q=$(printf '%s' "$term" | od -An -tx1 | tr -d ' \n' | sed 's/../%&/g')" || true)
	if printf '%s' "$got" | grep -q '"name"'; then
		printf '  ok    "%s" returns matches\n' "$term"
	else
		printf '  FAIL  "%s" returns nothing\n' "$term"
		failures=$((failures + 1))
	fi
done

# Bilingual cities must be findable under either language and must resolve to
# the same grid cell. GeoNames stores only one form in `name`.
printf '  ....  bilingual name equivalence (%s / %s)\n' "$BILINGUAL_A" "$BILINGUAL_B"
cell_of() {
	json "$BASE/api/plots?city=$1" 2>/dev/null |
		grep -o '"lat":[0-9.]*,"lon":[0-9.]*' | head -1
}
cell_a=$(cell_of "$BILINGUAL_A")
cell_b=$(cell_of "$BILINGUAL_B")
if [ -n "$cell_a" ] && [ "$cell_a" = "$cell_b" ]; then
	printf '  ok    both names resolve to %s\n' "$cell_a"
else
	printf '  FAIL  %s -> [%s] but %s -> [%s]\n' \
		"$BILINGUAL_A" "$cell_a" "$BILINGUAL_B" "$cell_b"
	failures=$((failures + 1))
fi

printf '  ....  plots for %s\n' "$CITY"
plots=$(json "$BASE/api/plots?city=$CITY" || true)
case "$plots" in
*'"success":true'*) printf '  ok    plots generated\n' ;;
*) printf '  FAIL  plots failed: %s\n' "${plots:0:300}"; failures=$((failures + 1)) ;;
esac

# Every one of the six species should come back with a full trace.
species_count=$(printf '%s' "$plots" | grep -o '"config"' | wc -l | tr -d ' ')
if [ "$species_count" -eq 6 ]; then
	printf '  ok    all 6 species present\n'
else
	printf '  FAIL  expected 6 species, got %s\n' "$species_count"
	failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
	printf 'All checks passed.\n'
else
	printf '%d check(s) failed.\n' "$failures"
	exit 1
fi
