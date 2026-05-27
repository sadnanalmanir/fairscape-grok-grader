#!/usr/bin/env bash
#
# Install the fairscape-grok-grader skills into your local Grok environment.
#
# This creates symlinks (or copies) from this project into ~/.grok/skills/
# so that /fairscape-grok-ai-ready-grader becomes available inside the Grok TUI.
#
set -euo pipefail

SKILL_SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/skills" && pwd)"
GROK_SKILLS_DIR="${HOME}/.grok/skills"

if [[ ! -d "$GROK_SKILLS_DIR" ]]; then
    echo "Creating $GROK_SKILLS_DIR"
    mkdir -p "$GROK_SKILLS_DIR"
fi

echo "Installing fairscape-grok-grader skills into $GROK_SKILLS_DIR ..."

# Main grader skill
if [[ -d "$SKILL_SRC_DIR/fairscape-grok-ai-ready-grader" ]]; then
    target="$GROK_SKILLS_DIR/fairscape-grok-ai-ready-grader"
    if [[ -e "$target" ]]; then
        echo "  - Removing existing $target"
        rm -rf "$target"
    fi
    ln -s "$SKILL_SRC_DIR/fairscape-grok-ai-ready-grader" "$target"
    echo "  + Installed fairscape-grok-ai-ready-grader (symlink)"
fi

echo
echo "Done. Restart your Grok TUI (or run 'grok' again) and try:"
echo
echo "  /fairscape-grok-ai-ready-grader"
echo
echo "You can also invoke the isolated sub-scorer directly if needed (advanced):"
echo "  /grok-rubric-scorer"
