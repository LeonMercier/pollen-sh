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
