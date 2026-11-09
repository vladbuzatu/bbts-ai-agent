#!/usr/bin/env bash
# Runs inside GitHub Actions. Asks the server's deploy gate to deploy/promote/drill.
#   bash deploy/deploy.sh staging      (needs TAG)  -> "deploy staging <tag>"
#   bash deploy/deploy.sh production                 -> "promote production" (= what staging runs)
#   bash deploy/deploy.sh drill                      -> fire drill on staging
# Needs secrets: DEPLOY_HOST, DEPLOY_SSH_KEY, DEPLOY_KNOWN_HOSTS (printed by server-setup.sh).
# The deploy key can ONLY run the gate (SSH forced command); everything else is refused.
set -euo pipefail

MODE="${1:?usage: deploy.sh staging|production|drill}"

if [ -z "${DEPLOY_HOST:-}" ] || [ -z "${DEPLOY_SSH_KEY:-}" ]; then
  if [ "$MODE" = staging ]; then
    echo "::warning::DEPLOY_HOST / DEPLOY_SSH_KEY secrets not set - skipping the staging deploy."; exit 0
  fi
  echo "::error::DEPLOY_HOST / DEPLOY_SSH_KEY repository secrets are not set."; exit 1
fi
if [ -z "${DEPLOY_KNOWN_HOSTS:-}" ]; then
  echo "::error::Owner action needed: add the DEPLOY_KNOWN_HOSTS repository secret (printed by deploy/server-setup.sh). Deploys refuse to connect to an unverified server."
  exit 1
fi

mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' "$DEPLOY_SSH_KEY" > ~/.ssh/deploy_key && chmod 600 ~/.ssh/deploy_key
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > ~/.ssh/known_hosts && chmod 600 ~/.ssh/known_hosts

case "$MODE" in
  staging)    REQ="deploy staging ${TAG:?TAG is required} ${GITHUB_REPOSITORY} ${GITHUB_ACTOR}" ;;
  production) REQ="promote production ${GITHUB_REPOSITORY} ${GITHUB_ACTOR}" ;;
  drill)      REQ="drill staging ${GITHUB_REPOSITORY} ${GITHUB_ACTOR}" ;;
  *) echo "unknown mode $MODE"; exit 1 ;;
esac

echo "Asking the deploy gate: $REQ"
set +e
printf '%s\n' "${REGISTRY_TOKEN:-}" | ssh -i ~/.ssh/deploy_key \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=~/.ssh/known_hosts \
  -o ConnectTimeout=20 -o BatchMode=yes "deploy@${DEPLOY_HOST}" "$REQ"
rc=$?
set -e
case $rc in
  0) echo "✅ done" ;;
  2) echo "::error::The new version failed its checks and was ROLLED BACK automatically." ;;
  3) echo "::error::Rollback FAILED - the environment may be down." ;;
  64) echo "::error::The deploy gate refused the request (see message above)." ;;
  *) echo "::error::Deploy failed (code $rc)." ;;
esac
exit $rc
