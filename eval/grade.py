import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

JUDGE: str = "anthropic/claude-sonnet-5.5"
RULES: str = """You grade answers about a video against reference answers.
Score each question 1 (correct), 0.5 (partly correct) or 0 (wrong, missing, or "cannot tell").
- A time is correct within {tol} seconds of the reference. When the reference gives a range, the answer's start must be within the tolerance.
- A count must be exact.
- Text shown on screen must have the same words (ignore case and punctuation).
- A question with several parts (a text and a time, a count and directions) gets 0.5 when only some parts are right.
- Judge only what the answer says, not how it was found. Extra detail is fine unless it contradicts the reference.
Reply with JSON only: a list of {{"n": <question number>, "score": <1|0.5|0>, "note": "<a few words>"}}."""


def judge(key: str, questions: list[dict[str, str]], answer: str, tolerance: int) -> list[dict[str, Any]]:
    reference: str = "\n".join(f"{n}. Q: {q['q']}\n   Reference: {q['a']}" for n, q in enumerate(questions, 1))
    body: dict[str, Any] = {
        "model": JUDGE,
        "temperature": 0,
        "messages": [{"role": "system", "content": RULES.format(tol=tolerance)},
                     {"role": "user", "content": f"Questions and references:\n{reference}\n\nThe answer to grade:\n{answer}"}],
    }
    request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        text: str = json.loads(response.read())["choices"][0]["message"]["content"]
    found: re.Match[str] | None = re.search(r"\[.*\]", text, re.S)
    if not found:
        raise ValueError("the judge's reply has no list of scores")
    scores: list[dict[str, Any]] = json.loads(found.group(0))
    if sorted(int(s["n"]) for s in scores) != list(range(1, len(questions) + 1)):
        raise ValueError("the judge's scores do not cover every question once")
    return scores


def answered(answer: str) -> bool:
    return bool(answer) and not re.search(r"\(Stopped after \d+ tool rounds", answer)


def main() -> None:
    results: Path = Path(sys.argv[1])
    key: str = os.environ["OPENROUTER_API_KEY"]
    sets: list[dict[str, Any]] = json.loads((Path(__file__).parent / "questions.json").read_text())
    for result in sorted(results.glob("*.json")):
        graded: Path = results.parent / "graded" / result.name
        if graded.exists():
            continue
        run: dict[str, Any] = json.loads(result.read_text())
        item: dict[str, Any] = next(s for s in sets if s["video"] == run["video"])
        tolerance: int = 2 if item["kind"] == "generated" else 3
        answer: str = run.get("answer") or ""
        scores: list[dict[str, Any]]
        if answered(answer):
            try:
                scores = judge(key, item["questions"], answer, tolerance)
            except (ValueError, KeyError, OSError) as e:
                print(f"{result.stem}: not graded ({e})", flush=True)
                continue
        else:
            note: str = ("no answer: stopped at the tool-round limit; interim narration not graded"
                         if answer else "no answer")
            scores = [{"n": n, "score": 0, "note": note} for n in range(1, len(item["questions"]) + 1)]
        graded.parent.mkdir(exist_ok=True)
        graded.write_text(json.dumps({"run": result.stem, "scores": scores}, indent=1))
        total: float = sum(float(s["score"]) for s in scores)
        print(f"{result.stem}: {total}/{len(item['questions'])}", flush=True)
    print(table(results, sets))


def table(results: Path, sets: list[dict[str, Any]]) -> str:
    rows: list[str] = ["| Video | Length | " + " | ".join(f"{arm} score | {arm} cost | {arm} time" for arm in "ABC") + " |",
                       "|---|---|" + "---|---|---|" * 3]
    totals: dict[str, list[float]] = {arm: [0.0, 0.0, 0.0, 0.0] for arm in "ABC"}
    for item in sets:
        name: str = item["video"].split(".")[0]
        cells: list[str] = [name, item["length"]]
        for arm in "ABC":
            run_path: Path = results / f"{arm}-{name}.json"
            graded_path: Path = results.parent / "graded" / f"{arm}-{name}.json"
            if not run_path.exists() or not graded_path.exists():
                cells += ["", "", ""]
                continue
            run: dict[str, Any] = json.loads(run_path.read_text())
            score: float = sum(float(s["score"]) for s in json.loads(graded_path.read_text())["scores"])
            cost: float = float((run.get("cost") if arm == "A" else run.get("key_cost")) or 0)
            count: int = len(item["questions"])
            cells += [f"{score:g}/{count}", f"${cost:.3f}", f"{run['seconds']:.0f} s"]
            for i, v in enumerate((score, count, cost, run["seconds"])):
                totals[arm][i] += v
        rows.append("| " + " | ".join(cells) + " |")
    cells = ["All", ""]
    for arm in "ABC":
        score, count, cost, seconds = totals[arm]
        cells += [f"{score:g}/{count:g} ({100 * score / count:.0f}%)" if count else "", f"${cost:.2f}",
                  f"{seconds / 60:.1f} min"]
    rows.append("| " + " | ".join(cells) + " |")
    return chr(10).join(rows)


if __name__ == "__main__":
    main()
