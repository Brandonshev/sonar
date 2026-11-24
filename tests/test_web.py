"""Dashboard: auth, pages, and path safety. Runs the real HTTP server on a random local port."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from sonar import db, pipeline, web

NOW = int(time.time())


@pytest.fixture
def server(tmp_path, monkeypatch):
    dbfile = tmp_path / "s.db"
    conn = db.connect(dbfile)
    for i, (title, src, score) in enumerate([("Rocket <b>repo</b>", "github", 88), ("Show HN: Thing", "hackernews", 40),
                                             ("Odd source", "mastodon", 30)], 1):
        item = db.upsert_item(conn, src, {"kind": "x", "external_id": str(i), "title": title,
                                          "url": "https://github.com/a/rocket" if i < 3 else None, "category": "Rust"},
                              now=NOW - 3 * 86400)
        for h in range(0, 72, 6):
            db.add_snapshots(conn, item, {"stars" if src == "github" else "points": 100 + (72 - h) * score}, now=NOW - h * 3600)
        conn.execute("INSERT INTO scores VALUES (?, ?, ?, ?, ?)", (item, NOW - 60, score / 8, score, json.dumps(["stars 10 -> 500 in 24h"])))
    conn.execute("INSERT INTO alerts_sent (item_id, ts, level, score) VALUES (1, ?, 'big', 88)", (NOW - 30,))
    conn.commit()
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / "2026-11-25.html").write_text("<html>digest body</html>")
    (tmp_path / "secret.html").write_text("nope")

    monkeypatch.setenv("SONAR_DASH_PASSWORD_HASH", web.hash_password("correct horse battery"))
    monkeypatch.setenv("SONAR_SECRET", "s" * 64)
    web.Handler.site = web.Site(pipeline.load(), db_path=dbfile, digests_dir=digests)
    web.Handler.throttle = web.Throttle(limit=3)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)


def get(url, cookie=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"Cookie": cookie} if cookie else {})
    try:
        r = opener.open(req)
        return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def login(base, pw="correct horse battery"):
    code, _, h = get(base + "/login", data=f"password={pw}".encode())
    return code, (h.get("Set-Cookie") or "").split(";")[0]


def test_password_hash_roundtrip():
    h = web.hash_password("hunter2hunter2")
    assert h.startswith("scrypt$") and "hunter2" not in h
    assert web.check_password("hunter2hunter2", h) and not web.check_password("nope", h)
    assert not web.check_password("x", "garbage")


def test_session_signing():
    tok = web.make_session("k", NOW)
    assert web.valid_session(tok, "k", NOW)
    assert not web.valid_session(tok, "other", NOW)
    assert not web.valid_session(tok, "k", NOW + 31 * 86400)
    assert not web.valid_session("9999999999.forged", "k", NOW)


def test_pages_need_login(server):
    for path in ("/", "/health", "/item/1", "/digests", "/digest/2026-11-25", "/tab/GitHub"):
        code, _, h = get(server + path)
        assert code == 303 and h["Location"] == "/login", path


def test_login_and_pages(server):
    assert login(server, "wrong")[0] == 401
    code, cookie = login(server)
    assert code == 303 and cookie.startswith("sonar_session=")
    code, home, h = get(server + "/", cookie)
    assert code == 200 and "Rocket &lt;b&gt;repo&lt;/b&gt;" in home  # escaped
    assert "Sonar v" in home and "<svg class=\"spark\"" in home and "default-src 'none'" in h["Content-Security-Policy"]
    assert "github" in home and "hackernews" in home  # cross-source chips on the shared entity
    _, gh, _ = get(server + "/tab/GitHub", cookie)
    assert "Rocket" in gh and "Show HN" not in gh
    _, other, _ = get(server + "/tab/Other", cookie)
    assert "Odd source" in other and "Rocket" not in other
    _, item, _ = get(server + "/item/1", cookie)
    assert "github stars" in item and "hackernews points" in item and "No alerts" not in item
    _, hp, _ = get(server + "/health", cookie)
    assert "Health" in hp and "GitHub" in hp and "Hacker News" in hp
    _, dl, _ = get(server + "/digests", cookie)
    assert "/digest/2026-11-25" in dl
    assert get(server + "/digest/2026-11-25", cookie)[1] == "<html>digest body</html>"
    assert get(server + "/item/999", cookie)[0] == 404


def test_digest_route_is_path_safe(server):
    _, cookie = login(server)
    for bad in ("/digest/..%2Fsecret", "/digest/../secret.html", "/digest/2026-11-2x", "/tab/Nope"):
        assert get(server + bad, cookie)[0] == 404, bad


def test_login_throttle(server):
    for _ in range(3):
        assert login(server, "wrong")[0] == 401
    assert login(server)[0] == 429  # even the right password is refused while blocked


def test_plain_http_via_proxy_goes_to_https(server, monkeypatch):
    monkeypatch.setenv("SONAR_PUBLIC_URL", "https://sonar.example.com")
    req = urllib.request.Request(server + "/login", headers={"X-Forwarded-Proto": "http"})
    try:
        opener.open(req)
        assert False, "expected a redirect"
    except urllib.error.HTTPError as e:
        assert e.code == 301 and e.headers["Location"] == "https://sonar.example.com/login"
    code, _, h = get(server + "/login")  # https (no header) is served, with HSTS
    assert code == 200 and "max-age" in h["Strict-Transport-Security"]


def test_no_password_set_fails_closed(server, monkeypatch):
    monkeypatch.delenv("SONAR_DASH_PASSWORD_HASH")
    assert login(server)[0] == 503


def test_icons_and_manifest_are_public_and_complete(server):
    from pathlib import Path
    static = Path(web.__file__).parent / "static"
    manifest = json.loads((static / "manifest.webmanifest").read_text())
    for icon in manifest["icons"]:
        assert (static / icon["src"].lstrip("/")).is_file(), icon["src"]
    for path, ctype in (("/favicon.ico", "image/x-icon"), ("/apple-touch-icon.png", "image/png"),
                        ("/icon-512.png", "image/png"), ("/manifest.webmanifest", "application/manifest+json")):
        r = opener.open(server + path)  # no cookie: still served
        assert r.status == 200 and r.headers["Content-Type"] == ctype and "max-age" in r.headers["Cache-Control"], path
        assert r.read()
    assert get(server + "/static/../web.py")[0] in (303, 404)  # only the listed files are served
    _, page, _ = get(server + "/login")
    assert 'rel="manifest"' in page and 'rel="apple-touch-icon"' in page
