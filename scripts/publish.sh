#!/usr/bin/env bash
# One-shot publisher: run this AFTER `gh auth login`.
# It fills your GitHub username into the OWNER placeholders, creates the repo,
# pushes, and triggers the multi-arch GHCR build.
set -euo pipefail

REPO_NAME="${1:-farm-netwatch}"

command -v gh >/dev/null || { echo "gh CLI not installed"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run 'gh auth login' first."; exit 1; }

OWNER=$(gh api user -q .login)
echo "==> GitHub user: $OWNER   repo: $REPO_NAME"

cd "$(dirname "$0")/.."

# Replace the OWNER placeholder everywhere it appears.
grep -rl --exclude-dir=.git 'Riaan007/farm-netwatch' . | while read -r f; do
  sed -i "s#Riaan007/farm-netwatch#${OWNER}/${REPO_NAME}#g" "$f"
done
sed -i "s#ghcr.io/riaan007#ghcr.io/${OWNER}#g" docker-compose.yml install.sh 2>/dev/null || true

git add -A
git commit -q -m "Set GHCR/repo owner to ${OWNER}" || echo "(nothing to commit)"

# Create the repo if it doesn't exist, otherwise just add the remote.
if gh repo view "${OWNER}/${REPO_NAME}" >/dev/null 2>&1; then
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/${OWNER}/${REPO_NAME}.git"
  git push -u origin main
else
  gh repo create "${OWNER}/${REPO_NAME}" --public --source=. --remote=origin --push
fi

echo
echo "==> Pushed. The GitHub Actions build is now creating the multi-arch image."
echo "    Watch it:   gh run watch -R ${OWNER}/${REPO_NAME} || gh run list -R ${OWNER}/${REPO_NAME}"
echo
echo "==> After the first build finishes, make the image public so any Pi can pull it:"
echo "    https://github.com/users/${OWNER}/packages/container/${REPO_NAME}/settings"
echo "    (Danger Zone -> Change visibility -> Public)"
echo
echo "==> Then install on any Raspberry Pi with:"
echo "    curl -fsSL https://raw.githubusercontent.com/${OWNER}/${REPO_NAME}/main/install.sh | sudo bash"
