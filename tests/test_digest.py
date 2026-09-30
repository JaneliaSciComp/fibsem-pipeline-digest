"""Self-checks for cleaning, Claude retry/JSON parsing, the HTML render and the CLI.

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
from fibsem_digest.__main__ import cli, default_since  # noqa: E402
from fibsem_digest.render import facts, render, timeline  # noqa: E402

START = D.dt("2026-09-01T00:00:00+00:00")


def snap(number, status, history, comments=(), body="", created="2026-08-01T00:00:00+00:00"):
    return {
        "issue": {
            "number": number, "title": f"jrc_ds{number}", "url": f"https://x/{number}",
            "repository": "org/repo", "labels": ["CellMap"], "assignees": ["alice"],
            "created_at": created, "body": body,
        },
        "status": status,
        "transitions": [{"from": None, "to": to, "at": at} for to, at in history],
        "comments": [
            {"author": {"login": "bob"}, "createdAt": at, "body": text} for at, text in comments
        ],
    }


def board(*datasets, since="2026-09-01T00:00:00+00:00", fetched="2026-09-16T09:00:00+00:00"):
    return {"fetched_at": fetched, "since": since, "board": "org/projects/1", "datasets": list(datasets)}


def test_clean():
    text = "Hi <b>there</b> ![img](x.png)\n> quoted\nsee [docs](http://u) or http://ng/#!%7B%22x%22\n```\ncode\n```\n\n\n\nOn Mon, X wrote:\n> old"
    out = D.clean_text(text)
    assert out == "Hi there [image]\n\nsee docs or [link]\n[code]", repr(out)

    s = snap(1, "Assembly", [("Assembly", "2026-08-20T00:00:00+00:00")],
             comments=[("2026-08-10T00:00:00+00:00", "x" * 1000), ("2026-09-10T00:00:00+00:00", "y" * 1000)])
    c = D.clean(s, START)
    assert c["comments"][0]["in_window"] is False and c["comments"][0]["text"].endswith(" …")
    assert c["comments"][1]["in_window"] is True and len(c["comments"][1]["text"]) == 1000
    s["status"] = "Done"
    assert len(D.clean(s, START)["comments"][0]["text"]) == 1000, "Done keeps full history"


def test_default_since():
    now = D.dt("2026-09-16T09:00:00+00:00")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        assert default_since(out, now) == D.dt("2026-09-02T09:00:00+00:00"), "no previous run: 14 days"
        (out / "2026-05-13_09-09-43").mkdir()  # old run without board.json is ignored
        for ts, fetched in [("2026-08-19_08-53-46", "2026-08-19T06:53:46+00:00"), ("2026-09-02_09-00-00", "2026-09-02T07:00:00+00:00")]:
            (out / ts).mkdir()
            (out / ts / "board.json").write_text(json.dumps(board(fetched=fetched)))
        assert default_since(out, now) == D.dt("2026-09-02T07:00:00+00:00")


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
    # Bounced back from Review to Assembly: ASM filled, REV still coloured, first ASM date kept.
    bounced = snap(7, "Assembly", [
        ("Assembly", "2026-08-01T00:00:00+00:00"), ("Review", "2026-08-15T00:00:00+00:00"),
        ("Assembly", "2026-09-10T00:00:00+00:00")],
        body='{"owner": "cellmap"}\n[imaging_preview](http://ng/7)')
    f = facts(bounced, START)
    assert f["owner"] == "cellmap" and f["preview"] == "http://ng/7" and f["collab"] == "CellMap"
    assert f["new"] is False
    tl = timeline(f, START)
    assert '<span class="stage imaging reached">' in tl, "left of current is coloured"
    assert 'assembly current"><i>ASM</i><small>Aug 1</small>' in tl, "first entry, not the bounce"
    assert 'review reached"><i>REV</i><small>Aug 15</small>' in tl, "right of current stays coloured"
    assert 'advanced-processing "><i>AP</i>' in tl and 'done "><i>DONE</i>' in tl

    done = snap(8, "Done", [("Review", "2026-08-20T00:00:00+00:00"), ("Done", "2026-09-12T00:00:00+00:00")],
                comments=[("2026-09-11T00:00:00+00:00", "signed off, see [state](http://ng/1) and http://ng/2")])
    fresh = snap(9, "Imaging", [("Imaging", "2026-09-05T00:00:00+00:00")], created="2026-09-04T00:00:00+00:00")
    assert facts(fresh, START)["new"] is True
    # Renamed columns map to today's names; a move too recent for the timeline uses the field timestamp.
    old = snap(10, "Done", [("Alignment", "2024-06-28T00:00:00+00:00"), ("R&D", "2026-07-22T00:00:00+00:00")])
    old["status_changed_at"] = "2026-09-15T12:00:00Z"
    e = facts(old, START)["entered"]
    assert e["Assembly"] == D.dt("2024-06-28T00:00:00+00:00") and "Alignment" not in e
    assert e["Advanced Processing"] == D.dt("2026-07-22T00:00:00+00:00")
    assert e["Done"] == D.dt("2026-09-15T12:00:00+00:00")
    digest = D.Digest.model_validate({
        "attention": [{"number": 7, "text": "waiting on **QC**"}],
        "datasets": {"7": {"progress": ["p1"], "blockers": ["b1"]}, "8": {"progress": ["finished"]}},
        "postmortem": {"8": {"well": ["w"], "bad": ["b"], "actions": ["a"]}},
    })
    html = render(board(done, bounced, fresh), digest)
    assert "Sep 1 → Sep 16, 2026" in html
    assert "<b>Sep 12</b>" in html, "transition in window is bold"
    assert '<a class="chip flagged" href="#ds-7"' in html and '<a class="chip " href="#ds-8"' in html
    assert html.count("<em>new</em>") == 1 and html.count('class="newtag"') == 1
    assert html.count('class="pm"') == 1 and "What went well" in html
    assert "waiting on <strong>QC</strong>" in html and "b1" in html
    assert "signed off, see state and [link]" in html and "http://ng/1" not in html and "http://ng/2" not in html
    assert html.index("ds-9") < html.index("ds-7") < html.index("ds-8"), "sorted by column"
    assert "footer" not in html
    # E-mail copy: collapsed header as an inline-styled template (rasterised in the browser),
    # links + bullets + activity as a plain-text template.
    mail = html.split('<template class="mail">')[1:]
    assert len(mail) == 3 and html.count('<button class="mail"') == 3
    m7 = next(m for m in mail if "#7</span>" in m).split("</template>")[0]
    assert 'class="' not in m7 and "b1" not in m7, "image part is header only"
    assert 'background:#8b5cf6">ASM' in m7 and 'color:#14b8a6">REV' in m7, "email timeline: current filled, reached outlined"
    t7 = next(m.split('<template class="mail-text">')[1].split("</template>")[0] for m in mail if "#7</span>" in m)
    assert t7.startswith("Issue: https://x/7\nPreview: http://ng/7\n\nPROGRESS\n- p1\n\nOPEN QUESTIONS / BLOCKERS\n- b1\n\nRaw GitHub activity (0 comments")
    t8 = next(m.split('<template class="mail-text">')[1].split("</template>")[0] for m in mail if "#8</span>" in m)
    assert "WHAT WENT WELL\n- w\n" in t8 and "2026-09-11  bob: signed off, see state and [link]" in t8


def test_cli_render():
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp)
        (run / "board.json").write_text(json.dumps(board(snap(1, "Imaging", [("Imaging", "2026-09-10T00:00:00+00:00")]))))
        (run / "digest.json").write_text('{"attention":[],"datasets":{"1":{"progress":["x"]}},"postmortem":{}}')
        r = CliRunner().invoke(cli, ["render", str(run)])
        assert r.exit_code == 0, r.output
        assert "<li>x</li>" in (run / "digest.html").read_text()


if __name__ == "__main__":
    test_clean()
    test_default_since()
    test_claude()
    test_render()
    test_cli_render()
    print("ok")
