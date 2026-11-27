#!/usr/bin/env bash
#
# havn-pack-video — host installer
#
# Installs, once on the host, for all havn agents on it:
#   /opt/havn/ffmpeg  ffmpeg + ffprobe (a pinned LGPL build of FFmpeg-Builds,
#                     its libraries linked in statically; needs only glibc
#                     >= 2.28), its licence texts, and a font for frame labels
#   /opt/havn/watch   the /watch skill of bradautomates/claude-video (MIT),
#                     pinned to one commit, in a Python venv with a pinned
#                     yt-dlp, and the havn-watch wrapper that runs it in an
#                     agent's sandbox
# Every download is over HTTPS and checked against its SHA-256 before it is
# used. Each version goes into a directory of its own (ffmpeg-<version>,
# watch-<version>), made complete next to the install and renamed into
# place; /opt/havn/ffmpeg and /opt/havn/watch are links switched to it in one
# rename. The version they pointed at before is kept, for agents still
# running on it; older ones are removed. Idempotent: re-running keeps what
# matches and installs the rest.
#
# Tested on Debian 12 / Ubuntu 24.04, x86_64; aarch64 builds are pinned too.

set -euo pipefail

PREFIX="${HAVN_PACK_PREFIX:-/opt/havn}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The pinned ffmpeg build: a month-end release of FFmpeg-Builds (kept for two
# years by its retention policy), ffmpeg's release/9.0 branch at
# n9.0.2-39-g7adfbe40dc.
FFMPEG_RELEASE="autobuild-2026-10-31-12-59"
FFMPEG_BUILD="ffmpeg-n9.0.2-39-g7adfbe40dc"
FFMPEG_BRANCH="9.0"
FFMPEG_SHA256_LINUX64="6046d6731b26f58887e953561551d254a6eec9fbca4100dfeab56e19af8328d5"
FFMPEG_SHA256_LINUXARM64="3d1f66827a73ce5a446312bc4924234dc1515726d3743e2df35c0177e1f82128"
FFMPEG_BASE_URL="https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/$FFMPEG_RELEASE"

# The pinned /watch skill: release 0.3.2 of bradautomates/claude-video.
WATCH_COMMIT="03ceb42f7fa2c4439aca01752118044baabffb8f"
WATCH_SHA256="495ae96eeb96b3f3a50a94e1a8542b255ecf72ec5e42aa3a85190af22f4412ba"
WATCH_URL="https://github.com/bradautomates/claude-video/archive/$WATCH_COMMIT.tar.gz"
# Its Python dependencies, pure-Python wheels from PyPI, by hash.
YTDLP_VERSION="2026.10.26"
YTDLP_SHA256="a05c5b5d8562f7e8a62f5334348b406af4930d56d3a65f8021d14af0e22339ae"
EJS_VERSION="0.8.0"
EJS_SHA256="79300e5fca7f937a1eeede11f0456862c1b41107ce1d726871e0207424f4bdb4"

# Free space the downloads and the install take, with room to spare.
NEEDED_MB=750
# The directory a version is made in, removed on exit.
WORK=""

log() { printf '\033[1;36m[pack]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[pack]\033[0m %s\n' "$*" >&2; }
cleanup() { if [ -n "$WORK" ]; then rm -rf "$WORK"; WORK=""; fi; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "this script writes under $PREFIX and /usr/local/bin — re-run with sudo"
        exit 1
    fi
}

require_tools() {
    local tool missing=()
    for tool in curl tar xz gzip sha256sum getconf python3; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        err "missing: ${missing[*]} — install them first (Debian/Ubuntu: apt-get install curl tar xz-utils gzip coreutils libc-bin python3 python3-venv)"
        exit 2
    fi
}

# yt-dlp needs Python 3.11 or later; the venv needs ensurepip, which Debian
# and Ubuntu ship as python3-venv.
require_python() {
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
        err "python3 is $(python3 -V 2>&1 | cut -d' ' -f2); the /watch scripts' yt-dlp needs 3.11 or later"
        exit 2
    fi
    if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
        err "python3 cannot make a venv — install python3-venv (Debian/Ubuntu) and re-run"
        exit 2
    fi
}

# The build's target for this machine, and the SHA-256 of its archive.
detect_target() {
    case "$(uname -m)" in
        x86_64) echo "linux64 $FFMPEG_SHA256_LINUX64" ;;
        aarch64|arm64) echo "linuxarm64 $FFMPEG_SHA256_LINUXARM64" ;;
        *) err "unsupported arch: $(uname -m) (x86_64 and aarch64 only)"; exit 4 ;;
    esac
}

require_glibc() {
    local version major minor
    version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')" || true
    if [ -z "$version" ]; then
        err "glibc not found (musl? Alpine?) — this ffmpeg build needs glibc >= 2.28"
        exit 4
    fi
    major="${version%%.*}"
    minor="${version#*.}"
    minor="${minor%%.*}"
    if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 28 ]; }; then
        err "glibc $version is older than 2.28, which this ffmpeg build needs"
        exit 4
    fi
}

require_space() {
    local avail_kb
    avail_kb="$(df -Pk "$PREFIX" | awk 'NR == 2 {print $4}')"
    if [ "$avail_kb" -lt $((NEEDED_MB * 1024)) ]; then
        err "only $((avail_kb / 1024)) MB free under $PREFIX — the install needs about $NEEDED_MB MB"
        exit 5
    fi
}

# Download $1 to $2 over HTTPS only, and check it against SHA-256 $3.
fetch() {
    local url="$1" out="$2" sha="$3"
    log "fetching $url"
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 "$url" -o "$out"
    if ! echo "$sha  $out" | sha256sum -c --status; then
        err "SHA-256 mismatch for $url — expected $sha, got $(sha256sum "$out" | cut -d' ' -f1). Nothing was installed."
        exit 6
    fi
    log "SHA-256 verified: $sha"
}

# The first 12 hex digits of the SHA-256 of the text $1: what names a version.
key_of() {
    printf '%s' "$1" | sha256sum | cut -c1-12
}

file_sha() {
    sha256sum "$1" | cut -d' ' -f1
}

# Point the link $PREFIX/$1 at the version directory $2 (a name in $PREFIX)
# in one rename. The version it pointed at before is kept (and named in
# $PREFIX/.$1.previous, so a rerun keeps it too); the other versions of $1
# are removed.
switch() {
    local name="$1" version="$2" link="$PREFIX/$1" record="$PREFIX/.$1.previous" current="" previous="" dir
    if [ -f "$record" ]; then
        previous="$(cat "$record")"
    fi
    if [ -L "$link" ]; then
        current="$(readlink "$link")"
    elif [ -e "$link" ]; then
        # An install made before versions had directories of their own.
        rm -rf "${PREFIX:?}/$name-previous"
        mv "$link" "$PREFIX/$name-previous"
        current="$name-previous"
    fi
    if [ "$current" != "$version" ]; then
        ln -sfn "$version" "$PREFIX/.$name.link"
        mv -T "$PREFIX/.$name.link" "$link"
        log "switched $link → $version"
        if [ -n "$current" ]; then
            previous="$current"
            echo "$previous" > "$record"
        fi
    fi
    for dir in "$PREFIX/$name"-*; do
        [ -e "$dir" ] || continue
        case "$(basename "$dir")" in
            "$version"|"$previous") ;;
            *) rm -rf "$dir"; log "removed the older $(basename "$dir")" ;;
        esac
    done
}

install_ffmpeg() {
    local found target sha archive build version
    found="$(detect_target)"
    target="${found% *}"
    sha="${found#* }"
    archive="$FFMPEG_BUILD-$target-lgpl-$FFMPEG_BRANCH.tar.xz"
    build="$archive $sha font $(file_sha "$HERE/fonts/DejaVuSansMono-Bold.ttf") gpl $(file_sha "$HERE/licenses/GPL-3.0.txt")"
    version="ffmpeg-${FFMPEG_BUILD#ffmpeg-}-$target-$(key_of "$build")"

    if [ -f "$PREFIX/$version/BUILD" ] && [ "$(cat "$PREFIX/$version/BUILD")" = "$build" ] \
        && "$PREFIX/$version/bin/ffmpeg" -hide_banner -version >/dev/null 2>&1; then
        log "ffmpeg ${FFMPEG_BUILD#ffmpeg-} ($target, LGPL) already installed as $PREFIX/$version — skipping download"
        switch ffmpeg "$version"
        return
    fi

    require_space
    WORK="$(mktemp -d "$PREFIX/.ffmpeg.XXXXXX")"
    fetch "$FFMPEG_BASE_URL/$archive" "$WORK/ffmpeg.tar.xz" "$sha"
    mkdir -p "$WORK/ffmpeg"
    tar -xJf "$WORK/ffmpeg.tar.xz" -C "$WORK/ffmpeg" --strip-components=1 --no-same-owner \
        "${archive%.tar.xz}/bin/ffmpeg" "${archive%.tar.xz}/bin/ffprobe" "${archive%.tar.xz}/LICENSE.txt"
    rm -f "$WORK/ffmpeg.tar.xz"
    if ! "$WORK/ffmpeg/bin/ffmpeg" -hide_banner -version >/dev/null 2>&1; then
        err "the downloaded ffmpeg does not run on this host. Nothing was installed."
        exit 7
    fi
    # The LGPL v3 the build is under refers to the GPL v3: both texts go
    # with it. DejaVu Sans Mono Bold (DejaVu 2.37), shipped in this repo, is
    # for the time labels on contact sheets.
    install -m 644 "$HERE/licenses/GPL-3.0.txt" "$WORK/ffmpeg/COPYING.GPLv3"
    install -d -m 755 "$WORK/ffmpeg/fonts"
    install -m 644 "$HERE/fonts/DejaVuSansMono-Bold.ttf" "$HERE/fonts/LICENSE-DejaVu.txt" "$WORK/ffmpeg/fonts/"
    echo "$build" > "$WORK/ffmpeg/BUILD"
    chmod 755 "$WORK/ffmpeg" "$WORK/ffmpeg/bin" "$WORK/ffmpeg/bin/ffmpeg" "$WORK/ffmpeg/bin/ffprobe"
    chmod 644 "$WORK/ffmpeg/LICENSE.txt" "$WORK/ffmpeg/BUILD"

    rm -rf "${PREFIX:?}/$version"
    mv "$WORK/ffmpeg" "$PREFIX/$version"
    cleanup
    log "ffmpeg ${FFMPEG_BUILD#ffmpeg-} ($target, LGPL) installed as $PREFIX/$version"
    switch ffmpeg "$version"
}

install_watch() {
    local build version
    build="claude-video $WATCH_COMMIT $WATCH_SHA256 yt-dlp $YTDLP_VERSION $YTDLP_SHA256 yt-dlp-ejs $EJS_VERSION $EJS_SHA256 wrapper $(file_sha "$HERE/havn_watch.py")"
    version="watch-${WATCH_COMMIT:0:12}-$(key_of "$build")"

    if [ -f "$PREFIX/$version/BUILD" ] && [ "$(cat "$PREFIX/$version/BUILD")" = "$build" ] \
        && "$PREFIX/$version/bin/yt-dlp" --version >/dev/null 2>&1; then
        log "/watch ${WATCH_COMMIT:0:12} and yt-dlp $YTDLP_VERSION already installed as $PREFIX/$version — skipping download"
        switch watch "$version"
        return
    fi

    require_space
    WORK="$(mktemp -d "$PREFIX/.watch.XXXXXX")"
    fetch "$WATCH_URL" "$WORK/watch.tar.gz" "$WATCH_SHA256"
    mkdir -p "$WORK/watch/upstream"
    tar -xzf "$WORK/watch.tar.gz" -C "$WORK/watch/upstream" --strip-components=1 --no-same-owner \
        "claude-video-$WATCH_COMMIT/skills/watch" "claude-video-$WATCH_COMMIT/LICENSE" \
        "claude-video-$WATCH_COMMIT/README.md" "claude-video-$WATCH_COMMIT/CHANGELOG.md"
    rm -f "$WORK/watch.tar.gz"
    echo "$WATCH_COMMIT" > "$WORK/watch/upstream/COMMIT"
    printf 'yt-dlp==%s --hash=sha256:%s\nyt-dlp-ejs==%s --hash=sha256:%s\n' \
        "$YTDLP_VERSION" "$YTDLP_SHA256" "$EJS_VERSION" "$EJS_SHA256" > "$WORK/requirements.txt"

    # The venv works from wherever it is: it is run only through the
    # launchers below, which find it next to themselves, so it is made here
    # whole and renamed into place.
    log "making the venv"
    python3 -m venv "$WORK/watch/venv"
    "$WORK/watch/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
        --no-deps --only-binary :all: --require-hashes -r "$WORK/requirements.txt"
    install -m 644 "$HERE/havn_watch.py" "$WORK/watch/havn_watch.py"
    install -d -m 755 "$WORK/watch/bin"
    # yt-dlp, through the wrapper: it reads no configuration file of the host
    # or the agent, and downloads 2 fragments at a time (upstream asks for 8).
    cat > "$WORK/watch/bin/yt-dlp" <<'SH'
#!/bin/sh
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$here/../venv/bin/python" -I "$here/../havn_watch.py" --yt-dlp "$@"
SH
    cat > "$WORK/watch/bin/havn-watch" <<'SH'
#!/bin/sh
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$here/../venv/bin/python" -I "$here/../havn_watch.py" "$@"
SH
    chmod 755 "$WORK/watch/bin/yt-dlp" "$WORK/watch/bin/havn-watch"
    if ! "$WORK/watch/bin/yt-dlp" --version >/dev/null 2>&1; then
        err "yt-dlp does not run from the new venv. Nothing was installed."
        exit 7
    fi
    echo "$build" > "$WORK/watch/BUILD"
    chmod -R u=rwX,go=rX "$WORK/watch"

    rm -rf "${PREFIX:?}/$version"
    mv "$WORK/watch" "$PREFIX/$version"
    cleanup
    log "/watch ${WATCH_COMMIT:0:12} (claude-video 0.3.2) and yt-dlp $YTDLP_VERSION installed as $PREFIX/$version"
    switch watch "$version"
}

# Expose havn-watch on the system PATH: agents see the host's /usr
# read-only, and the link resolves once $PREFIX/watch is bound into their
# namespace too. A file of that name that is not this pack's link is left
# alone. ffmpeg and ffprobe are not linked: the wrapper finds them in
# $PREFIX/ffmpeg/bin, and an ffmpeg the host has stays the one on PATH.
link_wrapper() {
    local dest="/usr/local/bin/havn-watch" target="$PREFIX/watch/bin/havn-watch" current
    if [ -L "$dest" ]; then
        current="$(readlink "$dest")"
        case "$current" in
            "$PREFIX"/*) ;;
            *) err "$dest points to $current, not into $PREFIX — left alone; agents run $target by its full path"; return ;;
        esac
    elif [ -e "$dest" ]; then
        err "$dest exists and is not a link — left alone; agents run $target by its full path"
        return
    fi
    ln -sfn "$target" "$PREFIX/.havn-watch.link"
    mv -T "$PREFIX/.havn-watch.link" "$dest"
    log "symlinked $target → $dest"
}

verify() {
    log "verifying"
    log "  ffmpeg:  $("$PREFIX/ffmpeg/bin/ffmpeg" -hide_banner -version | head -1)"
    log "  ffprobe: $("$PREFIX/ffmpeg/bin/ffprobe" -hide_banner -version | head -1)"
    log "  licence: $PREFIX/ffmpeg/LICENSE.txt (LGPL v3 or later) and COPYING.GPLv3"
    log "  yt-dlp:  $("$PREFIX/watch/bin/yt-dlp" --version)"
    log "  /watch:  $(cat "$PREFIX/watch/upstream/COMMIT") (MIT, $PREFIX/watch/upstream/LICENSE)"
    log "  havn-watch: $("$PREFIX/watch/bin/havn-watch" --help 2>&1 | head -1)"

    cat <<NEXT

Done. To enable video for an agent, make the install visible to agents in
the gateway's config (\$HAVN_CONFIG, else ~/.config/havn/config.toml of the
user the gateway runs as) and restart the gateway:

  [[extra_mounts]]
  path = "$PREFIX/ffmpeg"
  mode = "ro"

  [[extra_mounts]]
  path = "$PREFIX/watch"
  mode = "ro"

Raise the agent's resource_limits.pids_max to 128 (see the README), then
copy the skill into the agent's workspace, as root:

  WS=/root/.local/share/havn/agents/<agent-id>/workspace
  mkdir -p "\$WS/skills/video"
  cp $HERE/SKILL.md "\$WS/skills/video/SKILL.md"
  chown -R --reference="\$WS" "\$WS/skills/video"
  havn agent stop <agent-id> && havn agent start <agent-id>

Then in a webchat, send the agent a video and ask what is said or shown in
it. The agent's LLM picks up the SKILL.md from its workspace and knows what
to do. Transcribing the sound needs [transcription] in the gateway's config.
After an upgrade, restart the agents that use the pack: one still running
keeps the version it started with until the next upgrade removes it.
NEXT
}

main() {
    trap cleanup EXIT
    trap 'exit 130' INT TERM
    require_root
    require_tools
    require_glibc
    require_python
    mkdir -p "$PREFIX"
    # What an interrupted earlier run left.
    rm -rf "$PREFIX"/.ffmpeg.?????? "$PREFIX"/.watch.?????? "$PREFIX"/.ffmpeg.link "$PREFIX"/.watch.link "$PREFIX"/.havn-watch.link
    log "havn-pack-video setup → $PREFIX"
    install_ffmpeg
    install_watch
    link_wrapper
    verify
}

main "$@"
