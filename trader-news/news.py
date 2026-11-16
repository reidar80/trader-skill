#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines, SEC filings and exchange announcements for the stocks you
hold (or plan to copy) and prints only what is new since the last check,
newest first, with a flag on anything that touches your positions.

    python3 news.py                                  # tickers from trader/portfolio.json or targets.json
    python3 news.py --tickers AAPL AXP GOOGL KO BAC
    python3 news.py --tickers AAPL --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-sec                         # headlines only
    python3 news.py --tickers EQNR.OL NOVO-B.CO SAP.DE  # European listings, Yahoo suffixes

Sources, one request each per ticker:
  - Yahoo Finance headline RSS, with the region taken from the exchange suffix
    (EQNR.OL -> region=NO, SAP.DE -> region=DE, plain AAPL -> region=US):
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER>&region=<CC>&lang=en-US
  - SEC EDGAR filings Atom feed for the company. Form "auto" (the default)
    reads 8-K for US filers and 6-K for foreign private issuers (companies
    that file 20-F or 40-F annual reports, such as European ADRs), decided
    once per company from data.sec.gov/submissions and cached:
    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<CIK>&type=8-K&count=10&output=atom
    SEC needs a User-Agent with your name and email (sec_user_agent in
    trader/config.json or SEC_USER_AGENT). Without one the SEC part is skipped
    and the tool says so; headlines still run.
  - Exchange announcements (regulatory news under the EU Market Abuse
    Regulation) from feeds you list per ticker in trader/news_sources.json:
    any RSS 2.0, Atom or JSON Feed URL, e.g. the issuer's GlobeNewswire,
    Cision or MFN feed or its investor-relations RSS. See
    news_sources.example.json. A listing outside the US with no feed is named
    in the notes on every run, because quiet is not the same as nothing
    announced.

State: trader/state/news.json remembers what you have seen (30 days), so an
item shows once. Offline: --offline DIR reads yahoo-<TICKER>.xml,
sec-<FORM>-<CIK>.xml, submissions-CIK<CIK>.json, feed-<TICKER>-<N>.xml|json
and company_tickers.json from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=%s&lang=en-US"
SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=%s"
            "&dateb=&owner=include&count=%d&output=atom")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%s.json"
FOREIGN_FORMS = {"20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A"}
DOMESTIC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"}
FILER_TTL_DAYS = 30

# Yahoo exchange suffix -> (country code for Yahoo's region, venue). A
# suffix of two or more letters is always read as an exchange; a single
# letter (BRK.B, BF.B) is a US share class, except L (London) and F
# (Frankfurt).
EXCHANGES = {
    "OL": ("NO", "Oslo Bors"), "ST": ("SE", "Nasdaq Stockholm"),
    "CO": ("DK", "Nasdaq Copenhagen"), "HE": ("FI", "Nasdaq Helsinki"),
    "IC": ("IS", "Nasdaq Iceland"), "DE": ("DE", "Xetra"), "F": ("DE", "Frankfurt"),
    "BE": ("DE", "Berlin"), "DU": ("DE", "Dusseldorf"), "HM": ("DE", "Hamburg"),
    "HA": ("DE", "Hanover"), "MU": ("DE", "Munich"), "SG": ("DE", "Stuttgart"),
    "PA": ("FR", "Euronext Paris"), "AS": ("NL", "Euronext Amsterdam"),
    "BR": ("BE", "Euronext Brussels"), "LS": ("PT", "Euronext Lisbon"),
    "IR": ("IE", "Euronext Dublin"), "MI": ("IT", "Euronext Milan"),
    "MC": ("ES", "Bolsa de Madrid"), "L": ("GB", "London Stock Exchange"),
    "IL": ("GB", "London Stock Exchange (international)"), "SW": ("CH", "SIX Swiss Exchange"),
    "VI": ("AT", "Wiener Borse"), "WA": ("PL", "Warsaw"), "AT": ("GR", "Athens"),
    "PR": ("CZ", "Prague"), "BD": ("HU", "Budapest"),
}
# Exchange prefixes press releases put before a symbol ("OSE: EQNR").
VENUE_PREFIXES = r"(?:OSE|OB|OSL|OSLO|STO|CPH|HEL|ICE|ETR|XETRA|FRA|EPA|AMS|EBR|ELI|ISE|BIT|MIL|BME|LON|LSE|SIX|SWX|VIE|WSE)"
PLACEHOLDER = "you@example.com"
BROWSER_UA = "Mozilla/5.0 (compatible; trader-skill/1.0; +https://github.com/Jakeschincariol/trader-skill)"
KEEP_DAYS = 30

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = {"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"}
# English first, then the Nordic and German terms European issuers use in
# their own-language announcements (Norwegian, Swedish, Danish, Finnish,
# German).
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"
                 r"|\binterim report\b|\bhalf-year report\b|\bannual report\b|\btrading update\b"
                 r"|kvartalsrapport|delårsrapport|halvårsrapport|årsrapport|bokslutskommuniké"
                 r"|\bresultat\b|osavuosikatsaus|puolivuosikatsaus|tilinpäätös"
                 r"|Quartalsmitteilung|Halbjahresfinanzbericht|Geschäftsbericht|Jahresergebnis"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"
                r"|kursmål|Kursziel"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b|\bjudge\b|\bcourt\b"
              r"|søksmål|rettssak|stämning|tingsrätt|retssag|oikeudenkäynti|Gericht|Klage\b|Ermittlung"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b|\btender offer\b"
             r"|oppkjøp|\bfusjon|förvärv|uppköpserbjudande|\bfusion|opkøb|købstilbud|yritysosto"
             r"|Übernahme|Übernahmeangebot|\bFusion"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\boffering\b"
                r"|\bshare issue\b|\bprivate placement\b|\brights issue\b"
                r"|utbytte|utdelning|udbytte|osinko|Dividende|tilbakekjøp|återköp|tilbagekøb|Aktienrückkauf"
                r"|emisjon|nyemission|Kapitalerhöhung"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"
               r"|konsernsjef|administrerende direktør|verkställande direktör|\bVD\b|finansdirektør"
               r"|toimitusjohtaja|Vorstand|fratrer|avgår|utnevn|udnævn"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b|\bprofit warning\b"
             r"|\bimpairment\b|konkurs|resultatvarsel|vinstvarning|nedskrivning|Insolvenz|Gewinnwarnung"
             r"|cyberangrep|dataangrep|it-angrep"),
    ("insider", r"\bmandatory notification of trade\b|\bmanagers?'? transactions?\b|\bPDMR\b"
                r"|\bdirectors'? dealings?\b|meldepliktig handel|insynshandel|ledende medarbejderes transaktioner"
                r"|johtohenkilöiden liiketoimet|Eigengeschäfte"),
    ("holdings", r"\bmajor shareholding|\bdisclosure of large shareholding|\bflagging\b|flaggemelding"
                 r"|större innehav|storaktionærmeddelelse|liputusilmoitus|Stimmrechtsmitteilung"),
]
# Inside information under Art. 17 MAR (and German ad hoc notices): the
# European counterpart of the 8-K items flagged with "!".
INSIDE_INFO = re.compile(r"inside information|innsideinformasjon|insiderinformation|intern viden"
                         r"|sisäpiiritieto|Insiderinformation|\bad[ -]hoc\b", re.I)


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", targets="trader/targets.json"):
    """{ticker: name or None} from your holdings, else from your targets."""
    out = {}
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        if p.get("ticker"):
            out[p["ticker"].upper()] = p.get("name")
    if not out:
        tg = load_json(targets) or {}
        for w in tg.get("weights", []):
            if w.get("ticker"):
                out[w["ticker"].upper()] = w.get("name")
    return out


def sec_user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        cfg = load_json(config) or {}
        ua = str(cfg.get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, sec_ua=None, offline=None, pause=0.15):
        self.sec_ua = sec_ua
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, sec=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        if sec and not self.sec_ua:
            raise NewsError("no SEC User-Agent set")
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": self.sec_ua if sec else BROWSER_UA, "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def split_ticker(ticker):
    """("BASE", "SUFFIX") for an exchange listing, ("BASE", None) for US."""
    t = ticker.strip().upper().replace(" ", "-")
    if "." in t:
        base, suf = t.rsplit(".", 1)
        if base and (suf in EXCHANGES or (len(suf) >= 2 and suf.isalpha())):
            return base, suf
    return t, None


def is_foreign_listing(ticker):
    return split_ticker(ticker)[1] is not None


def venue(ticker):
    suf = split_ticker(ticker)[1]
    return EXCHANGES.get(suf, (None, "." + suf))[1] if suf else "US"


def yahoo_symbol(ticker):
    """Yahoo's symbol: US share classes take a dash (BRK.B -> BRK-B), an
    exchange suffix keeps its dot (EQNR.OL, BT-A.L)."""
    base, suf = split_ticker(ticker)
    base = base.replace(".", "-")
    return "%s.%s" % (base, suf) if suf else base


def yahoo_url(ticker):
    suf = split_ticker(ticker)[1]
    region = EXCHANGES.get(suf, ("US",))[0] if suf else "US"
    return YAHOO % (urllib.parse.quote(yahoo_symbol(ticker), safe=".-^"), region)


def cik_map(fetcher, state):
    """ticker -> 10-digit CIK, from SEC's company_tickers.json (cached in state)."""
    cached = state.setdefault("ciks", {})
    if cached:
        return cached
    data = json.loads(fetcher.get(TICKERS_URL, "company_tickers.json", sec=True).decode("utf-8"))
    for v in data.values():
        cached.setdefault(v["ticker"].upper().replace("-", "."), str(v["cik_str"]).zfill(10))
    return cached


def filer_kind(fetcher, state, cik, now):
    """"foreign" (files 20-F/40-F/6-K) or "domestic", from SEC submissions,
    cached in state for 30 days. Unknown counts as domestic."""
    cache = state.setdefault("filers", {})
    hit = cache.get(cik)
    if hit and hit.get("checked", "") >= (now - _dt.timedelta(days=FILER_TTL_DAYS)).isoformat():
        return hit["kind"]
    data = json.loads(fetcher.get(SUBMISSIONS_URL % cik, "submissions-CIK%s.json" % cik,
                                  sec=True).decode("utf-8"))
    forms = set(((data.get("filings") or {}).get("recent") or {}).get("form") or [])
    kind = "foreign" if (forms & FOREIGN_FORMS) and not (forms & DOMESTIC_FORMS) else "domestic"
    cache[cik] = {"kind": kind, "checked": now.isoformat()}
    return kind


def load_sources(path):
    """trader/news_sources.json -> {TICKER: {"name", "sec", "feeds": [{url, label}]}}."""
    raw = load_json(path) or {}
    out = {}
    for t, v in raw.items():
        if t.startswith("_") or not isinstance(v, dict):
            continue
        feeds = []
        for f in v.get("feeds") or []:
            if isinstance(f, str):
                f = {"url": f}
            if isinstance(f, dict) and f.get("url"):
                feeds.append({"url": f["url"], "label": f.get("label") or _host(f["url"])})
        out[t.upper()] = {"name": v.get("name"), "sec": (v.get("sec") or "").upper() or None,
                          "feeds": feeds}
    return out


def _host(url):
    return urllib.parse.urlsplit(url).netloc or "feed"


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def _texts(el, name):
    return [(c.text or "").strip() for c in el.iter() if _local(c.tag) == name and (c.text or "").strip()]


def _strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _regulatory(ticker, label, ident, title, summary, link, when, categories):
    hay = " ".join([title, summary] + categories)
    return {"id": "feed:" + (ident or link or title), "source": label, "kind": "regulatory",
            "ticker": ticker, "title": title, "summary": summary[:500], "link": link,
            "categories": categories, "important": bool(INSIDE_INFO.search(hay)),
            "time": parse_time(when)}


def parse_feed(data, ticker, label):
    """An exchange-announcement feed (RSS 2.0, Atom or JSON Feed) -> items."""
    head = data.lstrip()[:1]
    if head in (b"{", b"["):
        try:
            doc = json.loads(data.decode("utf-8"))
        except ValueError as exc:
            raise NewsError("%s feed for %s is not valid JSON: %s" % (label, ticker, exc))
        entries = doc.get("items") if isinstance(doc, dict) else doc
        out = []
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            c = e.get("content") if isinstance(e.get("content"), dict) else {}
            title = e.get("title") or c.get("title") or ""
            summary = _strip_html(e.get("summary") or e.get("content_text") or c.get("preamble") or "")
            link = e.get("url") or e.get("link") or e.get("external_url") or ""
            when = (e.get("date_published") or e.get("publish_date") or e.get("published")
                    or e.get("date") or "")
            cats = [str(x) for x in (e.get("tags") or []) if isinstance(x, (str, int))]
            ident = str(e.get("id") or e.get("news_id") or "")
            out.append(_regulatory(ticker, label, ident, title, summary, link, when, cats))
        return out
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("%s feed for %s is not valid XML: %s" % (label, ticker, exc))
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "item":
            link = _text(el, "link")
            out.append(_regulatory(ticker, label, _text(el, "guid") or link, _text(el, "title"),
                                   _strip_html(_text(el, "description")), link,
                                   _text(el, "pubDate") or _text(el, "date"), _texts(el, "category")))
        elif name == "entry":
            link = ""
            for c in el.iter():
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
            cats = [c.get("term") or c.get("label") or "" for c in el.iter() if _local(c.tag) == "category"]
            out.append(_regulatory(ticker, label, _text(el, "id") or link, _text(el, "title"),
                                   _strip_html(_text(el, "summary") or _text(el, "content")), link,
                                   _text(el, "published") or _text(el, "updated"), [c for c in cats if c]))
    return out


def parse_sec_atom(data, ticker):
    """EDGAR company filings Atom -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("SEC feed for %s is not valid XML: %s" % (ticker, exc))
    company = ""
    for el in root.iter():
        if _local(el.tag) == "conformed-name":
            company = (el.text or "").strip()
            break
    out = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        form = _text(entry, "filing-type")
        acc = _text(entry, "accession-number")
        items = re.findall(r"\d+\.\d+", _text(entry, "items-desc"))
        labels = [ITEMS_8K.get(i, "item " + i) for i in items if i != "9.01"]
        link = ""
        for c in entry.iter():
            if _local(c.tag) == "link":
                link = c.get("href", "")
        title = "%s filed a %s" % (company or ticker, form)
        if labels:
            title += ": " + ", ".join(labels)
        out.append({"id": "sec:" + acc, "source": "SEC EDGAR", "kind": "filing", "form": form,
                    "ticker": ticker, "title": title, "summary": _text(entry, "form-name"),
                    "items": items, "important": bool(set(items) & IMPORTANT_ITEMS),
                    "link": link, "time": parse_time(_text(entry, "updated"))})
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\$%s\b" % re.escape(t),
                r"\b(?:NYSE|NASDAQ|Nasdaq)\s*:\s*%s\b" % re.escape(t)]
        base, suf = split_ticker(t)
        if suf:
            alts.append(r"\b%s\s*:\s*%s\b" % (VENUE_PREFIXES, re.escape(base)))
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = " ".join([it["title"], it.get("summary") or ""] + it.get("categories", []))
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "regulatory" and it.get("important") and "inside info" not in it["tags"]:
            it["tags"].insert(0, "inside info")
        if it["kind"] == "filing" and "2.02" in it.get("items", []) and "earnings" not in it["tags"]:
            it["tags"].insert(0, "earnings")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, sec=True, forms=None, count=10, errors=None,
            sources=None, now=None, coverage=None):
    """Every item from every source for the watch list.

    forms None means auto: 8-K for US filers, 6-K for foreign private
    issuers. coverage (a dict) is filled with what each ticker was checked
    against, so a silent source is never mistaken for a quiet company."""
    errors = [] if errors is None else errors
    sources = sources or {}
    now = now or _dt.datetime.now(_dt.timezone.utc)
    coverage = {} if coverage is None else coverage
    items = []
    for t in sorted(watch):
        cov = coverage.setdefault(t, {"venue": venue(t), "headlines": False,
                                      "filings": None, "regulatory": []})
        try:
            items += parse_rss(fetcher.get(yahoo_url(t), "yahoo-%s.xml" % t), t)
            cov["headlines"] = True
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
        for n, feed in enumerate(sources.get(t, {}).get("feeds", [])):
            if "REPLACE" in feed["url"].upper():
                errors.append("feed for %s is still a placeholder (%s): put the real URL in"
                              % (t, feed["url"]))
                continue
            ext = "json" if feed["url"].lower().split("?")[0].endswith(".json") else "xml"
            try:
                data = fetcher.get(feed["url"], "feed-%s-%d.%s" % (t, n, ext))
                items += parse_feed(data, t, feed["label"])
                cov["regulatory"].append(feed["label"])
            except NewsError as exc:
                errors.append("%s feed for %s: %s" % (feed["label"], t, exc))
    if sec:
        try:
            ciks = cik_map(fetcher, state)
        except NewsError as exc:
            errors.append("SEC ticker list: %s" % exc)
            ciks = {}
        for t in sorted(watch):
            sec_t = sources.get(t, {}).get("sec") or (None if is_foreign_listing(t) else t)
            if not sec_t:
                continue  # a European listing with no US ticker mapped: covered by the notes
            cik = ciks.get(sec_t)
            if not cik:
                if ciks:
                    errors.append("no SEC CIK for %s" % sec_t)
                continue
            want = forms
            if not want:
                try:
                    want = ["6-K"] if filer_kind(fetcher, state, cik, now) == "foreign" else ["8-K"]
                except (NewsError, ValueError) as exc:
                    errors.append("SEC filer type for %s (reading 8-K): %s" % (sec_t, exc))
                    want = ["8-K"]
            got = []
            for form in want:
                try:
                    data = fetcher.get(SEC_FEED % (cik, form, count),
                                       "sec-%s-%s.xml" % (form, cik), sec=True)
                    items += parse_sec_atom(data, t)
                    got.append(form)
                except NewsError as exc:
                    errors.append("SEC %s for %s: %s" % (form, t, exc))
            if got:
                coverage[t]["filings"] = "%s (%s)" % (",".join(got), sec_t)
    return items


def coverage_notes(coverage, sources_path):
    """One line per non-US listing that has no exchange-announcement feed."""
    notes = []
    for t in sorted(coverage):
        c = coverage[t]
        if c["venue"] == "US" or c["regulatory"]:
            continue
        what = "Yahoo headlines" if c["headlines"] else "nothing"
        if c["filings"]:
            what += " and SEC %s" % c["filings"]
        notes.append("%s (%s): no exchange-announcement feed, so its regulatory news (inside "
                     "information, reports, insider trades) was NOT checked; only %s ran. Quiet "
                     "here does not mean nothing was announced. Add a feed in %s."
                     % (t, c["venue"], what, sources_path))
    return notes


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    out = []
    for it in items:
        if it["time"] is not None and it["time"] < cutoff:
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render(items, errors, now):
    out = []
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = it["time"].strftime("%Y-%m-%d %H:%M UTC") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % now.strftime("%Y-%m-%d %H:%M UTC"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: trader/portfolio.json, else targets.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--targets", default="trader/targets.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--forms", default="auto",
                    help="SEC forms, comma separated; auto = 8-K for US filers, 6-K for foreign")
    ap.add_argument("--no-sec", action="store_true", help="headlines only")
    ap.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--sources", default="trader/news_sources.json",
                    help="exchange-announcement feeds and US tickers per listing")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    sources = load_sources(args.sources)
    if args.tickers:
        watch = {t.upper(): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.targets)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.targets)
    for t in watch:
        if not watch[t] and sources.get(t, {}).get("name"):
            watch[t] = sources[t]["name"]
    if not watch:
        print("news: no tickers. Pass --tickers, or create trader/portfolio.json or "
              "trader/targets.json first (/trader-copycat writes targets).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    ua = sec_user_agent(args.user_agent, args.config)
    sec = not args.no_sec
    if sec and not ua and not args.offline:
        errors.append("SEC filings skipped: set sec_user_agent in trader/config.json or "
                      "SEC_USER_AGENT (\"Your Name you@example.com\") to include them")
        sec = False
    fetcher = Fetcher(ua, offline=args.offline)
    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    if forms == ["AUTO"]:
        forms = None
    coverage = {}
    items = collect(fetcher, watch, state, sec=sec, forms=forms, errors=errors,
                    sources=sources, now=now, coverage=coverage)
    errors += coverage_notes(coverage, args.sources)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    if args.json:
        print(json.dumps({"checked": now.isoformat(), "items": _json_ready(shown),
                          "coverage": coverage, "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
