#!/usr/bin/env python3
# havn-watch: runs the pinned /watch skill (bradautomates/claude-video, MIT) in
# a Havn agent's sandbox. Upstream files are used as they are; this wrapper
# only sets the environment and replaces two functions at run time:
# - no API key is read for speech: where a video has no captions, its sound
#   is cut into parts for the agent's own `transcribe` tool, which the gateway
#   runs, bills and audits;
# - the Gemini engine reads its key only from the GEMINI_API_KEY variable (an
#   agent secret), never from files, and runs only when asked for.
# It also keeps ffmpeg to one CPU, writes into the workspace, prints paths
# relative to it, and tiles the frames into labelled contact sheets.
from __future__ import annotations

import contextlib
import io
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, NoReturn

ROOT: Path = Path(__file__).resolve().parent
SCRIPTS: Path = ROOT / "upstream" / "skills" / "watch" / "scripts"
PREFIX: Path = ROOT.parent
FFMPEG_BIN: Path = PREFIX / "ffmpeg" / "bin"
FONT: Path = PREFIX / "ffmpeg" / "fonts" / "DejaVuSansMono-Bold.ttf"
VENV_BIN: Path = ROOT / "venv" / "bin"

# ffmpeg starts threads per CPU it may use; the agent's process count is capped
# (64 by default) and its runtime already holds many of them.
CPUS: int = 1
# Parts of sound for `transcribe`: short ones give finer times, long videos
# take fewer calls (10 a minute for a user's agents, 80 s each at most).
PART_SECONDS_SHORT: int = 60
PART_SECONDS_LONG: int = 300
LONG_SECONDS: float = 600.0
# Contact sheets: 4x4 cells, at most 1548 px wide.
SHEET_COLUMNS: int = 4
SHEET_ROWS: int = 4
CELL_LANDSCAPE: tuple[int, int] = (384, 216)
CELL_PORTRAIT: tuple[int, int] = (192, 342)
MARKER: str = "speech is left to the transcribe tool"
REFUSED: tuple[str, ...] = ("--whisper", "--cookies-from-browser")
FRAME_LINE: re.Pattern[str] = re.compile(r"^- `([^`]+)` \(t=([0-9:]+)")

PARTS: list[tuple[Path, float]] = []


def fail(message: str) -> NoReturn:
    print(f"havn-watch: {message}", file=sys.stderr)
    raise SystemExit(2)


def limit_cpus() -> None:
    allowed: list[int] = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, set(allowed[:CPUS]))


def prepare_environment() -> None:
    os.environ["PATH"] = os.pathsep.join([str(VENV_BIN), str(FFMPEG_BIN), os.environ.get("PATH", "")])
    os.environ["WATCH_WHISPER_BACKEND"] = "auto"
    os.environ["WATCH_ENGINE"] = "local"


def option(args: list[str], name: str) -> str | None:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def checked_arguments(args: list[str]) -> list[str]:
    for arg in args:
        name: str = arg.split("=", 1)[0]
        if name in REFUSED:
            fail(f"{name} is not available here: speech goes to the transcribe tool, "
                 "and there is no browser to take cookies from")
    engine: str | None = option(args, "--engine")
    if engine == "gemini" and not os.environ.get("GEMINI_API_KEY", "").strip():
        fail("the Gemini engine needs a key: the agent's owner can set one as an agent secret "
             "with the variable GEMINI_API_KEY. Without it, leave out --engine.")
    if option(args, "--out-dir") is None:
        args = [*args, "--out-dir", "video"]
    return args


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", f"cannot run {command[0]}: {exc}")


def ffmpeg(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *arguments])


def duration_of(path: str) -> float:
    result: subprocess.CompletedProcess[str] = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    try:
        value: float = float(result.stdout.strip())
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def focus(args: list[str]) -> tuple[float, float | None]:
    from frames import parse_time
    start: float | None = parse_time(option(args, "--start"))
    end: float | None = parse_time(option(args, "--end"))
    return start or 0.0, end


def prepare_audio(args: list[str]) -> Callable[..., tuple[list[dict[str, Any]], str]]:
    def cut(video_path: str, audio_out: Path, backend: str | None = None,
            api_key: str | None = None) -> tuple[list[dict[str, Any]], str]:
        start, end = focus(args)
        total: float = duration_of(video_path)
        stop: float = min(end, total) if end is not None and total > 0 else (end or total)
        length: float = max(0.0, stop - start) if stop else 0.0
        part: int = PART_SECONDS_LONG if length > LONG_SECONDS else PART_SECONDS_SHORT
        directory: Path = audio_out.parent / "audio"
        directory.mkdir(parents=True, exist_ok=True)
        window: list[str] = ["-ss", f"{start:.3f}"] if start else []
        if end is not None:
            window += ["-to", f"{end:.3f}"]
        result: subprocess.CompletedProcess[str] = ffmpeg([
            *window, "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame",
            "-b:a", "32k", "-f", "segment", "-segment_time", str(part), "-reset_timestamps", "1",
            str(directory / "part-%03d.mp3")])
        if result.returncode != 0:
            raise SystemExit(f"ffmpeg could not extract the sound: {result.stderr.strip()[-500:]}")
        files: list[Path] = sorted(directory.glob("part-*.mp3"))
        if not files:
            raise SystemExit("ffmpeg extracted no sound")
        PARTS.extend((f, start + i * part) for i, f in enumerate(files))
        raise SystemExit(MARKER)
    return cut


def gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


def no_cloud_key(preferred: str | None = None) -> tuple[str, None]:
    return "havn", None


def clock(seconds: float) -> str:
    from frames import format_time
    return format_time(seconds)


def quoted(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def build_sheets(frames: list[tuple[str, str]], work: Path) -> list[tuple[Path, list[str]]]:
    if len(frames) < 2:
        return []
    probe: subprocess.CompletedProcess[str] = run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", frames[0][0]])
    try:
        width, height = (int(v) for v in probe.stdout.strip().split(",")[:2])
    except ValueError:
        return []
    cw, ch = CELL_PORTRAIT if height > width else CELL_LANDSCAPE
    cells: Path = work / "cells"
    cells.mkdir(parents=True, exist_ok=True)
    per_sheet: int = SHEET_COLUMNS * SHEET_ROWS
    count: int = math.ceil(len(frames) / per_sheet) * per_sheet
    fit: str = f"scale={cw}:{ch}:force_original_aspect_ratio=decrease,pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2"
    for i in range(count):
        cell: Path = cells / f"c-{i:03d}.jpg"
        if i < len(frames):
            path, label = frames[i]
            text: Path = cells / f"c-{i:03d}.txt"
            text.write_text(label, encoding="utf-8")
            draw: str = (f",drawtext=fontfile={quoted(str(FONT))}:textfile={quoted(str(text))}:x=4:y=4:"
                         "fontsize=20:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=3"
                         if FONT.is_file() else "")
            result: subprocess.CompletedProcess[str] = ffmpeg(
                ["-i", path, "-frames:v", "1", "-vf", fit + draw, "-q:v", "4", str(cell)])
        else:
            result = ffmpeg(["-f", "lavfi", "-i", f"color=c=white:s={cw}x{ch}", "-frames:v", "1",
                             "-q:v", "4", str(cell)])
        if result.returncode != 0:
            print(f"[havn-watch] contact sheets skipped: {result.stderr.strip()[-300:]}", file=sys.stderr)
            return []
    result = ffmpeg(["-framerate", "1", "-i", str(cells / "c-%03d.jpg"), "-vf",
                     f"tile={SHEET_COLUMNS}x{SHEET_ROWS}:padding=4:color=white", "-q:v", "4",
                     str(work / "sheet-%02d.jpg")])
    shutil.rmtree(cells, ignore_errors=True)
    if result.returncode != 0:
        print(f"[havn-watch] contact sheets skipped: {result.stderr.strip()[-300:]}", file=sys.stderr)
        return []
    sheets: list[tuple[Path, list[str]]] = []
    for s in range(count // per_sheet):
        labels: list[str] = [label for _, label in frames[s * per_sheet:(s + 1) * per_sheet]]
        sheets.append((work / f"sheet-{s + 1:02d}.jpg", labels))
    return sheets


class Progress(io.TextIOBase):
    def __init__(self, target: Any) -> None:
        self.target: Any = target
        self.pending: str = ""

    def write(self, text: str) -> int:
        self.pending += text
        *lines, self.pending = self.pending.split("\n")
        for line in lines:
            if MARKER not in line:
                self.target.write(line + "\n")
        return len(text)

    def flush(self) -> None:
        if self.pending and MARKER not in self.pending:
            self.target.write(self.pending)
        self.pending = ""
        self.target.flush()


def relative(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base))
    except ValueError:
        return str(path)


def rewrite(report: str, base: Path) -> str:
    report = report.replace(f"{base}{os.sep}", "")
    lines: list[str] = report.split("\n")
    out: list[str] = []
    frames: list[tuple[str, str]] = []
    work: Path | None = None
    for line in lines:
        if line == f"- Transcription failed: {MARKER}":
            continue
        if line == "- **Transcript:** failed" and PARTS:
            line = "- **Transcript:** not made yet: transcribe the audio parts below with the `transcribe` tool"
        if line == "_Failed._" and PARTS:
            line = "_Not made yet: see **Audio for transcribe** below._"
        line = line.replace("with the Read tool to view the image", "with `view_image` to see it")
        match: re.Match[str] | None = FRAME_LINE.match(line)
        if match:
            frames.append((match.group(1), match.group(2)))
        if line.startswith("_Work dir: `"):
            work = Path(line.split("`")[1])
        out.append(line)
    text: str = "\n".join(out)
    text = re.sub(r"\n## Unavailable evidence\n(?=\n---|\s*$)", "\n", text)
    if "## Unavailable evidence" not in text:
        text = text.replace("- **Result:** partial evidence\n", "").replace("- **Result:** unavailable evidence\n", "")
    extra: list[str] = []
    if work is not None and frames:
        absolute: Path = work if work.is_absolute() else base / work
        sheets: list[tuple[Path, list[str]]] = build_sheets(
            [(str(base / p) if not Path(p).is_absolute() else p, t) for p, t in frames], absolute)
        if sheets:
            extra += ["", "## Contact sheets", "",
                      f"The frames above, {SHEET_COLUMNS}x{SHEET_ROWS} to a sheet in order, left to right "
                      "and top to bottom, each with its time at its top left. View the sheets with "
                      "`view_image` for an overview, and single frames for detail and text.", ""]
            for sheet, labels in sheets:
                extra.append(f"- `{relative(sheet, base)}`: {labels[0]} to {labels[-1]} ({len(labels)} frames)")
    if PARTS:
        extra += ["", "## Audio for transcribe", "",
                  "No captions: the sound is cut into these parts. Transcribe each with the `transcribe` "
                  "tool, in order, and put each part's start time before its text as `[MM:SS]`.", ""]
        for path, start in PARTS:
            extra.append(f"- `{relative(path, base)}` starts at {clock(start)}")
    if extra:
        marker: str = "\n---\n_Work dir:"
        at: int = text.rfind(marker)
        block: str = "\n".join(extra) + "\n"
        text = text[:at] + "\n" + block + text[at:] if at >= 0 else text + "\n" + block
    return re.sub(r"\n{3,}", "\n\n", text)


def main() -> int:
    if not (SCRIPTS / "watch.py").is_file():
        fail(f"the /watch scripts are not at {SCRIPTS}: run havn-pack-video's setup.sh")
    limit_cpus()
    prepare_environment()
    args: list[str] = checked_arguments(sys.argv[1:])
    sys.path.insert(0, str(SCRIPTS))
    import watch
    from runtime import configure_stdio
    configure_stdio()
    watch.load_api_key = no_cloud_key
    watch.load_gemini_key = gemini_key
    watch.transcribe_video = prepare_audio(args)
    sys.argv = ["watch", *args]
    captured: io.StringIO = io.StringIO()
    code: int = 0
    progress: Progress = Progress(sys.stderr)
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(progress):
        try:
            code = watch.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code is not None:
                print(f"havn-watch: {exc.code}", file=sys.stderr)
        except (watch.ConfigError, OSError) as exc:
            print(f"havn-watch: {exc}", file=sys.stderr)
            code = 1
    progress.flush()
    sys.stdout.write(rewrite(captured.getvalue(), Path.cwd().resolve()))
    return 0 if PARTS else code


if __name__ == "__main__":
    raise SystemExit(main())
