import struct
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

FFMPEG: str = "/opt/havn/ffmpeg/bin/ffmpeg"
FONT: str = "/opt/havn/ffmpeg/fonts/DejaVuSansMono-Bold.ttf"
RATE: int = 22050
W: int = 854
H: int = 480


def speak(text: str, out: Path) -> list[int]:
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(out), text], check=True)
    with wave.open(str(out)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        raw: bytes = w.readframes(w.getnframes())
    return list(struct.unpack(f"<{len(raw) // 2}h", raw))


def soundtrack(length: float, lines: list[tuple[float, str]], work: Path) -> Path:
    samples: list[int] = [0] * int(length * RATE)
    for n, (at, text) in enumerate(lines):
        voice: list[int] = speak(text, work / f"line{n}.wav")
        start: int = int(at * RATE)
        end: int = min(len(samples), start + len(voice))
        samples[start:end] = voice[: end - start]
    out: Path = work / "track.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return out


def text_filter(work: Path, name: str, text: str, start: float, end: float, x: str, y: str,
                size: int, color: str = "white", box: str = "") -> str:
    path: Path = work / f"{name}.txt"
    path.write_text(text)
    boxing: str = f":box=1:boxcolor={box}:boxborderw=12" if box else ""
    return (f"drawtext=fontfile={FONT}:textfile={path}:expansion=none:fontsize={size}:"
            f"fontcolor={color}:x={x}:y={y}{boxing}:enable='between(t,{start},{end})'")


def background(scenes: list[tuple[float, float, str]]) -> str:
    parts: list[str] = [f"drawbox=x=0:y=0:w=iw:h=ih:color={c}:t=fill:enable='between(t,{a},{b})'"
                        for a, b, c in scenes]
    return ",".join(parts)


def render(name: str, length: float, fps: int, filters: list[str], track: Path, out_dir: Path,
           work: Path, sources: str = "", tail: str = "") -> Path:
    graph: Path = work / f"{name}.graph"
    graph.write_text(sources + "[0:v]" + ",".join(filters) + tail + "[v]")
    out: Path = out_dir / f"{name}.webm"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}:d={length}",
                    "-i", str(track), "-/filter_complex", str(graph),
                    "-map", "[v]", "-map", "1:a", "-c:v", "libvpx-vp9", "-crf", "38", "-b:v", "0",
                    "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
                    "-c:a", "libopus", "-b:a", "48k", "-t", str(length), str(out)], check=True)
    return out


def g1(out_dir: Path, work: Path) -> None:
    scenes: list[tuple[float, float, str]] = [(0, 10, "0x1f4fbf"), (10, 20, "0x1f9f3f"), (20, 30, "0xbf1f1f")]
    filters: list[str] = [background(scenes),
                          text_filter(work, "g1code", "CODE 4417", 12, 15, "(w-text_w)/2", "(h-text_h)/2", 72)]
    track: Path = soundtrack(30, [(2.0, "The secret word is lantern."),
                                  (21.0, "Seven boats left the harbor at dawn.")], work)
    render("g1", 30, 25, filters, track, out_dir, work)


def g2(out_dir: Path, work: Path) -> None:
    scenes: list[tuple[float, float, str]] = [(0, 50, "0x14204f"), (50, 100, "0x145a28"),
                                              (100, 160, "0x6a1414"), (160, 220, "0x4f1f6f"),
                                              (220, 300, "0x147070")]
    filters: list[str] = [background(scenes)]
    sources: str = "color=c=yellow:s=60x60:r=25:d=300,split=3[b0][b1][b2];"
    tail: str = "[s0]"
    for n, (a, b, rightward) in enumerate([(20, 30, True), (115, 125, False), (250, 260, True)]):
        x: str = f"(t-{a})/{b - a}*(W-60)" if rightward else f"(W-60)-(t-{a})/{b - a}*(W-60)"
        end: str = "" if n == 2 else f"[s{n + 1}]"
        tail += f";[s{n}][b{n}]overlay=x='{x}':y=300:enable='between(t,{a},{b})'{end}"
    for at in [35, 72, 147, 185, 238, 284]:
        filters.append(f"drawbox=x=iw-110:y=30:w=80:h=80:color=red:t=fill:enable='between(t,{at},{at + 1})'")
    for n, (text, a) in enumerate([("STEP 1: CHECK THE VALVE", 40), ("STEP 2: OPEN THE GATE", 130),
                                   ("STEP 3: CALL MARTA", 270)]):
        filters.append(text_filter(work, f"g2step{n}", text, a, a + 4, "(w-text_w)/2", "h-110", 40,
                                   box="black@0.6"))
    track: Path = soundtrack(300, [(5, "Welcome to the harbor safety drill."),
                                   (80, "The pressure reading today is eighty two."),
                                   (195, "Remember, the meeting point is the north pier."),
                                   (290, "The drill is over. Thank you.")], work)
    render("g2", 300, 25, filters, track, out_dir, work, sources, tail)


def g3(out_dir: Path, work: Path) -> None:
    titles: list[str] = ["AGENDA", "MARKETS", "TEAM", "BUDGET", "REVENUE", "COSTS", "OFFICES", "HIRING",
                         "RISKS", "ROADMAP", "PROJECTS", "SUMMARY"]
    starts: list[float] = [0, 90, 190, 240, 380, 465, 540, 690, 780, 910, 1020, 1120]
    filters: list[str] = [background([(0, 1200, "0x202830")])]
    for n, (title, a) in enumerate(zip(titles, starts)):
        b: float = starts[n + 1] if n + 1 < len(starts) else 1200
        filters.append(text_filter(work, f"g3s{n}", f"SLIDE {n + 1}", a, b, "60", "60", 36, color="0xa0a0a0"))
        filters.append(text_filter(work, f"g3t{n}", title, a, b, "(w-text_w)/2", "(h-text_h)/2", 80))
    filters.append(text_filter(work, "g3alert", "ALERT: DOOR 9 OPEN", 862, 865, "(w-text_w)/2", "h-100", 40,
                               color="black", box="yellow"))
    track: Path = soundtrack(1200, [(10, "This is the quarterly review."),
                                    (305, "Revenue grew by twelve percent."),
                                    (640, "Our new office opens in Lisbon."),
                                    (990, "The code name for the project is falcon."),
                                    (1170, "That is all for today.")], work)
    render("g3", 1200, 10, filters, track, out_dir, work)


def g4(out_dir: Path, work: Path) -> None:
    code: list[tuple[str, float, float]] = [
        ("def total_price(items, tax):", 8, 120),
        ("    subtotal = sum(item.price for item in items)", 16, 120),
        ("    return subtotal * (1 + tax)", 24, 80),
        ("    return round(subtotal * (1 + tax), 2)", 80, 120),
        ("print(total_price(cart, 0.08))", 34, 120),
    ]
    rows: list[int] = [0, 1, 2, 2, 4]
    filters: list[str] = [background([(0, 120, "0x1e1e1e")]),
                          text_filter(work, "g4file", "shop.py", 0, 120, "30", "20", 22, color="0x9090ff")]
    for n, ((text, a, b), row) in enumerate(zip(code, rows)):
        filters.append(text_filter(work, f"g4c{n}", text, a, b, "30", str(70 + row * 34), 22, color="0xd0ffd0"))
    filters.append("drawbox=x=0:y=300:w=iw:h=180:color=0x000000:t=fill:enable='between(t,50,120)'")
    terminal: list[tuple[str, float, float, int]] = [
        ("$ python shop.py", 50, 120, 0),
        ("43.199999999999996", 55, 120, 1),
        ("$ python shop.py", 90, 120, 2),
        ("43.2", 95, 120, 3),
    ]
    for n, (text, a, b, row) in enumerate(terminal):
        filters.append(text_filter(work, f"g4t{n}", text, a, b, "30", str(315 + row * 34), 22))
    track: Path = soundtrack(120, [(2, "Today we write a function that adds tax to a cart."),
                                   (62, "The result has too many decimal places."),
                                   (73, "So we round it to two places.")], work)
    render("g4", 120, 25, filters, track, out_dir, work)


# Openly licensed real videos, and the part of each that is used.
REAL: list[tuple[str, str, float | None]] = [
    ("r1", "https://upload.wikimedia.org/wikipedia/commons/transcoded/b/bc/"
           "Scanning_Electron_Microscope_%28SEM%29_Lecture_2.webm/"
           "Scanning_Electron_Microscope_%28SEM%29_Lecture_2.webm.480p.vp9.webm", None),
    ("r2", "https://upload.wikimedia.org/wikipedia/commons/transcoded/c/c0/Big_Buck_Bunny_4K.webm/"
           "Big_Buck_Bunny_4K.webm.480p.vp9.webm", 180),
    ("r3", "https://download.blender.org/durian/trailer/sintel_trailer-480p.mp4", None),
    ("r4", "https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov", 240),
]


def real(out_dir: Path, work: Path) -> None:
    for name, url, length in REAL:
        source: Path = work / url.rsplit("/", 1)[1]
        if not source.exists():
            request = urllib.request.Request(url, headers={"User-Agent": "havn-pack-video-eval"})
            with urllib.request.urlopen(request, timeout=600) as response:
                source.write_bytes(response.read())
        if name == "r3":
            (out_dir / "r3.mp4").write_bytes(source.read_bytes())
            continue
        cut: list[str] = ["-t", str(length)] if length else []
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *cut, "-i", str(source),
                        "-c:v", "libvpx-vp9", "-crf", "38", "-b:v", "0", "-deadline", "realtime", "-cpu-used", "8",
                        "-row-mt", "1", "-vf", "scale=-2:'min(480,ih)'", "-c:a", "libopus", "-b:a", "64k",
                        str(out_dir / f"{name}.webm")], check=True)


def main() -> None:
    out_dir: Path = Path(sys.argv[1])
    work: Path = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    for make in (g1, g2, g3, g4, real):
        make(out_dir, work)
    print("made", ", ".join(sorted(p.name for p in out_dir.iterdir() if p.is_file())))


if __name__ == "__main__":
    main()
