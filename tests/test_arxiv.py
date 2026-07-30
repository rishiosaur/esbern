from __future__ import annotations

from unittest.mock import patch

from esbern.arxiv import (
    ArxivPaper,
    _api_params,
    _query_api,
    _safe_filename,
    fetch_arxiv,
    looks_like_paper_identifier,
)

ATOM_RESULT = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762</id>
    <title>Attention Is All You Need</title>
    <author><name>Ashish Vaswani</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762" rel="related"
          type="application/pdf" />
  </entry>
</feed>
"""


class FakePdfResponse:
    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int):
        yield b"%PDF-1.7\n"
        yield b"paper"


class FakeApiResponse:
    def __init__(self, status_code: int, text: str = "", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


def test_recognizes_arxiv_ids_urls_and_dois() -> None:
    assert looks_like_paper_identifier("arXiv:1706.03762")
    assert looks_like_paper_identifier("https://arxiv.org/abs/1706.03762")
    assert looks_like_paper_identifier("https://doi.org/10.48550/arXiv.1706.03762")
    assert not looks_like_paper_identifier("Attention Is All You Need")
    assert _api_params("arXiv:1706.03762")["id_list"] == "1706.03762"


@patch("esbern.arxiv.requests.get", return_value=FakePdfResponse())
@patch("esbern.arxiv._query_api", return_value=ATOM_RESULT)
def test_fetches_matching_arxiv_pdf(query_api, get, tmp_path) -> None:
    updates = []

    result = fetch_arxiv(
        "Attention Is All You Need Ashish Vaswani",
        tmp_path,
        lambda status, detail: updates.append((status, detail)),
    )

    assert result.paper.arxiv_id == "1706.03762"
    assert result.path.read_bytes().startswith(b"%PDF-")
    assert result.path.name == "Ashish Vaswani - Attention Is All You Need (2017).pdf"
    assert get.call_args.args[0] == "https://arxiv.org/pdf/1706.03762"
    assert any(status == "Searching arXiv…" for status, _ in updates)
    assert any(detail and "MB" in detail for _, detail in updates)


@patch("esbern.arxiv.time.sleep")
@patch("esbern.arxiv.requests.get")
def test_api_retries_rate_limits(get, sleep) -> None:
    get.side_effect = [
        FakeApiResponse(429, headers={"Retry-After": "4"}),
        FakeApiResponse(200, text=ATOM_RESULT),
    ]

    assert _query_api({"id_list": "1706.03762"}) == ATOM_RESULT

    assert get.call_count == 2
    assert any(call.args[0] >= 4 for call in sleep.call_args_list)


def test_safe_filename_bounds_many_authors() -> None:
    paper = ArxivPaper(
        arxiv_id="2401.12345",
        title="Short title",
        authors=tuple(f"Researcher {number:02d}" for number in range(40)),
        pdf_url="https://arxiv.org/pdf/2401.12345",
    )

    filename = _safe_filename(paper)

    assert len(filename.encode("utf-8")) <= 240
    assert "Short title" in filename
    assert filename.endswith(" (2024).pdf")
