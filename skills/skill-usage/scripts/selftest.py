#!/usr/bin/env python3
"""Dependency-free self-tests for skill-usage.py pure functions. Run: python3 selftest.py"""
import os, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("su", os.path.join(HERE, "skill-usage.py"))
su = importlib.util.module_from_spec(spec)
spec.loader.exec_module(su)


def test_lint():
    good = su.lint_skill(name="seo", desc="Use when the user wants SEO audits and on-page work.", lines=150)
    assert good["desc_ok"] and good["body_ok"], good
    assert good["flags"] == [], good
    missing = su.lint_skill(name="x", desc="", lines=10)
    assert "desc_missing" in missing["flags"] and missing["desc_ok"] is False, missing
    notrig = su.lint_skill(name="x", desc="A short blurb about formatting that is reasonably long.", lines=10)
    assert "desc_no_trigger" in notrig["flags"], notrig
    big = su.lint_skill(name="x", desc="Use when doing X for the user.", lines=900)
    assert "body_oversize" in big["flags"] and big["body_ok"] is False, big


def test_baseline():
    assert su.heuristic_baseline(count=10, days_ago=0, flags=[], source="plugin", text="firecrawl scrape") == "keep"
    assert su.heuristic_baseline(count=3, days_ago=2, flags=["desc_no_trigger"], source="global", text="landing page") == "modernize"
    assert su.heuristic_baseline(count=0, days_ago=None, flags=[], source="global", text="investor outreach deck") == "turn_off"
    assert su.heuristic_baseline(count=0, days_ago=None, flags=[], source="global", text="docker compose patterns") == "prune"
    # plugin skills are never heuristic-"modernize" (we don't edit plugin caches)
    assert su.heuristic_baseline(count=8, days_ago=1, flags=["body_oversize"], source="plugin", text="hyperframes") == "keep"


def test_merge():
    rows = [
        {"skill": "docker-patterns", "baseline": "prune", "source": "global", "tok_desc": 40, "count": 0},
        {"skill": "seo", "baseline": "keep", "source": "global", "tok_desc": 68, "count": 5},
    ]
    recs = {"skills": {"seo": {"verdict": "modernize", "reason": "desc weak", "confidence": "high"}}}
    out = su.merge_recommendations(rows, recs)
    seo = next(r for r in out if r["skill"] == "seo")
    dk = next(r for r in out if r["skill"] == "docker-patterns")
    assert seo["rec_verdict"] == "modernize" and seo["rec_source"] == "claude", seo
    assert dk["rec_verdict"] == "prune" and dk["rec_source"] == "heuristic", dk
    agg = su.rec_aggregates(out)
    assert agg["counts"]["modernize"] == 1 and agg["counts"]["prune"] == 1, agg
    assert agg["token_impact"]["prune"] == 40, agg


if __name__ == "__main__":
    test_lint()
    test_baseline()
    test_merge()
    print("OK")
