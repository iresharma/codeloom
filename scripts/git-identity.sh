#!/usr/bin/env bash
# Resolve the human git author for codeloom scripts.
# Prints: <name><TAB><email>
# Never returns Cursor Agent / cursoragent identities.
set -euo pipefail

is_cursor_identity() {
  local name="${1:-}"
  local email="${2:-}"
  local lname lemail
  lname="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  lemail="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]')"

  [[ "$lemail" == *cursoragent@cursor.com* ]] && return 0
  [[ "$lemail" == *@cursor.com ]] && return 0
  [[ "$lname" == "cursor" || "$lname" == "cursor agent" ]] && return 0
  return 1
}

parse_ident() {
  # "Name <email> ..." -> name + email
  local ident="$1"
  local name email
  name="$(printf '%s' "$ident" | sed -n 's/^\(.*\) <[^>]*>.*/\1/p')"
  email="$(printf '%s' "$ident" | sed -n 's/^.* <\([^>]*\)>.*/\1/p')"
  if [[ -n "$name" && -n "$email" ]]; then
    printf '%s\t%s\n' "$name" "$email"
    return 0
  fi
  return 1
}

try_pair() {
  local name="${1:-}"
  local email="${2:-}"
  if [[ -z "$name" || -z "$email" ]]; then
    return 1
  fi
  if is_cursor_identity "$name" "$email"; then
    return 1
  fi
  printf '%s\t%s\n' "$name" "$email"
}

# Prefer explicit codeloom overrides, then real git config, then env / git var
# (skipping Cursor), then the local machine identity.
if pair="$(try_pair "${CODELOOM_GIT_NAME:-}" "${CODELOOM_GIT_EMAIL:-}")"; then
  printf '%s\n' "$pair"
  exit 0
fi

cfg_name="$(git config user.name 2>/dev/null || true)"
cfg_email="$(git config user.email 2>/dev/null || true)"
if pair="$(try_pair "$cfg_name" "$cfg_email")"; then
  printf '%s\n' "$pair"
  exit 0
fi

if pair="$(try_pair "${GIT_AUTHOR_NAME:-}" "${GIT_AUTHOR_EMAIL:-}")"; then
  printf '%s\n' "$pair"
  exit 0
fi

if ident="$(git var GIT_AUTHOR_IDENT 2>/dev/null || true)"; then
  if parsed="$(parse_ident "$ident")"; then
    name="${parsed%%$'\t'*}"
    email="${parsed#*$'\t'}"
    if pair="$(try_pair "$name" "$email")"; then
      printf '%s\n' "$pair"
      exit 0
    fi
  fi
fi

sys_name="$(id -F 2>/dev/null || id -un)"
sys_email="${USER:-$(id -un)}@$(hostname -s 2>/dev/null || hostname).local"
if pair="$(try_pair "$sys_name" "$sys_email")"; then
  printf '%s\n' "$pair"
  exit 0
fi

echo "error: could not resolve a non-Cursor git identity" >&2
echo "set CODELOOM_GIT_NAME / CODELOOM_GIT_EMAIL, or:" >&2
echo "  git config --global user.name \"Your Name\"" >&2
echo "  git config --global user.email \"you@example.com\"" >&2
exit 1
