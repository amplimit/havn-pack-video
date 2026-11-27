# Havn Pack Video

Video capability pack for [havn](https://github.com/amplimit/havn) agents — lets an
agent watch videos: files in its workspace, clips sent in a chat, or URLs.
It reads what is said (captions, or the sound through havn's `transcribe`
tool) and looks at timestamped frames (with havn's `view_image`), and can
cut clips.

Most models take still images only, and what to take from a video (which
frames, a clip, only the sound) depends on the task. So havn leaves video to
the agent ([amplimit/Havn#230](https://github.com/amplimit/Havn/issues/230)).
This pack gives it the [`/watch` skill](https://github.com/bradautomates/claude-video)
of claude-video by Bradley Bonanno (MIT), pinned to one commit, run through
a thin wrapper, `havn-watch`, that fits it to havn's sandbox, plus ffmpeg and
a skill (`SKILL.md`) that teaches the agent when and how to use it.

This is a **pack**, not a feature: havn ships zero skills, and capabilities
that don't fit havn's infrastructure-only mandate live in their own repos.
Install with one script and two config steps; uninstall by deleting two
directories.

## What this is, what it isn't

- A **bundled skill** (`SKILL.md`) that teaches an agent's LLM to run
  `havn-watch` through the `shell` tool it already has: get the transcript
  first (captions, else the sound in parts for `transcribe`), then a capped
  storyboard of frames and 4x4 contact sheets labelled with their times,
  then single frames only where the question needs them; to keep within
  image budgets, cite times and say what it did not see; and to cut clips
  with ffmpeg.
- A **setup script** that installs, once on the host, for every agent:
  a static ffmpeg + ffprobe build in `/opt/havn/ffmpeg` (pinned, checked by
  SHA-256, no distro package), and in `/opt/havn/watch` the `/watch`
  scripts at commit `03ceb42` (release 0.3.2, checked by SHA-256), a Python
  venv with pinned yt-dlp and yt-dlp-ejs wheels (installed by hash), and
  `havn-watch`.
- **`havn-watch`** (`havn_watch.py`) runs upstream's `watch.py` as it is.
  It only sets the environment and replaces two functions at run time:
  - **No API keys in the sandbox.** Upstream's own speech backends (Groq,
    OpenAI, local WhisperX) are never used. Captions still come first; a
    video without them gets its sound cut into mono MP3 parts within
    `transcribe`'s limits, and the agent transcribes them with havn's
    `transcribe` tool, which the gateway runs, bills and audits.
  - **Gemini only when the owner gives a key.** Upstream's Gemini engine
    (Google's model watches the whole video) runs only with
    `--engine gemini` and only when the agent's shell environment has
    `GEMINI_API_KEY`, which the owner sets as an agent secret; the key is
    never read from files. It sends the video to Google.
  - It also writes everything into the agent's workspace (`video/`), prints
    paths relative to it (for `view_image`), keeps ffmpeg on two CPUs (an
    agent's process count is capped, and ffmpeg starts a thread per CPU),
    and tiles the frames into contact sheets (havn shows at most 10 images
    in a request by default, so 16 frames to an image go further).
- **Not** a havn-core change. The gateway still hands a video sent in a chat
  to the agent as a file.
- **Not** a transcriber of its own: without `[transcription]` in the
  gateway's config (Havn 0.2.16 and later) the agent gets captions and
  frames only.

## Prereqs

- Linux on x86_64 or aarch64 with glibc 2.28 or later. Not Alpine or other
  musl systems.
- Python 3.11 or later with `venv` (Debian 12+, Ubuntu 24.04+: `python3`
  and `python3-venv`), as yt-dlp needs it.
- `curl`, `tar`, `xz`, `gzip`, `sha256sum` and `getconf` on the host
  (Debian/Ubuntu: `curl tar xz-utils gzip coreutils libc-bin`).
- About 750 MB free under `/opt/havn` during the install, 300 MB after it.
- A havn agent whose policy allows `can_use_shell: true` (default). To look
  at frames, a model with vision; to read speech that has no captions,
  Havn 0.2.16 or later with `[transcription]` set and `can_transcribe`
  allowed (see the havn README's "Voice messages"). For URLs, network
  access (see below).

## Install

### 1. Run the host installer

```bash
git clone https://github.com/amplimit/havn-pack-video /tmp/havn-pack-video
cd /tmp/havn-pack-video
sudo ./setup.sh                    # installs to /opt/havn, idempotent
```

It downloads the pinned ffmpeg build (about 140 MB) and the `/watch`
tarball over HTTPS and checks their SHA-256 before using them, makes the
venv and installs the two wheels with `pip --require-hashes`, and links
`/usr/local/bin/ffmpeg`, `ffprobe` and `havn-watch` to the install (a file
of that name that is not a link is left alone). Re-running it keeps what
matches the pins and replaces the rest. `HAVN_PACK_PREFIX` installs under
another directory instead of `/opt/havn`.

### 2. Make the install visible to agents

Agents see the host's `/usr` read-only, so the links in `/usr/local/bin`
are on their PATH, but `/opt/havn` is not bound into their namespace unless
the gateway config says so. Add this to your havn gateway config
(`$HAVN_CONFIG`, else `~/.config/havn/config.toml` of the user the gateway
runs as: `/root/.config/havn/config.toml` under havn's systemd unit) and
restart the gateway:

```toml
# Make the ffmpeg and /watch installs visible to agents. Mirror-bound at the
# same absolute path on the host and inside the agent namespace.
[[extra_mounts]]
path = "/opt/havn/ffmpeg"
mode = "ro"

[[extra_mounts]]
path = "/opt/havn/watch"
mode = "ro"
```

With other packs (havn-pack-browser), add these entries next to theirs.
Nothing else is needed: no tmpfs, no seccomp allowance.

The agent's default limits are enough for ordinary videos:
`resource_limits.memory_mb` 512 and `pids_max` 64 (why havn-watch keeps
ffmpeg on two CPUs). A long video or a download takes longer than the shell
tool's default 300 s: the skill runs such jobs in the background, within
the policy's `shell.max_timeout_seconds` (1800).

### 3. Drop the skill into agents that should work with video

Agent workspaces are under the gateway's `workspace_root`, by default
`~/.local/share/havn/agents` of the user the gateway runs as
(`/root/.local/share/havn/agents` under havn's systemd unit). Only root
can enter an agent's directory, and the workspace belongs to the
agent's own uid, so copy as root and hand the skill to that owner:

```bash
# replace <agent-id> with the UUID of the agent you want to enable video for
WS=/root/.local/share/havn/agents/<agent-id>/workspace
mkdir -p "$WS/skills/video"
cp SKILL.md "$WS/skills/video/SKILL.md"
chown -R --reference="$WS" "$WS/skills/video"
```

Restart the agent so the runtime indexes the new skill:

```bash
havn agent stop <agent-id> && havn agent start <agent-id>
```

The agent's LLM will now see the `video` skill in its retrieval set and pull
it into context whenever a message is about a video. Pin it on the agent's
skills page if the agent works with video only now and then, so the curator
does not archive it.

### 4. Optional: Gemini

To let the agent hand a whole video to Google's Gemini (useful for long
videos, or when motion or sound other than speech matters), set a Gemini
API key as an agent secret on the agent's settings page (**Secrets**) and
give it the variable `GEMINI_API_KEY` (or put `GEMINI_API_KEY=secret:<name>`
in its shell environment), and allow `generativelanguage.googleapis.com`
in its network access. The agent then may run `havn-watch --engine gemini`;
YouTube URLs are sent to Google as they are, other videos are uploaded to
Google's Files API and deleted after the answer. Without the variable the
Gemini engine is off.

## Network

For a URL, `havn-watch` runs yt-dlp inside the agent's sandbox, so it goes
through the agent's network access like any tool: with no egress it fails;
through the egress proxy (the default), each domain not yet allowed waits
for the owner's answer in the dashboard chat, and a site whose video comes
from CDNs needs those allowed too. Captions are fetched first, and a
transcript-only run downloads no video when there are any. YouTube may need
a JavaScript runtime (Deno, or Node with yt-dlp's `--js-runtimes`), which
this pack does not install; local files need no network at all.

## Verify

In a webchat session with the enabled agent, send a short video with speech:

```
You: (a video) 这段视频里说了什么，画面上写了什么？
Agent: (runs havn-watch, transcribes the audio parts, looks at the contact
        sheet and a frame or two, then answers with times)
```

If the agent says it cannot work with video, check:

1. The skill file exists at the right path (`workspace/skills/video/SKILL.md`).
2. The agent's policy has `can_use_shell: true`.
3. `havn-watch --help` works in a host shell, and the two `[[extra_mounts]]`
   entries are in the gateway config.
4. For the sound: `[transcription]` is set and the agent has the
   `transcribe` tool ("transcribe audio" on its settings page).

## Upgrading

Run a newer `setup.sh` from this repo, then restart the agents that use the
pack: one running during the upgrade keeps the old directories bound until
its next start. `havn update` (from Havn 0.2.17) lists the pack (as
`havn-pack-video`, by its `/opt/havn/ffmpeg`) but does not update it. Sites
change and break older yt-dlp releases; a pack release pins a newer one.

## Uninstall

```bash
sudo rm -rf /opt/havn/ffmpeg /opt/havn/watch
# the links, if they are this pack's
for b in ffmpeg ffprobe havn-watch; do
  case "$(readlink /usr/local/bin/$b)" in /opt/havn/*) sudo rm /usr/local/bin/$b ;; esac
done
```

Then remove the two `[[extra_mounts]]` entries from the gateway config and
restart the gateway, and have each agent delete the `video` skill with
`skill_manage` (a skill directory removed by hand stays in the agent's
index).

## What lives in this repo

```
SKILL.md            content injected into the agent's system prompt — teaches
                    the LLM what to take from a video for a task, havn-watch's
                    modes, budgets and pitfalls, and ffmpeg for clips
havn_watch.py       the havn-watch wrapper around the pinned /watch scripts
setup.sh            host-side installer for ffmpeg, /watch, its venv, havn-watch
fonts/              DejaVu Sans Mono Bold and its licence, for frame labels
README.md           you're reading it
NOTICE              third-party notices
LICENSE             Apache-2.0
```

## How this works under havn

havn-core ships **no** video-aware code. The pack uses one generic primitive,
the shell environment, and two core tools:

| havn piece | What this pack uses it for |
|------------|----------------------------|
| `[[extra_mounts]]` | Bind-mounts `/opt/havn/ffmpeg` and `/opt/havn/watch` into the agent namespace, so the `/usr/local/bin` links the agent sees resolve. |
| `view_image` | Shows the agent the frames and contact sheets (JPEG). |
| `transcribe` | Transcribes the audio parts, through the gateway's `[transcription]`: at most 20 MB a file, 80 s a call, 20,000 characters a transcript, 10 calls a minute; havn-watch cuts 60 s parts (300 s past 10 minutes). |
| Secrets for tools | An optional `GEMINI_API_KEY` for upstream's Gemini engine, set by the owner. |

## Licences

This repo's own files (the skill, the wrapper, the script, this README) are
licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE), and
[NOTICE](NOTICE) for the third-party notices.

**/watch (claude-video).** Copyright (c) 2026 Bradley Bonanno, MIT License.
`setup.sh` downloads it from GitHub at commit
`03ceb42f7fa2c4439aca01752118044baabffb8f` (SHA-256 of the tarball
`495ae96eeb96b3f3a50a94e1a8542b255ecf72ec5e42aa3a85190af22f4412ba`) and
installs its `skills/watch/`, `LICENSE`, `README.md` and `CHANGELOG.md`
unchanged under `/opt/havn/watch/upstream/`; `havn_watch.py` changes nothing
in them. The MIT notice is in [NOTICE](NOTICE).

**yt-dlp** 2026.10.26 (The Unlicense) and **yt-dlp-ejs** 0.8.0 (Unlicense
AND MIT AND ISC), from PyPI, by hash.

**DejaVu Sans Mono Bold** (DejaVu fonts 2.37, from the
[dejavu-fonts](https://github.com/dejavu-fonts/dejavu-fonts) release
`version_2_37`), shipped in `fonts/` under the Bitstream Vera and Arev
fonts licences, whose text is [fonts/LICENSE-DejaVu.txt](fonts/LICENSE-DejaVu.txt);
DejaVu's changes are in the public domain.

**ffmpeg.** `setup.sh` does not ship ffmpeg; it downloads it from its
builder. The build is the **LGPL** variant of
[FFmpeg-Builds](https://github.com/FFmpeg-Builds/FFmpeg-Builds) (formerly
BtbN/FFmpeg-Builds; its build scripts are MIT), release
`autobuild-2026-10-31-12-59`: FFmpeg **9.0.2**, the release/9.0 branch at
`n9.0.2-39-g7adfbe40dc`, for `linux64` and `linuxarm64`.

| Archive | SHA-256 |
|---------|---------|
| `ffmpeg-n9.0.2-39-g7adfbe40dc-linux64-lgpl-9.0.tar.xz` | `6046d6731b26f58887e953561551d254a6eec9fbca4100dfeab56e19af8328d5` |
| `ffmpeg-n9.0.2-39-g7adfbe40dc-linuxarm64-lgpl-9.0.tar.xz` | `3d1f66827a73ce5a446312bc4924234dc1515726d3743e2df35c0177e1f82128` |

It is configured with `--enable-version3` and without `--enable-gpl` or
`--enable-nonfree`, so the binaries are under the **GNU Lesser General
Public License, version 3 or later**; `ffmpeg -L` prints the notice and the
build's `LICENSE.txt` (the LGPL v3 text) is installed at
`/opt/havn/ffmpeg/LICENSE.txt`. The libraries linked into it are each under
their own licence, all of them compatible with the LGPL (`ffmpeg -buildconf`
lists them; FFmpeg-Builds' `scripts.d` names each one's source). The
corresponding source is FFmpeg's git at that commit
(`https://git.ffmpeg.org/ffmpeg.git`, tag `n9.0.2` plus 39 commits of
release/9.0) and FFmpeg-Builds' scripts at that release.

Why this build: it is the LGPL one, so it leaves out the GPL-only encoders
(x264, x265), which reading videos does not need (ffmpeg's own decoders read
H.264, HEVC and the rest; clips are re-encoded with OpenH264), and it does
not put the GPL's terms on operators who build it into images they hand on.
FFmpeg-Builds keeps each month's last build for two years, so the pinned URL
stays; its Linux builds link every library in statically except glibc.

If you redistribute these binaries yourself (say, in a machine image), the
LGPL's terms apply to you: pass on the licence and the notice, and offer the
corresponding source. Codec patents (H.264, HEVC, AAC) may apply in some
countries whatever the copyright licence; that is the operator's call.
