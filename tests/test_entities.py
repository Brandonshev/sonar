import pytest

from sonar.entities import entity_key, normalize_url


@pytest.mark.parametrize("url,key", [
    ("https://github.com/Owner/Repo", "github:owner/repo"),
    ("https://www.github.com/owner/repo.git", "github:owner/repo"),
    ("https://github.com/owner/repo/issues/12#x", "github:owner/repo"),
    ("https://github.com/topics/ai", None),
    ("https://github.com/owner", None),
    ("https://www.Example.com/", "site:example.com"),
    ("https://example.com/?utm_source=hn&ref=ph", "site:example.com"),
    ("http://m.example.com/blog/post/?utm_medium=x#top", "url:example.com/blog/post"),
    ("https://youtube.com/watch?v=abc&feature=share", "url:youtube.com/watch?feature=share&v=abc"),
    ("", None),
    (None, None),
    ("not a url", None),
])
def test_entity_key(url, key):
    assert entity_key(url) == key


def test_normalize_same_page_variants():
    a = normalize_url("https://www.example.com/a/?utm_campaign=z")
    assert a == normalize_url("http://example.com/a") == "example.com/a"


def test_latin_title():
    from sonar.entities import latin_title
    assert latin_title("Mini Polaroid Towel Cleaning ASMR! #diy 😳")
    assert latin_title("¿Qué pasó? Día 30")
    assert not latin_title("एक खरगोश की वजह से गिर गया पूरा शहर 🐇 #shorts")
    assert not latin_title("예상치 못한 반전이 있는 댄스 챌린지")
    assert latin_title(None)
