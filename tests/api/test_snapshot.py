"""The hosted control room: a committed snapshot of the Board, served read-only with its evidence files."""
import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from gtl.api.snapshot import hosted_app, restore_archive, take
from gtl.board import SqliteBoard
from gtl.contracts import Post


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def working_var(tmp_path):
    """A var/ folder as `gtl run` leaves it: a WAL Board, the file index, a mapping, a Gym run and noise."""
    var = tmp_path / "var"
    var.mkdir()
    board = SqliteBoard(str(var / "board.db"))
    board.append(Post("event", "new", "file.changed", {"path": "orders.csv"}, "watcher"))
    (var / "portal-current.json").write_text(json.dumps({"orders.csv": digest(b"id,qty\n1,5\n")}))
    (var / "mappings").mkdir()
    (var / "mappings" / "abc.json").write_text("{}")
    (var / "gym" / "night" / "best").mkdir(parents=True)
    (var / "gym" / "night" / "curve.json").write_text("{}")
    (var / "gym" / "night" / "best" / "resolver.md").write_text("rule")
    (var / "gym" / "night" / "gen_0.json").write_text("{}")
    (var / "raw" / ("0" * 64)).mkdir(parents=True)
    (var / "soony-watch.log").write_text("log")
    return var, board


def test_take_copies_the_board_in_one_file_and_only_what_the_screens_read(tmp_path):
    var, board = working_var(tmp_path)
    out = tmp_path / "snapshot"
    written = take(var, out, "acme")
    names = sorted(str(p.relative_to(out)) for p in written)
    assert names == ["board.db", "gym/night/best/resolver.md", "gym/night/curve.json", "mappings/abc.json",
                     "portal-current.json", "snapshot.json"]
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()) == names
    assert json.loads((out / "snapshot.json").read_text()) == {"tenant": "acme"}
    # The live Board keeps its write-ahead log; the copy has every post without one.
    with sqlite3.connect(out / "board.db") as copy:
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert [p.subject for p in SqliteBoard(str(out / "board.db")).query()] == ["file.changed"]


def test_take_refuses_a_var_without_a_board(tmp_path):
    (tmp_path / "var").mkdir()
    try:
        take(tmp_path / "var", tmp_path / "out", "acme")
    except FileNotFoundError as error:
        assert "board.db" in str(error)
    else:
        raise AssertionError("a snapshot without a Board was written")


def test_the_archive_is_rebuilt_from_the_mirror_only_for_unchanged_files(tmp_path):
    mirror = tmp_path / "mirror"
    (mirror / "suppliers").mkdir(parents=True)
    (mirror / "orders.csv").write_bytes(b"id\n1\n")
    (mirror / "suppliers" / "profile.json").write_bytes(b"{}")
    (mirror / "stock.csv").write_bytes(b"changed since the snapshot")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    index = {"orders.csv": digest(b"id\n1\n"), "suppliers/profile.json": digest(b"{}"),
             "stock.csv": digest(b"as indexed"), "gone.csv": digest(b""), "../escape.csv": digest(b"")}
    (snapshot / "portal-current.json").write_text(json.dumps(index))
    placed, skipped = restore_archive(snapshot, mirror)
    assert placed == 2 and sorted(skipped) == ["../escape.csv", "gone.csv", "stock.csv"]
    assert (snapshot / "raw" / index["orders.csv"] / "orders.csv").read_bytes() == b"id\n1\n"
    assert (snapshot / "raw" / index["suppliers/profile.json"] / "suppliers" / "profile.json").is_file()
    assert not (tmp_path / "escape.csv").exists()


def test_the_hosted_app_serves_the_screens_and_evidence_and_leaves_the_snapshot_untouched(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tenants" / "acme").mkdir(parents=True)
    (repo / "tenants" / "acme" / "tenant.toml").write_text(
        'id = "acme"\nname = "Acme"\ntimezone = "Europe/Madrid"\n[sources]\ndata_root = "mirror"\n')
    (repo / "mirror").mkdir()
    (repo / "mirror" / "orders.csv").write_bytes(b"id,qty\n1,5\n")
    var, board = working_var(tmp_path)
    snapshot = repo / "snapshot"
    take(var, snapshot, "acme")
    before = (snapshot / "board.db").read_bytes()
    monkeypatch.chdir(tmp_path)
    api = TestClient(hosted_app(repo, snapshot, tmp_path / "work"))
    assert api.get("/").status_code == 200
    assert api.get("/api/state").status_code == 200
    assert api.get("/api/integrations").json()["company"]["id"] == "acme"
    assert any(p["subject"] == "file.changed" for p in api.get("/api/feed").json())
    evidence = api.get("/api/evidence", params={"source": "orders.csv"})
    assert evidence.status_code == 200 and evidence.content == b"id,qty\n1,5\n"
    assert api.get("/api/evidence", params={"source": "missing.csv"}).status_code == 404
    assert api.post("/api/control/pause").status_code == 200
    assert (snapshot / "board.db").read_bytes() == before and not (snapshot / "raw").exists()
