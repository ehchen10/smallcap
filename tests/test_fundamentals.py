from smallcap import fundamentals
from smallcap.sec import concepts


def _facts(tag, end):
    return {"us-gaap": {tag: {"units": {"USD": [{"start": "2017-01-01", "end": end, "val": 1.0, "filed": end, "accn": "a", "form": "10-K"}]}}}}


def test_freshest_prefers_recent_tag_over_first_in_chain():
    """Regression (found on CLDX): the first-present tag was a stale one the filer
    stopped using years ago, so FY/quarter figures were ~8 years old."""
    stale, fresh = concepts.REVENUE[0], concepts.REVENUE[1]
    facts = {"facts": {"us-gaap": {**_facts(stale[1], "2018-12-31")["us-gaap"], **_facts(fresh[1], "2026-09-30")["us-gaap"]}}}
    assert fundamentals._freshest(facts, concepts.REVENUE).tag == fresh[1]


def test_freshest_none_when_absent():
    assert fundamentals._freshest({"facts": {}}, concepts.REVENUE) is None
