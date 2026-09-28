"""Self-checks for cleaning, Claude retry/JSON parsing, and the HTML render.

Run directly: `uv run tests/test_digest.py`
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from click.testing import CliRunner  # noqa: E402

from fibsem_digest import digest as D  # noqa: E402
from fibsem_digest.__main__ import cli  # noqa: E402
from fibsem_digest.render import facts, render, timeline  # noqa: E402


def snap(number, status, history, prev="2026-09-01T00:00:00+00:00", comments=(), body=""):
    return {
        "last_pull_at": "2026-09-16T09:00:00+00:00",
        "previous_last_pull_at": prev,
        "issue": {
            "number": number, "title": f"jrc_ds{number}", "url": f"https://x/{number}",
            "repository": "org/repo", "labels": ["CellMap"], "assignees": ["alice"],
            "created_at": "2026-08-01T00:00:00+00:00", "body": body,
        },
        "current_status": status,
        "status_history": [
            {"from": None, "to": to, "changed_at": at, "detected_at": at} for to, at in history
        ],
        "comments": [
            {"id": str(i), "author": {"login": "bob"}, "createdAt": at, "body": text}
            for i, (at, text) in enumerate(comments)
        ],
    }


def test_clean():
    text = "Hi <b>there</b> ![img](x.png)\n> quoted\nsee [docs](http://u)\n```\ncode\n```\n\n\n\nOn Mon, X wrote:\n> old"
    out = D.clean_text(text)
    assert out == "Hi there [image]\n\nsee docs\n[code]", repr(out)

    start = D.dt("2026-09-01T00:00:00+00:00")
    s = snap(1, "Assembly", [("Assembly", "2026-08-20T00:00:00+00:00")],
             comments=[("2026-08-10T00:00:00+00:00", "x" * 1000), ("2026-09-10T00:00:00+00:00", "y" * 1000)])
    c = D.clean(s, start)
    assert c["comments"][0]["in_window"] is False and c["comments"][0]["text"].endswith(" …")
    assert c["comments"][1]["in_window"] is True and len(c["comments"][1]["text"]) == 1000
    s["current_status"] = "Done"
    assert len(D.clean(s, start)["comments"][0]["text"]) == 1000, "Done keeps full history"


def test_cycle_and_window():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / ".state.json").write_text("{}")
        a = snap(1, "Imaging", []); b = snap(2, "Imaging", [], prev=None)
        b["last_pull_at"] = "2026-09-16T09:00:05+00:00"  # same fetch, seconds apart
        old = snap(3, "Done", []); old["last_pull_at"] = "2026-09-02T00:00:00+00:00"
        for s in (a, b, old):
            (d / f"ds{s['issue']['number']}.json").write_text(json.dumps(s))
        cycle = D.load_cycle(d)
        assert sorted(s["issue"]["number"] for s in cycle) == [1, 2], cycle
        assert D.window_start(cycle) == D.dt("2026-09-01T00:00:00+00:00")
        assert D.window_start([b]) is None


def _fake_run(failures, calls, stdout="{}"):
    def run(*_a, **_k):
        calls.append(1)
        if len(calls) <= failures:
            raise subprocess.CalledProcessError(1, "claude", output="API overloaded", stderr="")
        return subprocess.CompletedProcess("claude", 0, stdout=stdout, stderr="")
    return run


def test_claude():
    D.time.sleep = lambda _s: None
    calls = []
    D.subprocess.run = _fake_run(2, calls)
    assert D.run_claude("p", "{}") == "{}" and len(calls) == 3
    calls = []
    D.subprocess.run = _fake_run(99, calls)
    try:
        D.run_claude("p", "{}")
    except RuntimeError as e:
        assert "API overloaded" in str(e) and f"after {D.MAX_ATTEMPTS}" in str(e)
    else:
        raise AssertionError
    assert len(calls) == D.MAX_ATTEMPTS

    reply = 'Here you go:\n```json\n{"attention":[{"number":5,"text":"stuck"}],"datasets":{"5":{"progress":["a"]}},"postmortem":{}}\n```'
    d = D.parse_digest(reply)
    assert d.attention[0].number == 5 and d.datasets[5].progress == ["a"] and d.datasets[5].blockers == []
    try:
        D.parse_digest("nope")
    except RuntimeError:
        pass
    else:
        raise AssertionError


def test_render():
    start = D.dt("2026-09-01T00:00:00+00:00")
    # Bounced back from Review to Assembly: ASM filled, REV still coloured, first ASM date kept.
    bounced = snap(7, "Assembly", [
        ("Assembly", "2026-08-01T00:00:00+00:00"), ("Review", "2026-08-15T00:00:00+00:00"),
        ("Assembly", "2026-09-10T00:00:00+00:00")],
        body='{"owner": "cellmap"}\n[imaging_preview](http://ng/7)')
    f = facts(bounced, start)
    assert f["owner"] == "cellmap" and f["preview"] == "http://ng/7" and f["collab"] == "CellMap"
    tl = timeline(f, start)
    assert '<span class="stage imaging reached">' in tl, "left of current is coloured"
    assert 'assembly current"><i>ASM</i><small>Aug 1</small>' in tl, "first entry, not the bounce"
    assert 'review reached"><i>REV</i><small>Aug 15</small>' in tl, "right of current stays coloured"
    assert 'advanced-processing "><i>AP</i>' in tl and 'done "><i>DONE</i>' in tl

    done = snap(8, "Done", [("Review", "2026-08-20T00:00:00+00:00"), ("Done", "2026-09-12T00:00:00+00:00")],
                comments=[("2026-09-11T00:00:00+00:00", "signed off")])
    digest = D.Digest.model_validate({
        "attention": [{"number": 7, "text": "waiting on **QC**"}],
        "datasets": {"7": {"progress": ["p1"], "blockers": ["b1"]}, "8": {"progress": ["finished"]}},
        "postmortem": {"8": {"well": ["w"], "bad": ["b"], "actions": ["a"]}},
    })
    html = render([done, bounced], digest)
    assert "<b>Sep 12</b>" in html, "transition in window is bold"
    assert '<a class="chip flagged" href="#ds-7"' in html and '<a class="chip " href="#ds-8"' in html
    assert html.count('class="pm"') == 1 and "What went well" in html
    assert "waiting on <strong>QC</strong>" in html and "b1" in html and "signed off" in html
    assert html.index("ds-7") < html.index("ds-8"), "sorted by column"
    assert "footer" not in html and "first seen by a fetch" not in html


def test_cli_render():
    with tempfile.TemporaryDirectory() as tmp:
        d, run = Path(tmp) / "data", Path(tmp) / "run"
        d.mkdir(); run.mkdir()
        (d / "ds1.json").write_text(json.dumps(snap(1, "Imaging", [("Imaging", "2026-09-10T00:00:00+00:00")])))
        (run / "digest.json").write_text('{"attention":[],"datasets":{"1":{"progress":["x"]}},"postmortem":{}}')
        r = CliRunner().invoke(cli, ["render", str(run), "--data-dir", str(d)])
        assert r.exit_code == 0, r.output
        assert "<li>x</li>" in (run / "digest.html").read_text()


if __name__ == "__main__":
    test_clean()
    test_cycle_and_window()
    test_claude()
    test_render()
    test_cli_render()
    print("ok")
