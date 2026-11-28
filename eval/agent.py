import asyncio
import json
import os
import sys
import time
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import websockets

GW: str = "http://127.0.0.1:8080"
TURN: float = 2400


def get(path: str) -> Any:
    with urllib.request.urlopen(GW + path, timeout=30) as response:
        return json.loads(response.read())


def key_usage() -> float:
    request = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={
        "Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]})
    with urllib.request.urlopen(request, timeout=30) as response:
        return float(json.loads(response.read())["data"]["usage"])


def spent() -> dict[str, Any]:
    view: dict[str, Any] = get("/usage?days=1&user=all")
    return {"calls": view.get("calls"), "tokens": view.get("tokens"), "cost": view.get("cost"),
            "by_model": {b["key"]: {"calls": b.get("calls"), "tokens": b.get("tokens"), "cost": b.get("cost")}
                         for b in view.get("by_model", [])}}


def prompt(video: str, questions: list[dict[str, str]]) -> str:
    lines: str = "\n".join(f"{n}. {q['q']}" for n, q in enumerate(questions, 1))
    return (f"The video videos/{video} is in your workspace. Watch it and answer these questions "
            "about it. Reply with a numbered list, one answer per question, in the same order. Give "
            "times as MM:SS. If you cannot tell, say so instead of guessing.\n\n" + lines)


async def turn(agent: str, text: str) -> dict[str, Any]:
    token: str = get("/me")["ws_token"]
    url: str = f"ws://127.0.0.1:8080/ws/chat/{agent}?token={token}"
    chat: str = str(uuid.uuid4())
    steps: list[dict[str, str]] = []
    start: float = time.monotonic()
    async with websockets.connect(url, origin="http://127.0.0.1:3000", max_size=None) as ws:
        await ws.send(json.dumps({"type": "send", "content": text, "conversation": chat}))
        while True:
            left: float = TURN - (time.monotonic() - start)
            if left <= 0:
                await ws.send(json.dumps({"type": "stop", "conversation": chat}))
                return {"answer": "", "error": "timed out", "steps": steps}
            try:
                msg: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), left))
            except asyncio.TimeoutError:
                continue
            kind: str | None = msg.get("type")
            if msg.get("conversation") not in (chat, None):
                continue
            if kind == "approval":
                await ws.send(json.dumps({"type": "decide", "id": msg.get("id"), "approve": True, "remember": False}))
                steps.append({"tool": "approval", "detail": str(msg.get("tool"))})
            elif kind == "step":
                steps.append({"tool": str(msg.get("tool")), "detail": str(msg.get("detail"))[:300],
                              "at": f"{time.monotonic() - start:.0f}s"})
            elif kind == "agent_message":
                return {"answer": str(msg.get("content")), "steps": steps}
            elif kind == "error":
                return {"answer": "", "error": str(msg.get("message")), "steps": steps}


def main() -> None:
    out: Path = Path(sys.argv[1])
    arm: str = sys.argv[2]
    agent: str = sys.argv[3]
    only: set[str] = set(sys.argv[4:])
    out.mkdir(parents=True, exist_ok=True)
    for item in json.loads((Path(__file__).parent / "questions.json").read_text()):
        name: str = item["video"].split(".")[0]
        target: Path = out / f"{arm}-{name}.json"
        if target.exists() or (only and name not in only):
            continue
        before: dict[str, Any] = spent()
        key_before: float = key_usage()
        start: float = time.monotonic()
        result: dict[str, Any] = asyncio.run(turn(agent, prompt(item["video"], item["questions"])))
        result["seconds"] = round(time.monotonic() - start, 1)
        time.sleep(5)
        after: dict[str, Any] = spent()
        result["key_cost"] = round(key_usage() - key_before, 6)
        result["usage"] = {
            "calls": (after["calls"] or 0) - (before["calls"] or 0),
            "tokens": (after["tokens"] or 0) - (before["tokens"] or 0),
            "cost": round((after["cost"] or 0) - (before["cost"] or 0), 6),
            "by_model": {m: {k: (v.get(k) or 0) - (before["by_model"].get(m, {}).get(k) or 0)
                             for k in ("calls", "tokens", "cost")}
                         for m, v in after["by_model"].items()},
        }
        result["tools"] = dict(Counter(s["tool"] for s in result["steps"]))
        result.update({"arm": arm, "video": item["video"]})
        target.write_text(json.dumps(result, indent=1))
        print(f"{arm} {item['video']}: {result['seconds']}s tools={result['tools']} "
              f"cost={result['usage']['cost']} key={result['key_cost']} {result.get('error', '')}", flush=True)


if __name__ == "__main__":
    main()
