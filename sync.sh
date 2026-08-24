#!/bin/bash
# One-command sync: stage everything, commit, push to GitHub.
# Run this on whichever machine you just made changes on.
# Usage:  sync.sh "what you changed"
cd "$(dirname "$0")" || exit 1

git add -A
if git diff --cached --quiet; then
    echo "Nothing to commit - working tree already clean."
    exit 0
fi
git commit -m "$1"
git push origin main