import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-10-14T00:00:00+00:00"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["title"], "Apple may have a way to dominate the smart home space")
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 13, 19, 46, 52,
                                                          tzinfo=datetime.timezone.utc))
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))

    def test_sec_atom_8k(self):
        items = news.parse_sec_atom(raw("sec-8-K-0000320193.xml"), "AAPL")
        self.assertEqual([i["form"] for i in items], ["8-K/A", "8-K", "8-K"])
        earn = items[1]
        self.assertEqual(earn["items"], ["2.02", "9.01"])
        self.assertTrue(earn["important"])
        self.assertEqual(earn["title"], "Apple Inc. filed a 8-K: earnings results")
        self.assertEqual(earn["id"], "sec:0000320193-26-000018")
        self.assertIn("000032019326000018", earn["link"])
        # 16:30:28 New York time is 20:30:28 UTC
        self.assertEqual(earn["time"].isoformat(), "2026-07-30T20:30:28+00:00")
        self.assertEqual(items[0]["title"], "Apple Inc. filed a 8-K/A: director or officer change")

    def test_times(self):
        self.assertEqual(news.parse_time("Tue, 13 Oct 2026 20:02:03 +0000").hour, 20)
        self.assertEqual(news.parse_time("2026-09-01T16:30:35-04:00").hour, 20)
        self.assertIsNone(news.parse_time("soon"))

    def test_yahoo_symbols(self):
        self.assertEqual(news.yahoo_symbol("BRK.B"), "BRK-B")
        self.assertEqual(news.yahoo_symbol("brk.b"), "BRK-B")
        # exchange suffixes keep their dot
        self.assertEqual(news.yahoo_symbol("EQNR.OL"), "EQNR.OL")
        self.assertEqual(news.yahoo_symbol("VOLV-B.ST"), "VOLV-B.ST")
        self.assertEqual(news.yahoo_symbol("VOLV B.ST"), "VOLV-B.ST")
        self.assertEqual(news.yahoo_symbol("BT.A.L"), "BT-A.L")
        self.assertEqual(news.yahoo_symbol("SAP.DE"), "SAP.DE")
        self.assertEqual(news.yahoo_symbol("SHOP.TO"), "SHOP.TO")

    def test_yahoo_region_from_suffix(self):
        self.assertIn("s=EQNR.OL&region=NO&", news.yahoo_url("EQNR.OL"))
        self.assertIn("s=SAP.DE&region=DE&", news.yahoo_url("SAP.DE"))
        self.assertIn("s=AAPL&region=US&", news.yahoo_url("AAPL"))
        self.assertIn("s=BRK-B&region=US&", news.yahoo_url("BRK.B"))
        self.assertEqual(news.venue("NOVO-B.CO"), "Nasdaq Copenhagen")
        self.assertEqual(news.venue("AAPL"), "US")
        self.assertFalse(news.is_foreign_listing("BRK.B"))
        self.assertTrue(news.is_foreign_listing("VOD.L"))


class European(unittest.TestCase):
    """Fixtures for EQNR.OL and SAP.DE are synthetic, written for these tests."""

    def test_rss_announcement_feed(self):
        items = news.parse_feed(raw("feed-EQNR.OL-0.xml"), "EQNR.OL", "Equinor IR")
        self.assertEqual(len(items), 2)
        trade, impairment = items
        self.assertEqual(trade["kind"], "regulatory")
        self.assertEqual(trade["source"], "Equinor IR")
        self.assertFalse(trade["important"])
        self.assertEqual(trade["summary"], "A primary insider bought 2,000 shares.")
        self.assertEqual(trade["time"].isoformat(), "2026-10-13T12:30:00+00:00")
        self.assertTrue(impairment["important"])  # Innsideinformasjon = inside information
        news.tag(items, {"EQNR.OL": "Equinor"})
        self.assertIn("insider", trade["tags"])
        self.assertEqual(impairment["tags"][:2], ["inside info", "risk"])  # nedskrivning

    def test_json_feed(self):
        items = news.parse_feed(raw("feed-SAP.DE-0.json"), "SAP.DE", "SAP IR")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["important"])  # ad hoc
        self.assertEqual(items[0]["link"], "https://example.org/sap/adhoc/77")
        self.assertEqual(items[0]["time"].hour, 14)

    def test_atom_feed(self):
        data = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x1</id>'
                b'<title>Volvo: Interim report Q3</title><link href="https://example.org/v/1"/>'
                b'<updated>2026-10-13T06:00:00Z</updated><category term="Regulatory"/></entry></feed>')
        items = news.tag(news.parse_feed(data, "VOLV-B.ST", "Volvo"), {"VOLV-B.ST": "Volvo"})
        self.assertEqual(items[0]["link"], "https://example.org/v/1")
        self.assertEqual(items[0]["categories"], ["Regulatory"])
        self.assertIn("earnings", items[0]["tags"])

    def test_bad_feed_is_an_error(self):
        with self.assertRaises(news.NewsError):
            news.parse_feed(b"<html><body>not a feed", "X.OL", "X")

    def test_nordic_keywords(self):
        items = [{"id": "a", "kind": "headline", "ticker": "X.ST", "title": t, "summary": "",
                  "link": "", "time": None} for t in
                 ["Bolaget offentliggör delårsrapport", "Styrelsen föreslår utdelning",
                  "Konsernsjef fratrer", "Flaggemelding"]]
        tags = [i["tags"] for i in news.tag(items, {})]
        self.assertEqual(tags, [["earnings"], ["capital"], ["people"], ["holdings"]])

    def test_venue_prefixed_symbol_is_a_mention(self):
        it = {"id": "a", "kind": "headline", "ticker": "AAPL", "title": "Deal news (OSE: EQNR)",
              "summary": "", "link": "", "time": None}
        news.tag([it], {"EQNR.OL": None, "AAPL": None})
        self.assertEqual(it["touches"], ["AAPL", "EQNR.OL"])

    def test_sources_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w") as fh:
                json.dump({"_readme": ["x"], "eqnr.ol": {"name": "Equinor", "sec": "eqnr",
                           "feeds": ["https://a.example/rss", {"url": "https://b.example/x", "label": "B"}]}}, fh)
            src = news.load_sources(path)
        self.assertEqual(list(src), ["EQNR.OL"])
        self.assertEqual(src["EQNR.OL"]["sec"], "EQNR")
        self.assertEqual([f["label"] for f in src["EQNR.OL"]["feeds"]], ["a.example", "B"])

    def test_foreign_filer_gets_6k(self):
        state = {}
        f = news.Fetcher(offline=DIR)
        now = news.parse_time(NOW)
        self.assertEqual(news.filer_kind(f, state, "0001140625", now), "foreign")
        self.assertEqual(news.filer_kind(f, state, "0000320193", now), "domestic")
        self.assertIn("0001140625", state["filers"])


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("yahoo-KO.xml"), "KO")
        watch = {"KO": "Coca-Cola", "CVX": "Chevron", "AAPL": "Apple"}
        news.tag(items, watch)
        by = {i["title"][:20]: i for i in items}
        chevron = by["Coca-Cola or Chevron"]
        self.assertEqual(chevron["touches"], ["CVX", "KO"])
        self.assertIn("capital", chevron["tags"])  # dividend
        self.assertEqual(by["Is Coca-Cola (KO) St"]["touches"], ["KO"])

    def test_earnings_filings_are_tagged(self):
        items = news.tag(news.parse_sec_atom(raw("sec-8-K-0000021344.xml"), "KO"), {"KO": None})
        self.assertEqual(items[0]["tags"][0], "earnings")
        self.assertFalse(items[1]["important"])  # 8.01 other events

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        b = news.parse_rss(raw("yahoo-AAPL.xml"), "GOOGL")
        items = news.dedupe(news.tag(a + b, {"AAPL": None, "GOOGL": None}))
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["touches"], ["AAPL", "GOOGL"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--targets", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "AAPL", "KO", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 10)  # 6 Apple + 4 Coca-Cola headlines, filings are older
        code, out, _ = run(["--tickers", "AAPL", "KO", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_filings_show_in_a_wider_window(self):
        code, out, err = run(["--tickers", "AAPL", "--hours", "2400"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-07-30 20:30 UTC  AAPL", out)
        self.assertIn("earnings results", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_tickers_from_targets(self):
        targets = os.path.join(self.tmp.name, "targets.json")
        with open(targets, "w") as fh:
            json.dump({"weights": [{"ticker": "KO", "name": "Coca-Cola", "weight": 100}]}, fh)
        args = list(self.base)
        args[args.index("--targets") + 1] = targets
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"KO"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_sec_needs_a_user_agent(self):
        old = os.environ.pop("SEC_USER_AGENT", None)
        try:
            self.assertIsNone(news.sec_user_agent(None, "/nonexistent.json"))
            self.assertIsNone(news.sec_user_agent("Your Name you@example.com"))
            self.assertEqual(news.sec_user_agent("Ada ada@example.org"), "Ada ada@example.org")
            with self.assertRaises(news.NewsError):
                news.Fetcher(None).get("https://www.sec.gov/x", "x", sec=True)
        finally:
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old

    def _sources(self, data):
        path = os.path.join(self.tmp.name, "sources.json")
        with open(path, "w") as fh:
            json.dump(data, fh)
        return ["--sources", path]

    def test_european_listing_with_feed_and_adr(self):
        src = self._sources({"EQNR.OL": {"name": "Equinor", "sec": "EQNR",
                                         "feeds": [{"label": "Equinor IR", "url": "https://x.example/rss"}]}})
        code, out, err = run(["--tickers", "EQNR.OL", "--hours", "48", "--json"] + src + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        kinds = sorted(i["kind"] for i in data["items"])
        self.assertEqual(kinds, ["filing", "headline", "regulatory", "regulatory"])
        filing = [i for i in data["items"] if i["kind"] == "filing"][0]
        self.assertEqual(filing["form"], "6-K")  # auto: Equinor files 20-F, so 6-K not 8-K
        self.assertEqual(data["coverage"]["EQNR.OL"]["filings"], "6-K (EQNR)")
        self.assertEqual(data["coverage"]["EQNR.OL"]["regulatory"], ["Equinor IR"])
        self.assertEqual(data["notes"], [])
        self.assertTrue(all(i["touches"] == ["EQNR.OL"] for i in data["items"]))

    def test_european_listing_without_feed_is_named(self):
        src = self._sources({})
        code, out, err = run(["--tickers", "VOLV-B.ST", "AAPL", "--hours", "24"] + src + self.base)
        self.assertIn("VOLV-B.ST (Nasdaq Stockholm): no exchange-announcement feed", out)
        self.assertIn("does not mean nothing was announced", out)
        self.assertNotIn("no SEC CIK for VOLV", out)
        self.assertNotIn("AAPL (US)", out)

    def test_placeholder_feed_is_not_fetched(self):
        src = self._sources({"VOLV-B.ST": {"feeds": ["https://REPLACE-WITH-VOLVO-RSS"]}})
        code, out, err = run(["--tickers", "VOLV-B.ST", "--hours", "24"] + src + self.base)
        self.assertIn("still a placeholder", out)
        self.assertNotIn("offline: no file", out)
        self.assertIn("no exchange-announcement feed", out)

    def test_explicit_forms_override_auto(self):
        src = self._sources({"EQNR.OL": {"sec": "EQNR"}})
        code, out, err = run(["--tickers", "EQNR.OL", "--forms", "8-K", "--hours", "2400",
                              "--json"] + src + self.base)
        notes = json.loads(out)["notes"]
        self.assertTrue(any("SEC 8-K for EQNR.OL" in n for n in notes), notes)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
