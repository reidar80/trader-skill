---
name: trader-news
description: >-
  The News Monitor. Watches breaking market news, SEC filings and European
  exchange announcements for the stocks you hold (or are about to copy):
  Yahoo Finance headlines per ticker (US and European listings such as
  EQNR.OL or SAP.DE), each company's 8-K (US) or 6-K (foreign issuer)
  filings on SEC EDGAR, and regulatory announcement feeds you configure per
  listing (inside information, reports, insider trades). Shows only what is
  new since the last check,
  newest first, flags what touches your positions and why it matters, and
  never repeats an item. Use when the user says "any news on my stocks",
  "what's happening with Apple", "monitor the news", "watch my portfolio",
  "did anything break", "check the 8-Ks", or "morning briefing".
argument-hint: "[tickers]"
---

# trader-news

A position you hold can change in a minute: an earnings miss, a CEO leaving,
a lawsuit, a restatement. This skill reads the two feeds that carry those
first for each of your stocks and tells you only what is new.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py" --help
```

## What it reads and writes

- Your tickers: `--tickers`, else `trader/portfolio.json` positions, else
  `trader/targets.json` weights (so it works right after `/trader-copycat`).
- Tickers use Yahoo's symbols. A US share class takes a dot or dash
  (`BRK.B`); a non-US listing keeps its exchange suffix: `EQNR.OL` (Oslo),
  `VOLV-B.ST` (Stockholm), `NOVO-B.CO` (Copenhagen), `NOKIA.HE` (Helsinki),
  `SAP.DE` (Xetra), `MC.PA` (Paris), `ASML.AS` (Amsterdam), `VOD.L` (London).
- Headlines: Yahoo Finance's public headline RSS, one feed per ticker, with
  the region taken from the suffix
  (`https://feeds.finance.yahoo.com/rss/2.0/headline?s=EQNR.OL&region=NO&lang=en-US`).
  Yahoo's coverage of European small and mid caps is thin; that is why the
  announcement feeds below exist.
- Filings: SEC EDGAR's company Atom feed. `--forms auto` (the default) reads
  8-K for US filers and 6-K for foreign private issuers (companies that file
  20-F or 40-F, such as Equinor, Novo Nordisk, ASML, SAP), decided once per
  company from `data.sec.gov/submissions` and cached 30 days. `--forms
  8-K,10-Q` overrides it for every ticker. A European listing reads SEC only
  if you map it to its US ticker (`"sec": "EQNR"` in
  `trader/news_sources.json`). Needs the SEC User-Agent from
  `trader/config.json` (`sec_user_agent`) or `SEC_USER_AGENT`. Without it,
  filings are skipped with a note and headlines still run.
- Exchange announcements: `trader/news_sources.json` (start from
  `news_sources.example.json` in this folder) lists, per ticker, RSS 2.0,
  Atom or JSON Feed URLs that carry the company's regulatory news: its
  investor-relations RSS, its GlobeNewswire, Cision/Mynewsdesk or MFN feed.
  This is the European counterpart of the 8-K: under the EU Market Abuse
  Regulation, inside information has to be published there first. There is
  no free, documented, keyless per-company API for Oslo Bors NewsWeb, Nasdaq
  Nordic, Euronext or EQS, so the tool does not pretend to read them; you
  supply the feed. Help the user find one and check it returns XML or JSON
  before adding it.
- Writes `trader/state/news.json`: what has been shown (kept 30 days), the
  CIK for each ticker and each filer's type, so every item appears once.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say it is
  paywalled.
- **Filings beat headlines.** If an 8-K, 6-K or exchange announcement and a
  headline disagree, the filing is what the company told its regulator.
- **Silence is not coverage.** A non-US listing with no announcement feed is
  named in the notes on every run ("no exchange-announcement feed ... NOT
  checked"). Pass that on to the user; never report such a ticker as quiet.
  `--json` gives a `coverage` block per ticker: venue, headlines, filings,
  feeds.
- **Say what it touches.** Every item lists the tickers it touches (the feed
  it came from plus any of your tickers or company names it mentions) and
  tags (earnings, analyst, legal, deal, capital, people, risk). Filings marked
  `!` carry an important 8-K item: earnings results (2.02), an officer change
  (5.02), an impairment (2.06), a delisting notice (3.01), past financials no
  longer reliable (4.02), bankruptcy (1.03) or a cyber incident (1.05).
  Announcements marked `!` are inside information under the Market Abuse
  Regulation (or a German ad hoc notice); 6-Ks carry no item codes, so read
  them. European tags include `insider` (managers' transactions, "meldepliktig
  handel") and `holdings` (major shareholding / flagging notices), and the
  tags also match Norwegian, Swedish, Danish, Finnish and German wording.
- **No predictions.** Report what was published. Never say what the stock
  will do next.

## Steps

1. Run the check:

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/news.py"
   python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers AAPL AXP GOOGL KO BAC --hours 24
   python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers EQNR.OL NOVO-B.CO SAP.DE
   ```

   Exit code 1 means nothing new. `--all` shows items already seen,
   `--json` gives everything machine-readable, `--dry-run` leaves the state
   alone.
2. Read the notes first: any "no exchange-announcement feed" line means that
   ticker was only half checked. Then read the items that matter: every `!`
   filing or announcement, every item tagged earnings, legal, risk or insider, and anything touching the biggest positions first.
3. Write a short brief: per ticker, what happened, the source and link, and
   one line on why it matters to this portfolio (its weight, from
   `trader/targets.json` or `trader/portfolio.json`).
4. If a story might change the plan (an 8-K 4.02 restatement, a delisting
   notice), say so and suggest `/trader-analyst` for the numbers or
   `/trader-risk` to recheck pending orders. Never act on a headline alone.

To run it on a schedule, use `/loop 30m /trader-news` during market hours or
a scheduled task each morning.

## Example: the reel's portfolio

The user copied Berkshire's top 5 (AAPL, AXP, GOOGL, KO, BAC). On Tuesday
2026-10-13 the check shows Apple headlines first ("Apple Introduces HomeView
Smart Home Hub, Other Gadgets", "Judge orders OpenAI to turn over hiring
records in Apple trade secrets case", tagged legal), Coca-Cola dividend
pieces, and from SEC: Apple's 8-K of 2026-07-30 (item 2.02, earnings results)
and Coca-Cola's of 2026-07-28 (2.02). Brief: "Apple (your biggest position,
30.7%) launched a $299 smart home hub; a court ruling in its trade secrets
case against OpenAI. No new filings from your five companies this week." When
a ticker has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per ticker, a pause between
  SEC calls, and SEC's 10 requests a second limit stays far away.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
