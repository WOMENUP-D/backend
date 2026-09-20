#!/usr/bin/env bash
set -euo pipefail
: "${APP:?}" "${IMAGE:?}" "${SOURCE_SHA:?}" "${GH_TOKEN:?}"
[[ "$APP" == backend || "$APP" == frontend ]]
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$IMAGE" =~ ^ghcr.io/womenup-d/$APP@sha256:[0-9a-f]{64}$ ]]
key="${APP^^}"
git -C .infra config user.name "WomanUP deploy"
git -C .infra config user.email "deploy@users.noreply.github.com"
for attempt in 1 2 3 4 5; do
  head=$(gh api "repos/WOMENUP-D/$APP/commits/main" --jq .sha)
  if [[ "$head" != "$SOURCE_SHA" ]]; then
    echo "A newer source commit exists; skipping stale promotion."
    exit 0
  fi
  if (( attempt == 1 )); then
    printf '%s_IMAGE=%s\n%s_VERSION=%s-%s\n' "$key" "$IMAGE" "$key" "$APP" "$SOURCE_SHA" > ".infra/apps/$APP/image.env"
    git -C .infra add "apps/$APP/image.env"
    if git -C .infra diff --cached --quiet; then exit 0; fi
    git -C .infra commit -m "deploy $APP-$SOURCE_SHA"
  fi
  if git -C .infra push origin HEAD:main; then exit 0; fi
  # Concurrent changes to the other application's file rebase cleanly.
  git -C .infra pull --rebase origin main
  sleep "$attempt"
done
echo "Promotion could not be pushed; rerun this job." >&2
exit 1
