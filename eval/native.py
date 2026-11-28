import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

KEY: str = os.environ["OPENROUTER_API_KEY"]
MODEL: str = "google/gemini-3.8-flash"
TYPES: dict[str, str] = {".webm": "video/webm", ".mp4": "video/mp4"}


def prompt(questions: list[dict[str, str]]) -> str:
    lines: str = "\n".join(f"{n}. {q['q']}" for n, q in enumerate(questions, 1))
    return ("Answer these questions about the video. Reply with a numbered list, one answer per "
            "question, in the same order. Give times as MM:SS. If you cannot tell, say so instead "
            f"of guessing.\n\n{lines}")


def ask(video: Path, text: str) -> dict[str, Any]:
    data: str = base64.b64encode(video.read_bytes()).decode()
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "video_url", "video_url": {"url": f"data:{TYPES[video.suffix]};base64,{data}"}}]}],
        "usage": {"include": True},
    }
    request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    start: float = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            reply: dict[str, Any] = json.loads(response.read())
    except urllib.error.HTTPError as e:
        return {"answer": "", "error": f"HTTP {e.code}: {e.read().decode()[:400]}",
                "seconds": round(time.monotonic() - start, 1)}
    usage: dict[str, Any] = reply.get("usage") or {}
    return {"answer": reply["choices"][0]["message"].get("content") or "",
            "seconds": round(time.monotonic() - start, 1),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "cost": usage.get("cost"), "details": usage.get("prompt_tokens_details")}


def main() -> None:
    videos: Path = Path(sys.argv[1])
    out: Path = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for item in json.loads((Path(__file__).parent / "questions.json").read_text()):
        target: Path = out / f"A-{item['video'].split('.')[0]}.json"
        if target.exists():
            continue
        result: dict[str, Any] = ask(videos / item["video"], prompt(item["questions"]))
        result.update({"arm": "A", "video": item["video"], "model": MODEL, "bytes": (videos / item["video"]).stat().st_size})
        target.write_text(json.dumps(result, indent=1))
        print(f"A {item['video']}: {result['seconds']}s cost={result.get('cost')} {result.get('error', '')}", flush=True)


if __name__ == "__main__":
    main()
