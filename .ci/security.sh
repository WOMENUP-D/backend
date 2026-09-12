#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 {semgrep|gitleaks|trivy}" >&2
  exit 2
fi

scanner="$1"
mkdir -p reports

case "$scanner" in
  semgrep)
    docker run --rm -v "$PWD:/src:ro" -v "$PWD/reports:/reports" -w /src \
      semgrep/semgrep:1.177.0@sha256:acaac22ffc7b7cc5926de0751b223bce0b2491c33d18422fa72f632c78d81198 \
      semgrep scan --config p/ci --metrics off --disable-version-check --error --json \
      --output /reports/semgrep.json .
    ;;
  gitleaks)
    docker run --rm -v "$PWD:/src:ro" -v "$PWD/reports:/reports" \
      -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/src \
      zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f \
      detect --source /src --config /src/.gitleaks.toml --redact --report-format json \
      --report-path /reports/gitleaks.json
    ;;
  trivy)
    docker run --rm -v "$PWD:/src:ro" -v "$PWD/reports:/reports" \
      aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969 \
      fs --scanners vuln --severity HIGH,CRITICAL --exit-code 0 --format json \
      --output /reports/dependencies.json /src
    docker run --rm -v "$PWD:/src:ro" \
      aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969 \
      fs --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 --quiet /src
    ;;
  *)
    echo "Unknown security scanner: $scanner" >&2
    exit 2
    ;;
esac
