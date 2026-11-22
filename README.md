Canada Economic Monitor
A Streamlit dashboard that answers one question: how is Canada's economy
performing right now, and how does that compare with recent history? It pulls
data directly from Statistics Canada and the Bank of Canada, updates itself
on a schedule, and checks itself against the publishers' own release pages.
```
app.py            dashboard + data pipeline + scheduler + CLI (one file)
requirements.txt  dependencies (Python 3.12)
README.md         this file
data/             created on first run: observations.csv, refresh_meta.json, refresh.log
```
---
1. Quick start
```bash
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python app.py --refresh              # first pull + verification log (~10 s)
streamlit run app.py                 # open http://localhost:8501
```
No API keys are needed. Both APIs are free and public.
Deploying to Streamlit Community Cloud: put `app.py` and `requirements.txt`
side by side at the repo root, choose Python 3.12, and set the main file to
`app.py`. Charts use Altair, which installs with Streamlit, so the app runs
even if the host skips `requirements.txt`.
---
2. Indicators
Six indicators, shown in five charts. Use Indicators to display in the sidebar
to pick which ones appear (4–8 is the intended range; all are shown by default).
#	Indicator	Series (code)	Units	Freq.	Adj.	Source
1	Real GDP growth	GDP at basic prices, all industries (v65201210)	Millions of chained 2017 $, annualized → plotted as % change	Monthly	SAAR	StatCan Table 36-10-0434-01
2	Headline CPI inflation	CPI all-items (v41690973)	Index 2002=100 → plotted as 12-month % change	Monthly	NSA	StatCan Table 18-10-0004-01
3	Core inflation	CPI-trim (v108785715), CPI-median (v108785714)	12-month % change	Monthly	SA	StatCan Table 18-10-0256-01 (Bank of Canada definitions)
4	Unemployment rate	15+ years, both sexes (v2062815)	% of labour force	Monthly	SA	StatCan Labour Force Survey, Table 14-10-0287-01
5	Policy rate & bond yields	Target overnight rate (V39079); GoC 2-yr and 10-yr benchmark yields (BD.CDN.2YR.DQ.YLD, BD.CDN.10YR.DQ.YLD)	% per year	Daily (business days)	–	Bank of Canada Valet API
6	Exchange rate	USD/CAD daily average (FXUSDCAD)	CAD per 1 USD	Daily (business days)	–	Bank of Canada Valet API
Why these: together they cover the Bank of Canada's whole picture: output
(GDP), its mandate (inflation, with core measures to strip out noise), slack
(unemployment), the policy response and how markets price it (policy rate, the
2-yr yield's expected path, the 10-yr yield's longer-term view), and the currency,
which matters a great deal in an open, commodity-exporting economy.
Chart details shown on the dashboard
Under every chart the dashboard lists the units, frequency, seasonal adjustment,
source (with a link to the original table), latest observation date and any
transformation. The latest observation date is read from the data on each
refresh, so it is always current.
Transformations
Chart	Transformation
GDP	Month-over-month % = 100 × (GDPₜ / GDPₜ₋₁ − 1), as bars (not annualized). Year-over-year % = 100 × (GDPₜ / GDPₜ₋₁₂ − 1), as a line. When the window includes 2020–21, the y-axis is clipped to −4…+6% so recent moves stay readable; hovering still shows the true values.
Headline CPI	Computed here from the NSA index: 100 × (CPIₜ / CPIₜ₋₁₂ − 1), rounded to one decimal place the same way StatCan rounds. Year-over-year changes remove seasonality, which is why the NSA index is the standard source. Months are matched by calendar date, not by row position.
CPI-trim / median	None. StatCan already publishes them as 12-month % changes. The Bank's 1–3% control range is shaded, with the 2% target dotted.
Unemployment	None.
Rates	None. The policy rate is drawn as a step line. The 10y − 2y spread (in basis points) is shown as a headline number.
USD/CAD	None by default. There's an optional sidebar toggle to invert it to CAD/USD = 1 ÷ USD/CAD.
Exchange-rate quotation convention
The Bank of Canada quotes one unit of foreign currency in Canadian dollars.
FXUSDCAD = 1.4325 therefore means C$1.4325 buys US$1. On the default
chart, a rising line means the Canadian dollar is weakening. The inverted view
(USD per CAD) reverses that. Rates are the Bank's daily averages of indicative
quotes, published by 16:30 ET each business day.
Sample period
The app downloads data from January 2010 onward (the extra history is needed
for 12-month changes). Charts start in 2015 by default, and the sidebar slider
moves that anywhere from 2011 on. Starting in 2015 covers the 2015 oil-price shock,
the 2020 pandemic, the 2022–23 tightening cycle, the 2024–25 easing and tariff
shock, and the October 2026 rate hike. That gives enough context to judge whether
current readings are unusual.
---
3. Automatic refresh
How it works
Sources: one request to StatCan WDS
(`getDataFromVectorByReferencePeriodRange`, all five vectors) and one to the
BoC Valet API (`/valet/observations/V39079,BD.CDN.2YR.DQ.YLD,…/json`). Each
request is retried up to three times, with exponential back-off.
Schedule: while `streamlit run app.py` is running, a background thread checks
every 5 minutes and refreshes once the last success is more than
6 hours old (`CEM_REFRESH_HOURS`). After a failure it retries after
30 minutes (`CEM_RETRY_MINUTES`). Open browser tabs re-read the cache every
5 minutes, so charts update without a reload. A stale cache also triggers a
refresh as soon as the page opens, which covers hosts that put idle apps to sleep.
Failure handling: if a source is down, its series keep their last good
values, a warning names them, and the Last successful refresh time does
not move forward. Files are written atomically, and a lock file prevents
two refreshes (for example the app and cron) from running at once.
Two timestamps, kept separate: the header shows Last successful refresh
(when the app last pulled data, in Eastern and UTC time). Each chart shows its
latest observation (the most recent period the publisher has released). The
two differ by design: today's refresh can return the October CPI because November
hasn't been published yet.
Refresh without the dashboard open (recommended for always-on)
```bash
# cron (Linux/macOS): every 6 h, plus 08:45 ET on weekdays to catch 08:30 StatCan releases
0 */6 * * *   cd /path/to/app && .venv/bin/python app.py --refresh >> data/cron.out 2>&1
45 8 * * 1-5  cd /path/to/app && .venv/bin/python app.py --refresh >> data/cron.out 2>&1
```
Windows Task Scheduler: run `.venv\Scripts\python.exe app.py --refresh`
every 6 hours.
Long-running server: `python app.py --loop` runs the scheduler without Streamlit.
`python app.py --refresh` exits with code 0 on success and 1 on failure, so
monitoring tools can alert on it.
---
4. Verification against original sources
Observations checked by hand (22 Nov 2026)
At least one observation from every series was compared with the publisher's own
release page. These values are stored in `REFERENCE_CHECKS` in `app.py` and
re-checked automatically on every refresh. The results appear in the log and in
the dashboard's Data, sources & verification tab.
Series	Period	Published value	Where it was checked
Real GDP, all industries (v65201210)	Aug 2026	2,371,635 ($ millions, chained 2017, SAAR)	StatCan Table 36-10-0434-01; The Daily, 30 Oct 2026 (“essentially unchanged in August”)
CPI all-items (v41690973)	Oct 2026	170.4 (2002=100)	Bank of Canada CPI page, which reproduces StatCan v41690973
CPI 12-month change (our transformation)	Oct 2026	+3.1%	StatCan CPI release, 16 Nov 2026 (3.1% y/y, unchanged from September)
CPI-trim (v108785715)	Oct 2026	1.8%	Bank of Canada Daily Digest / CPI page
CPI-median (v108785714)	Oct 2026	2.0%	Bank of Canada Daily Digest / CPI page
Unemployment rate (v2062815)	Oct 2026	6.7%	StatCan The Daily, Labour Force Survey, 6 Nov 2026
Target for the overnight rate (V39079)	19 Nov 2026	2.50%	Bank of Canada Daily Digest (rate raised to 2.5% on 28 Oct 2026)
GoC 2-yr benchmark yield	19 Nov 2026	3.30%	Bank of Canada, Selected bond yields page
GoC 10-yr benchmark yield	19 Nov 2026	3.81%	Bank of Canada, Selected bond yields page
USD/CAD (FXUSDCAD)	20 Nov 2026	1.4325	Bank of Canada, Daily exchange rates page (US dollar row)
Each check returns MATCH, NOT FOUND, MISMATCH, or
DIFFERS (likely revised). The last applies only to series the publisher
routinely revises (GDP, core CPI, LFS). When you see it, confirm the new figure
on the source page and update `REFERENCE_CHECKS`.
---
5. Refresh demonstration
Test of the full pipeline (no internet)
The build environment can't reach `bankofcanada.ca` or `statcan.gc.ca`, so the
log below comes from a test that replaces only the HTTP call. It uses responses
shaped like the real WDS and Valet JSON: synthetic history, with the latest points
set to the published values above. Parsing, merging, transformation, verification,
the atomic cache write and the timestamps are the real code. A second run shows a
successful refresh that finds no new releases.
```
=== Refresh started (trigger=cli) ===
  OK   gdp        65201210             rows=200   latest=2026-08-01 value=2371635 | +200 new, 0 revised | initial load
  OK   cpi        41690973             rows=202   latest=2026-10-01 value=170.4 | +202 new, 0 revised | initial load
  ...
  OK   usdcad     FXUSDCAD             rows=4405  latest=2026-11-20 value=1.4325 | +4405 new, 0 revised | initial load
  CHECK cpi        2026-10-01 yoy   ref=3.1        got=3.1        MATCH
  ...
=== Refresh SUCCESS in 1s: 9/9 series OK, 10/10 checks MATCH ===
--- second run ---
  OK   cpi        41690973             rows=202   latest=2026-10-01 value=170.4 | +0 new, 0 revised | unchanged (no new release since last refresh)
=== Refresh SUCCESS in 0s: 9/9 series OK, 10/10 checks MATCH ===
--- simulated outage ---
  FAIL gdp        65201210             StatCan WDS error: simulated outage
=== Refresh FAILED: no series retrieved; cache untouched ===   (last successful refresh unchanged)
```
Produce the live log and screenshot yourself
```bash
python app.py --refresh          # prints the log above with live data; also saved to data/refresh.log
streamlit run app.py             # screenshot the header (Last successful refresh) and a chart caption
```
Run `--refresh` twice to show the unchanged result when nothing new has been
released. The Data, sources & verification tab shows the last 40 log lines and
the full verification table, so one screenshot of that tab covers both
requirements.
---
6. Configuration
Env variable	Default	Meaning
`CEM_REFRESH_HOURS`	`6`	Hours between scheduled refreshes
`CEM_RETRY_MINUTES`	`30`	Wait before retrying after a failed refresh
`CEM_DATA_DIR`	`./data`	Cache and log location
Adding a series: add a `Series(...)` entry (a StatCan vector number or Valet
code), plot it in an existing or new `chart_*` function, and add it to
`INDICATORS`. If you can, add a `REFERENCE_CHECKS` entry for it too.
Data notes: monthly GDP by industry (basic prices) differs slightly from the
quarterly expenditure-based GDP headline. LFS and core-CPI history is revised
periodically. Benchmark bond yields move when the Bank rolls to a new benchmark
issue.
