#!/usr/bin/env bash
# Prove whether a project's PUBLISHED artifacts are complete, without Docker.
#
# Why: "the git repo is missing a file" is a weak claim. "the official container
# image contains the exact same bytes as the clone, and it is also missing the
# file" is a strong one — it means nobody can run the project as published.
#
# Usage:
#   verify_upstream_complete.sh <owner/repo> [path/inside/image ...]
#
# Example:
#   verify_upstream_complete.sh 23Star/xianyu-super-butler \
#       app/app/delivery_template.py app/app/services/notification_test.py
#
# Env:
#   TAG=latest            image tag to inspect
#   REGISTRY=ghcr.io      registry host
#   MAX_LAYER_MB=0        skip layers larger than this (0 = no limit)
#   NO_EARLY_STOP=1       keep scanning even after every target was found
#
# Gotchas baked in:
#   * curl on this host needs --ssl-no-revoke, else CRYPT_E_NO_REVOCATION_CHECK
#   * use the FULL layer digest; a truncated digest returns a ~98 byte error body
#   * **strip CR from every Python-generated value** — on Windows, `python3` writes
#     CRLF, so a digest read back into bash carries a trailing \r and curl then dies
#     with "URL rejected: Malformed input to a URL function". Silently, this turns
#     every layer into a false "absent" — i.e. the script confidently reports the
#     wrong answer. Every command substitution below pipes through `tr -d '\r'`.
#   * layers are scanned SMALLEST FIRST and we stop once all targets are found —
#     the application source layer is usually one of the smallest (the huge ones
#     are the language runtime, site-packages and browser bundles)
#   * **`mktemp -d` on Git Bash returns a BACKSLASH path** (`C:\Users\...\tmp.XXX`).
#     MSYS tar cannot open that, and GNU tar additionally reads the `C:` of a
#     `C:/...` path as a *remote host* ("Cannot connect to C: resolve failed").
#     Either way tar exits non-zero and the listing comes out EMPTY — and an empty
#     listing makes every target look absent. Two defences: normalise backslashes
#     to `/`, and pass `tar --force-local`.
#   * **an empty listing is NEVER evidence of absence.** If tar/extraction fails we
#     say so explicitly and refuse to emit an ABSENT verdict for that layer. This is
#     the single most important guard in this script: the failure mode here is not
#     a crash, it is a *confidently wrong answer*.
#   * we deliberately never `rm` the work files: on this host the safe-delete shim
#     fails closed on TEMP paths and floods stderr. The mktemp dir is disposable.

set -uo pipefail

REGISTRY="${REGISTRY:-ghcr.io}"
REPO="${1:-}"
shift || true
TARGETS=("$@")

if [[ -z "$REPO" ]]; then
  echo "usage: $0 <owner/repo> [path/inside/image ...]" >&2
  echo "  env: TAG (default latest), REGISTRY (default ghcr.io), MAX_LAYER_MB, NO_EARLY_STOP" >&2
  exit 2
fi

TAG="${TAG:-latest}"
OWNER_PATH="${REPO,,}"          # ghcr paths are lowercase
MAX_LAYER_MB="${MAX_LAYER_MB:-0}"
WORK="$(mktemp -d)"
WORK="${WORK//\\//}"             # mktemp on Git Bash yields C:\... — tar chokes on it
TAR=(tar --force-local)          # else GNU tar reads the "C:" of C:/... as a remote host
trap 'echo "[workdir] $WORK"' EXIT

CURL=(curl -sS --ssl-no-revoke)

# Run python and normalise line endings to LF (Windows python3 emits CRLF).
py() { python3 "$@" | tr -d '\r'; }

# List a layer tarball. Returns non-zero (and writes nothing) if tar could not read it.
list_layer() { "${TAR[@]}" -tzf "$1" 2>/dev/null; }

echo "== registry=$REGISTRY repo=$OWNER_PATH tag=$TAG"

TOKEN="$("${CURL[@]}" \
  "https://${REGISTRY}/token?scope=repository:${OWNER_PATH}:pull&service=${REGISTRY}" \
  | tr -d '\r' | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"

if [[ -z "$TOKEN" ]]; then
  echo "!! could not obtain an anonymous pull token (private image?)" >&2
  exit 1
fi
echo "== token acquired (${#TOKEN} chars)"

AUTH=(-H "Authorization: Bearer ${TOKEN}")
ACCEPT=(-H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json")

"${CURL[@]}" "${AUTH[@]}" "${ACCEPT[@]}" \
  "https://${REGISTRY}/v2/${OWNER_PATH}/manifests/${TAG}" -o "$WORK/index.json" || exit 1

DIGEST="$(py - "$WORK/index.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
entries = data.get("manifests") or []
for entry in entries:
    platform = entry.get("platform") or {}
    if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
        print(entry["digest"]); break
else:
    print(entries[0]["digest"] if entries else "")
PY
)"

if [[ -z "$DIGEST" ]]; then
  echo "!! no manifest digest found; raw index follows:" >&2
  head -c 600 "$WORK/index.json" >&2
  exit 1
fi
echo "== amd64 manifest: $DIGEST"

"${CURL[@]}" "${AUTH[@]}" \
  -H "Accept: application/vnd.oci.image.manifest.v1+json" \
  "https://${REGISTRY}/v2/${OWNER_PATH}/manifests/${DIGEST}" -o "$WORK/manifest.json" || exit 1

# Smallest first: the app source layer is normally near the bottom of this list.
py - "$WORK/manifest.json" > "$WORK/layers.txt" <<'PY'
import json, sys
layers = json.load(open(sys.argv[1]))["layers"]
for i, layer in sorted(enumerate(layers), key=lambda pair: pair[1]["size"]):
    print(i, layer["size"], layer["digest"])
PY

echo "== layers, smallest first (index size digest)"
cat "$WORK/layers.txt"

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo
  echo "No paths requested — showing the top-level entries of each layer instead."
  while read -r idx size digest; do
    digest="${digest%$'\r'}"
    echo "--- layer $idx ($size bytes)"
    "${CURL[@]}" -L "${AUTH[@]}" \
      "https://${REGISTRY}/v2/${OWNER_PATH}/blobs/${digest}" \
      | "${TAR[@]}" -tz 2>/dev/null | head -5
  done < "$WORK/layers.txt"
  exit 0
fi

echo
echo "== searching for: ${TARGETS[*]}"
declare -A FOUND_LAYER=()
found=0
scanned=0
extract_failed=0

while read -r idx size digest; do
  digest="${digest%$'\r'}"
  if [[ "$MAX_LAYER_MB" != "0" ]] && (( size > MAX_LAYER_MB * 1024 * 1024 )); then
    echo "  skipped  layer $idx ($size bytes > ${MAX_LAYER_MB}MB cap)"
    continue
  fi

  file="$WORK/layer-$idx.tar.gz"
  listing="$WORK/layer-$idx.list"
  if ! "${CURL[@]}" -L "${AUTH[@]}" \
      "https://${REGISTRY}/v2/${OWNER_PATH}/blobs/${digest}" -o "$file"; then
    echo "  !! download failed for layer $idx — NOT evidence of absence"
    extract_failed=$((extract_failed + 1))
    continue
  fi
  scanned=$((scanned + 1))

  # Never let a failed extraction masquerade as "this layer lacks the file".
  if ! list_layer "$file" > "$listing" || [[ ! -s "$listing" ]]; then
    echo "  !! could not list layer $idx (tar failed) — NOT evidence of absence"
    extract_failed=$((extract_failed + 1))
    continue
  fi

  hits=0
  for target in "${TARGETS[@]}"; do
    if grep -qxF "$target" "$listing"; then
      echo "  FOUND    layer $idx -> $target"
      FOUND_LAYER["$target"]="$idx"
      hits=$((hits + 1))
    fi
  done
  if [[ $hits -eq 0 ]]; then
    echo "  absent   layer $idx ($size bytes)"
  else
    found=$((found + hits))
    if [[ "${NO_EARLY_STOP:-0}" != "1" ]] && (( found >= ${#TARGETS[@]} )); then
      echo "  (all targets located — stopping early)"
      break
    fi
  fi
done < "$WORK/layers.txt"

echo
echo "== verdict (scanned $scanned layer(s), $extract_failed unreadable)"
missing_any=0
unproven=0
for target in "${TARGETS[@]}"; do
  if [[ -n "${FOUND_LAYER[$target]:-}" ]]; then
    echo "  present  $target  (layer ${FOUND_LAYER[$target]})"
  elif (( extract_failed > 0 )); then
    # We could not read every layer, so we cannot honestly claim absence.
    echo "  UNKNOWN  $target  — not seen, but $extract_failed layer(s) were unreadable"
    unproven=1
    missing_any=1
  else
    echo "  ABSENT   $target  — not in any scanned layer"
    missing_any=1
  fi
done

if (( unproven )); then
  cat <<'EOF'

UNKNOWN is not a result. Fix the download/extraction failure and re-run — do NOT
report "the image is missing this file" from a run that could not read its layers.
EOF
fi

if (( missing_any )); then
  cat <<'EOF'

An ABSENT target means the published image does not contain it either.
Next step that turns this into proof: extract the image's own copy of the file that
imports the missing module and diff it against the clone. Byte-identical output
means the official image is broken exactly the same way the source is — i.e. the
project cannot run as published.

Sanity-check before believing an ABSENT result: re-run with NO_EARLY_STOP=1, and
confirm the layers actually downloaded (a "download failed" / "could not list" line
invalidates the verdict rather than proving absence). A positive control is cheap:
pass a path you KNOW is in the image (e.g. the file that does the importing) and
confirm it comes back `present`. If the control is also absent, the harness is broken.
EOF
fi

exit $missing_any
