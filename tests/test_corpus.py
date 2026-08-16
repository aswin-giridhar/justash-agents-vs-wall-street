from avws.corpus import build_index, filter_docs, load_index, search


def test_index_covers_all_four_companies():
    build_index()
    docs = load_index()
    assert len({d.ticker for d in docs}) == 4
    assert len(docs) > 1000, f"expected the full corpus, got {len(docs)}"


def test_every_doc_has_parsed_frontmatter():
    docs = load_index()
    missing = [d.path for d in docs if not d.published_at or not d.document_type]
    assert not missing[:5], f"frontmatter parse failed for e.g. {missing[:5]}"


def test_search_finds_adi_q3_guidance():
    hits = search(
        "third quarter fiscal 2026 outlook forecasting revenue adjusted EPS",
        ticker="ADI", doc_type="FILING", since="2026-05-01", k=5,
    )
    assert hits, "no hits for ADI Q3 guidance"
    joined = " ".join(chunk for _, _, chunk in hits)
    assert "3.9 billion" in joined


def test_metadata_filter_is_exact_not_a_hint():
    hits = search("net fees", ticker="HAS", k=10)
    assert hits
    assert all(doc.ticker == "HAS" for doc, _, _ in hits)


def test_filter_by_document_type():
    docs = filter_docs(ticker="DE", doc_type="SLIDE")
    assert docs
    assert all(d.document_type.upper() == "SLIDE" for d in docs)


def test_search_is_deterministic():
    a = search("comparable sales", ticker="HD", k=5)
    b = search("comparable sales", ticker="HD", k=5)
    assert [(d.path, round(s, 6)) for d, s, _ in a] == [
        (d.path, round(s, 6)) for d, s, _ in b
    ]
