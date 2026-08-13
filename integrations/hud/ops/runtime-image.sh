#!/bin/sh
# Recover and verify the exact OpenHands 0.33 amd64 runtime after the
# paper-era Scarf hostname was retired. This script makes no model calls.
set -eu
set +x

ORIGINAL_REF=docker.all-hands.dev/all-hands-ai/runtime:0.33-nikolaik
SOURCE_REPOSITORY=ghcr.io/all-hands-ai/runtime
INDEX_DIGEST=sha256:290784f8564ab5585025dc155cbfc39c3a5bb952511811f85b7371179e4dc446
AMD64_MANIFEST_DIGEST=sha256:ff8d9ef50ceb475130de5bca59d5c8f4dc9c45e11566ebaa6cae6a95b388d989
CONFIG_DIGEST=sha256:f29a0b0a27ea307e0a7aee2a538ad75bdd41cc2db85cfd9e0ac7fe355ca8cacb
SOURCE_REF=$SOURCE_REPOSITORY@$AMD64_MANIFEST_DIGEST

usage() {
    cat <<'EOF'
Usage: runtime-image.sh ensure|verify

  ensure  Pull the immutable official GHCR amd64 manifest when needed,
          verify its identity, and apply the original paper-era local tag.
  verify  Verify the immutable source identity and original local tag only.

The original OpenHands/CyberGym configuration remains unchanged. The retired
docker.all-hands.dev hostname is never contacted.
EOF
}

die() {
    printf 'runtime-image: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

image_field() {
    docker image inspect --format "$1" "$2" 2>/dev/null
}

source_matches() {
    [ "$(image_field '{{.Id}}' "$SOURCE_REF")" = "$CONFIG_DIGEST" ] \
        || return 1
    [ "$(image_field '{{.Os}}/{{.Architecture}}' "$SOURCE_REF")" = linux/amd64 ] \
        || return 1
    image_field '{{range .RepoDigests}}{{println .}}{{end}}' "$SOURCE_REF" \
        | grep -Fqx "$SOURCE_REF"
}

verify_source() {
    SOURCE_ID=$(image_field '{{.Id}}' "$SOURCE_REF") \
        || die "missing immutable OpenHands runtime source: $SOURCE_REF"
    [ "$SOURCE_ID" = "$CONFIG_DIGEST" ] \
        || die "source image config is $SOURCE_ID, expected $CONFIG_DIGEST"

    SOURCE_PLATFORM=$(image_field '{{.Os}}/{{.Architecture}}' "$SOURCE_REF") \
        || die "could not inspect immutable OpenHands runtime source"
    [ "$SOURCE_PLATFORM" = linux/amd64 ] \
        || die "source image platform is $SOURCE_PLATFORM, expected linux/amd64"

    image_field '{{range .RepoDigests}}{{println .}}{{end}}' "$SOURCE_REF" \
        | grep -Fqx "$SOURCE_REF" \
        || die "source image does not retain expected manifest digest $AMD64_MANIFEST_DIGEST"
}

verify_original_tag() {
    ORIGINAL_ID=$(image_field '{{.Id}}' "$ORIGINAL_REF") \
        || die "missing original OpenHands runtime tag: $ORIGINAL_REF"
    [ "$ORIGINAL_ID" = "$CONFIG_DIGEST" ] \
        || die "original runtime tag resolves to $ORIGINAL_ID, expected $CONFIG_DIGEST"

    ORIGINAL_PLATFORM=$(image_field '{{.Os}}/{{.Architecture}}' "$ORIGINAL_REF") \
        || die "could not inspect original OpenHands runtime tag"
    [ "$ORIGINAL_PLATFORM" = linux/amd64 ] \
        || die "original runtime tag platform is $ORIGINAL_PLATFORM, expected linux/amd64"

    image_field '{{range .RepoTags}}{{println .}}{{end}}' "$ORIGINAL_REF" \
        | grep -Fqx "$ORIGINAL_REF" \
        || die "expected paper-era local tag is not attached: $ORIGINAL_REF"
}

verify_all() {
    verify_source
    verify_original_tag
    printf '%s\n' "Verified exact OpenHands runtime:"
    printf '  original ref: %s\n' "$ORIGINAL_REF"
    printf '  GHCR index:   %s@%s\n' "$SOURCE_REPOSITORY" "$INDEX_DIGEST"
    printf '  amd64 child:  %s\n' "$AMD64_MANIFEST_DIGEST"
    printf '  config/image: %s\n' "$CONFIG_DIGEST"
}

[ "$#" -eq 1 ] || {
    usage >&2
    exit 1
}
case "$1" in
    -h|--help)
        usage
        exit 0
        ;;
esac
require_command docker
require_command grep

case "$1" in
    ensure)
        if ! source_matches; then
            printf 'Pulling immutable official OpenHands amd64 runtime: %s\n' "$SOURCE_REF"
            docker pull --platform linux/amd64 "$SOURCE_REF"
        fi
        verify_source
        docker tag "$SOURCE_REF" "$ORIGINAL_REF"
        verify_all
        ;;
    verify)
        verify_all
        ;;
    *)
        die "unknown command: $1"
        ;;
esac
