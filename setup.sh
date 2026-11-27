#!/usr/bin/env bash
#
# havn-pack-video — host installer
#
# Installs, once on the host, for all havn agents on it:
#   /opt/havn/ffmpeg  ffmpeg + ffprobe (a pinned LGPL build of FFmpeg-Builds,
#                     its libraries linked in statically; needs only glibc
#                     >= 2.28) and a font for frame labels
#   /opt/havn/watch   the /watch skill of bradautomates/claude-video (MIT),
#                     pinned to one commit, in a Python venv with a pinned
#                     yt-dlp, and the havn-watch wrapper that runs it in an
#                     agent's sandbox
# Every download is over HTTPS and checked against its SHA-256 before it is
# used. Idempotent: re-running keeps what matches and replaces the rest.
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
# The directory a download is unpacked in, removed on exit.
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
    mkdir -p "$PREFIX"
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

# Whether $PREFIX/ffmpeg already holds this build and its binaries run.
ffmpeg_installed() {
    local build="$1"
    [ -f "$PREFIX/ffmpeg/BUILD" ] \
        && [ "$(cat "$PREFIX/ffmpeg/BUILD")" = "$build" ] \
        && [ -f "$PREFIX/ffmpeg/LICENSE.txt" ] \
        && "$PREFIX/ffmpeg/bin/ffmpeg" -hide_banner -version >/dev/null 2>&1 \
        && "$PREFIX/ffmpeg/bin/ffprobe" -hide_banner -version >/dev/null 2>&1
}

install_ffmpeg() {
    local found target sha archive build
    found="$(detect_target)"
    target="${found% *}"
    sha="${found#* }"
    archive="$FFMPEG_BUILD-$target-lgpl-$FFMPEG_BRANCH.tar.xz"
    build="$archive $sha"

    if ffmpeg_installed "$build"; then
        log "ffmpeg ${FFMPEG_BUILD#ffmpeg-} ($target, LGPL) already installed at $PREFIX/ffmpeg — skipping download"
        return
    fi

    require_space
    # Unpacked next to the install, on the same filesystem, so it is put in
    # place by renames; removed whatever happens.
    WORK="$(mktemp -d "$PREFIX/.ffmpeg.XXXXXX")"
    fetch "$FFMPEG_BASE_URL/$archive" "$WORK/ffmpeg.tar.xz" "$sha"

    mkdir -p "$WORK/ffmpeg"
    tar -xJf "$WORK/ffmpeg.tar.xz" -C "$WORK/ffmpeg" --strip-components=1 --no-same-owner \
        "${archive%.tar.xz}/bin/ffmpeg" "${archive%.tar.xz}/bin/ffprobe" "${archive%.tar.xz}/LICENSE.txt"
    rm -f "$WORK/ffmpeg.tar.xz"
    chmod 755 "$WORK/ffmpeg" "$WORK/ffmpeg/bin" "$WORK/ffmpeg/bin/ffmpeg" "$WORK/ffmpeg/bin/ffprobe"
    chmod 644 "$WORK/ffmpeg/LICENSE.txt"
    if ! "$WORK/ffmpeg/bin/ffmpeg" -hide_banner -version >/dev/null 2>&1; then
        err "the downloaded ffmpeg does not run on this host. Nothing was installed."
        exit 7
    fi
    echo "$build" > "$WORK/ffmpeg/BUILD"
    chmod 644 "$WORK/ffmpeg/BUILD"

    if [ -e "$PREFIX/ffmpeg" ]; then
        mv "$PREFIX/ffmpeg" "$WORK/previous"
    fi
    mv "$WORK/ffmpeg" "$PREFIX/ffmpeg"
    cleanup
    log "ffmpeg ${FFMPEG_BUILD#ffmpeg-} ($target, LGPL) installed at $PREFIX/ffmpeg"
}

# DejaVu Sans Mono Bold (DejaVu 2.37), shipped in this repo, for the time
# labels on contact sheets.
install_font() {
    install -d -m 755 "$PREFIX/ffmpeg/fonts"
    install -m 644 "$HERE/fonts/DejaVuSansMono-Bold.ttf" "$HERE/fonts/LICENSE-DejaVu.txt" "$PREFIX/ffmpeg/fonts/"
    log "font installed at $PREFIX/ffmpeg/fonts/DejaVuSansMono-Bold.ttf"
}

# Whether $PREFIX/watch already holds this commit and these dependencies.
watch_installed() {
    local build="$1"
    [ -f "$PREFIX/watch/BUILD" ] \
        && [ "$(cat "$PREFIX/watch/BUILD")" = "$build" ] \
        && [ -f "$PREFIX/watch/upstream/skills/watch/scripts/watch.py" ] \
        && "$PREFIX/watch/venv/bin/yt-dlp" --version >/dev/null 2>&1
}

install_watch() {
    local build="claude-video $WATCH_COMMIT $WATCH_SHA256 yt-dlp $YTDLP_VERSION $YTDLP_SHA256 yt-dlp-ejs $EJS_VERSION $EJS_SHA256"
    if watch_installed "$build"; then
        log "/watch ${WATCH_COMMIT:0:12} and yt-dlp $YTDLP_VERSION already installed at $PREFIX/watch — skipping download"
        return
    fi

    require_space
    WORK="$(mktemp -d "$PREFIX/.watch.XXXXXX")"
    fetch "$WATCH_URL" "$WORK/watch.tar.gz" "$WATCH_SHA256"
    mkdir -p "$WORK/upstream"
    tar -xzf "$WORK/watch.tar.gz" -C "$WORK/upstream" --strip-components=1 --no-same-owner \
        "claude-video-$WATCH_COMMIT/skills/watch" "claude-video-$WATCH_COMMIT/LICENSE" \
        "claude-video-$WATCH_COMMIT/README.md" "claude-video-$WATCH_COMMIT/CHANGELOG.md"
    echo "$WATCH_COMMIT" > "$WORK/upstream/COMMIT"
    chmod -R u=rwX,go=rX "$WORK/upstream"
    printf 'yt-dlp==%s --hash=sha256:%s\nyt-dlp-ejs==%s --hash=sha256:%s\n' \
        "$YTDLP_VERSION" "$YTDLP_SHA256" "$EJS_VERSION" "$EJS_SHA256" > "$WORK/requirements.txt"

    # A venv holds its own path, so it is made where it stays. BUILD is
    # written last: an install cut short is redone on the next run.
    rm -rf "$PREFIX/watch"
    install -d -m 755 "$PREFIX/watch"
    mv "$WORK/upstream" "$PREFIX/watch/upstream"
    log "making the venv at $PREFIX/watch/venv"
    python3 -m venv "$PREFIX/watch/venv"
    "$PREFIX/watch/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
        --no-deps --only-binary :all: --require-hashes -r "$WORK/requirements.txt"
    chmod -R go=rX "$PREFIX/watch/venv"
    cleanup
    echo "$build" > "$PREFIX/watch/BUILD"
    chmod 644 "$PREFIX/watch/BUILD"
    log "/watch ${WATCH_COMMIT:0:12} (claude-video 0.3.2) and yt-dlp $YTDLP_VERSION installed at $PREFIX/watch"
}

# The wrapper is this repo's: put in place on every run.
install_wrapper() {
    install -m 644 "$HERE/havn_watch.py" "$PREFIX/watch/havn_watch.py"
    install -d -m 755 "$PREFIX/watch/bin"
    cat > "$PREFIX/watch/bin/havn-watch" <<SH
#!/bin/sh
exec "$PREFIX/watch/venv/bin/python" -I "$PREFIX/watch/havn_watch.py" "\$@"
SH
    chmod 755 "$PREFIX/watch/bin/havn-watch"
    log "havn-watch wrapper installed at $PREFIX/watch/bin/havn-watch"
}

# Expose on the system PATH: agents see the host's /usr read-only, and the
# links resolve once $PREFIX/ffmpeg and $PREFIX/watch are bound into their
# namespace too. A real file of that name (installed otherwise) is left alone.
link_binaries() {
    local target dest
    for target in "$PREFIX/ffmpeg/bin/ffmpeg" "$PREFIX/ffmpeg/bin/ffprobe" "$PREFIX/watch/bin/havn-watch"; do
        dest="/usr/local/bin/$(basename "$target")"
        if [ -e "$dest" ] && [ ! -L "$dest" ]; then
            err "$dest exists and is not a link — left alone; agents run $target by its full path"
            continue
        fi
        ln -sfn "$target" "$dest"
        log "symlinked $target → $dest"
    done
}

verify() {
    log "verifying"
    log "  ffmpeg:  $("$PREFIX/ffmpeg/bin/ffmpeg" -hide_banner -version | head -1)"
    log "  ffprobe: $("$PREFIX/ffmpeg/bin/ffprobe" -hide_banner -version | head -1)"
    log "  licence: $PREFIX/ffmpeg/LICENSE.txt (LGPL v3 or later)"
    log "  yt-dlp:  $("$PREFIX/watch/venv/bin/yt-dlp" --version)"
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

Then copy the skill into the agent's workspace, as root:

  WS=/root/.local/share/havn/agents/<agent-id>/workspace
  mkdir -p "\$WS/skills/video"
  cp $HERE/SKILL.md "\$WS/skills/video/SKILL.md"
  chown -R --reference="\$WS" "\$WS/skills/video"
  havn agent stop <agent-id> && havn agent start <agent-id>

Then in a webchat, send the agent a video and ask what is said or shown in
it. The agent's LLM picks up the SKILL.md from its workspace and knows what
to do. Transcribing the sound needs [transcription] in the gateway's config.
NEXT
}

main() {
    trap cleanup EXIT
    trap 'exit 130' INT TERM
    require_root
    require_tools
    require_glibc
    require_python
    # What an interrupted earlier run left.
    mkdir -p "$PREFIX"
    rm -rf "$PREFIX"/.ffmpeg.?????? "$PREFIX"/.watch.??????
    log "havn-pack-video setup → $PREFIX"
    install_ffmpeg
    install_font
    install_watch
    install_wrapper
    link_binaries
    verify
}

main "$@"
