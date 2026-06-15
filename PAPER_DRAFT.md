# Deliberative Multi-Agent LLM Architecture for Autonomous Equity Portfolio Management

**Parth Choksi**  
Independent Researcher  
parthchoksi007@gmail.com

---

## Abstract

We present a fully autonomous equity trading system that executes a seven-agent
deliberative pipeline every market day without routine human intervention
(one documented exception — a mid-execution crash — requires manual
reconciliation; Section 3.6). The system
separates concerns cleanly: deterministic quantitative scoring provides a
data-grounded prior on each candidate security, while a sequence of specialized
large language model (LLM) agents reasons about regime context, investment
theses, adversarial bear cases, and position reviews before a portfolio manager
synthesizes capital allocation decisions. A chief risk officer agent is designed
to exercise independent review over the portfolio manager's output, receiving
only the trade list and current portfolio rather than the full research state.
Deterministic guardrails — sector caps, minimum holding periods, wash-sale
re-entry blocking, and a kill switch triggered by drawdown — execute after all
LLM deliberation, so no model output can directly produce a policy-violating
order. A stamp-first idempotency protocol biases every failure mode toward
missed trades rather than duplicate trades.

**This paper is a system description.** The system is deployed on a live
brokerage account and is accumulating the operational track record required
before any investment performance claims can be made. We present no evidence
of investment edge; the statistical sample is not yet sufficient for such
claims. We describe the architecture, design rationale, operational
infrastructure, and observational system behavior, and we enumerate the
experiments that must be conducted before any architectural choice can be
declared empirically validated.

**Keywords:** multi-agent systems, large language models, algorithmic trading,
autonomous AI, portfolio management, adversarial debate

---

## 1. Introduction

Systematic equity trading has been dominated for decades by statistical and
machine-learning approaches that reduce the problem to a prediction task:
forecast a return signal, size positions proportional to predicted alpha, and
manage risk with volatility targets or portfolio optimization. These approaches
are powerful but brittle at the boundaries of their training distributions, and
they lack the ability to reason about qualitative catalysts, management
narratives, or regime shifts that do not yet appear in return history.

The emergence of large language models capable of multi-step reasoning raises a
different question: can a system that deliberates — debates a thesis,
stress-tests it against a bear case, and subjects the conclusion to independent
risk review — operate autonomously and reliably enough to trade real capital
without human supervision? This is distinct from using an LLM to generate
return forecasts. The question is whether a pipeline of specialized agents,
each contributing a different epistemic role, can produce investment decisions
that are both qualitatively well-reasoned and operationally safe.

We describe such a system, deployed live on a Robinhood brokerage account.
The system makes no claim to have discovered a persistent edge — its live
history is too short for statistical significance, and we have not conducted
the ablation studies required to attribute any future performance to
architectural choices. What this paper offers is a concrete, deployed
instantiation of a deliberative multi-agent architecture, along with an honest
enumeration of what remains unproven.

**Scope and venue positioning.** We treat this work as an engineering systems
and evaluation report, not a methodological machine-learning advance and not an
empirical finance paper. It does not belong in a venue that rewards a validated
algorithmic or architectural contribution, because we present no experiment
showing the multi-agent design beats a single-agent baseline. Its natural home
is an applied or systems venue, or a workshop, where a deployed, capital-bearing
system and a candid account of its unproven assumptions have value. The primary
claims are architectural and operational: that the design pattern is coherent,
that deterministic safety layers function correctly, and that the system
produces auditable decisions. We explicitly do not claim that seven agents
outperform one, that adversarial framing improves output quality, or that the
CRO's information isolation prevents anchoring. These are design hypotheses that
require ablation studies we have not yet conducted. The component with the most
research-transferable value is the evaluation apparatus — the calibration ledger
(Section 3.7) that scores per-agent forecasts against forward returns — provided
its measurement biases, which we document rather than hide, are corrected.

**Contributions:**

1. A seven-role deliberative agent architecture for portfolio management,
   with adversarially-framed agents (Devil's Advocate, Chief Risk Officer)
   structurally separated from the idea-generating agents. Whether this
   separation improves outcomes over a single-agent baseline is an open
   empirical question; the architecture is described here as a design artifact,
   not a validated technique.
2. A principled separation between the LLM deliberation layer and the
   deterministic execution layer, so guardrails cannot be circumvented by
   model output. This is an engineering design principle, not a novel algorithm.
3. A stamp-first idempotency protocol that biases all failure modes toward
   missed trades rather than duplicate trades. This is standard distributed
   systems practice applied to a live trading context.
4. An `invalidates_if` structured output field in the decision journal that
   records, for each bull thesis, a concrete condition that would falsify it;
   the condition is stored and re-injected into future analyses of the same
   ticker. We flag a limitation an earlier draft overstated: the system does
   not yet programmatically verify whether the stated condition materialized.
   The journal's `thesis_correct` flag is currently a realized-return proxy
   (Section 3.7), not a check against the `invalidates_if` text, so the
   falsifiability tracking this field was designed to enable is partially
   aspirational.
5. A calibration ledger that scores full-universe agent numeric outputs against
   realized 21-day forward returns, accumulating validation data faster than
   executed trades alone would permit.
6. An honest description of what a deployed system of this kind cannot yet
   demonstrate: ablation results, backtest validation of the quant layer,
   and statistical evidence of investment edge.

---

## 2. Related Work

**LLMs for financial analysis.** Lopez-Lira and Tang [1] demonstrate that
ChatGPT-derived sentiment signals predict short-term stock returns. Malo et
al. [2] established sentiment orientation benchmarks for economic text.
BloombergGPT [3] demonstrated domain-adapted LLMs for financial NLP. These
approaches treat the LLM as a predictor rather than a deliberative agent and
do not address autonomous execution.

**LLM-based trading agents.** FinAgent [6] describes a multimodal LLM agent
for financial trading, evaluating on simulation. FinGPT [5] explores
open-source LLM adaptation for financial tasks including portfolio management.
These works typically do not address live execution infrastructure, operational
safety, or idempotency. Our work differs in deploying on a live account with
real capital, though this deployment distinction is operational rather than
methodological, and live deployment alone does not validate the architecture.

**Multi-agent systems in finance.** Reinforcement learning multi-agent systems
for market simulation are well-studied [4]. Our system instantiates an
adversarial debate structure in production, but we do not claim superiority over
simulation-evaluated approaches without ablation evidence.

**Adversarial debate and independent review in AI.** Irving et al. [7] proposed
debate between AI agents as a scalable oversight mechanism, arguing that
adversarial framing surfaces weaknesses that a single agent might suppress.
Constitutional AI [8] introduced critic roles that evaluate model outputs
against stated principles. Our Devil's Advocate and CRO agents are motivated
by these frameworks, but the mechanism (prompt framing of an LLM agent) is
categorically different from the training-time or game-theoretic mechanisms in
those works. Whether adversarial prompt framing produces materially better
output than a single agent prompted to "consider risks" is an empirical question
we have not answered.

**Algorithmic trading systems.** Aldridge [9] covers operational concerns —
idempotency, order sequencing, circuit breakers — largely absent from ML
trading literature. We treat these as first-class requirements.

**Positioning.** The system described here is closest to FinAgent [6] and
related LLM-agent trading works, with three distinguishing operational
properties: live execution on a real account, deterministic safety layers that
are architecturally separate from the LLM pipeline, and explicit treatment of
idempotency. We acknowledge that these operational properties are engineering
contributions rather than methodological novelty.

---

## 3. System Architecture

### 3.1 Overview

The system runs a complete investment pipeline each market day. It is triggered
by a scheduled cloud routine at 9:45 AM Eastern Time, with three hourly retries.
The 9:45 AM start allows overnight news to settle but does not guarantee
favorable execution conditions; spread widening in the first 30 minutes of
trading (9:30–10:00) may still affect fills on retry attempts. Before any LLM
call, a pre-flight gate checks data freshness, API health, and idempotency.
If the gate passes, the pipeline runs a deterministic quantitative scoring
layer, then the seven-agent deliberative pipeline, then deterministic
guardrails, then execution via a brokerage API. Artifacts are committed to a
git repository and published to a real-time web dashboard.

```
GitHub Actions (8:00 AM ET)
  → Fetch 210-day OHLCV + fundamentals (Polygon, EDGAR/FMP)
  → Commit market_snapshot.json

Cloud Routine (9:45 AM ET, + 3 hourly retries)
  → git pull --rebase
  → preflight_gate.py       [exit 0: proceed | 10: retry | 20: already done]
  → main.py
      Step 1: Load portfolio (Robinhood MCP)
      Step 2: Kill-switch check (drawdown > 20%)
      Step 3: Load market snapshot (abort if stale or depth < 22 bars)
      Step 4: Quant scoring (deterministic)
      Step 5: 7-agent deliberative pipeline
      Step 6: Deterministic guardrails
      Step 7: Trade execution (Robinhood MCP)
      Step 8: Logging (CSV, JSON journal, agent log, health)
      Step 9: git commit + push → triggers publish and alert jobs

EOD Routine (4:00 PM ET)
  → Fetch portfolio close value → publish to Supabase
```

**Infrastructure caveat.** The data pipeline and the execution trigger both
run on GitHub Actions, which provides no SLA for scheduling latency. In
practice, scheduling delays have already caused stale-snapshot aborts (see
Section 5). GitHub Actions is not purpose-built financial infrastructure, and
this represents a real operational risk. Mitigation is discussed in Section 6.

### 3.2 Data Pipeline

Market data is fetched by a GitHub Actions job that runs independently of the
trading routine. Decoupling data acquisition from the trading pipeline ensures
that the trading routine always reads from a committed, versioned snapshot
rather than making live API calls that could be rate-limited or inconsistent.
The snapshot contains up to 210 days of OHLCV bars per ticker and fundamental
enrichment from SEC EDGAR (free tier) and Financial Modeling Prep (keyed tier).
The snapshot is committed to the repository, making every historical run
reproducible from the repository state.

The trading routine's pre-flight gate verifies that the snapshot date matches
today's market date and that every ticker has at least 22 bars of history before
any LLM call is made.

### 3.3 Quantitative Scoring Layer

Before any LLM agent runs, a deterministic quantitative engine scores all
candidate tickers on four factors. The factor design draws on established
quantitative factor literature [10, 11, 12] but the specific implementation —
weights, time horizons, and scoring methodology — has not been independently
backtested. Importantly, each factor is scored by a fixed-threshold absolute
mapping to a 0–100 scale (a baseline with bounded, piecewise contributions),
*not* by a cross-sectional rank or a regression-based factor construction; the
only cross-sectional step is candidate selection (top-N by composite). The
mapping to the cited premia is therefore by motivation, not by replication, and
the factor design is a starting point, not a validated signal.

**Momentum** (weight 0.30): Three time-horizon return signals (1-, 3-, and
6-month returns, i.e. 21/63/126 trading days) combined with moving-average
filters (price vs. 50-day and 200-day MA). These are mapped to a 0–100 score by
a fixed rule — a 50 baseline plus bounded contributions from each return horizon
and the MA flags — not by a cross-sectional rank. The motivation is the
momentum premium of Jegadeesh and Titman [13] and Carhart [11].

**Quality** (weight 0.25): An average of fixed-threshold sub-scores for gross
margin, operating margin, and debt-to-equity (and free-cash-flow margin when
present), sourced from SEC EDGAR (free) or FMP fundamentals when available (the
quality-minus-junk effect [12]). Return on assets is *not* used. The free SEC
EDGAR feed supplies these three quality inputs; the valuation inputs below
require an FMP key.

**Valuation** (weight 0.20): An average of fixed-threshold sub-scores for the
price-to-earnings ratio, free-cash-flow yield, and EV/EBITDA (FMP-keyed inputs),
the value premium of Fama and French [10]. Price-to-sales is not used.

**Volatility** (weight 0.25, inverted): Annualized standard deviation of the
trailing ~63-day (3-month) window of simple daily returns (× √252), mapped so
that lower volatility yields a higher score. This is the low-risk /
betting-against-beta anomaly of Frazzini and Pedersen [14].

We state these provenances plainly because they bear directly on the
interpretation of any future result: the quantitative core of this system is a
*bundle of well-documented factor premia*, not a novel signal. The economic
implication is developed in Section 6.9 — in short, any return the system
produces is presumed to be factor exposure until a factor attribution
demonstrates residual alpha.

**Factor weight rationale and its limitations.** The weights (0.30 / 0.25 /
0.20 / 0.25) were chosen by reference to the academic factor literature
assigning primacy to momentum and risk-adjusted quality, but they have not been
validated on the specific candidate universe used here. No information
coefficient or Sharpe-per-factor analysis was performed prior to deployment.
Sensitivity analysis of these weights, and a point-in-time backtest of the
composite score on a defined universe, are required before the quant layer's
design choices can be defended empirically.

**Missing data handling.** Each factor carries an `available` flag. When
fundamentals are absent (as they frequently are on the free-tier data feed),
quality and valuation factors are dropped and the remaining weights are
renormalized. This means the operative scoring function varies across tickers
and days: the system may be running a 4-factor model for some tickers and a
2-factor (momentum + volatility) model for others. The pipeline state
propagated to LLM agents discloses which factors were used for each ticker.
The rate at which fundamental data is unavailable — and thus the true
distribution of operative scoring functions — is not yet quantified.

Quant scores enter the LLM pipeline as structured context, not hard
constraints. They inform candidate selection (the top-scoring candidates plus
all current holdings, up to 20 tickers total) and appear in the portfolio
manager's prompt. The LLM may allocate to a lower-scoring name for qualitative
reasons, but this is visible in the decision journal.

A structural limitation of this selection deserves emphasis. The candidate set
is dominated by the top composite-score names (a score weighted 0.30 toward
momentum and 0.25 toward low volatility) plus current holdings; a secondary,
incidental channel admits news-mentioned tickers when price data is available,
but there is no reserved allocation for low-score names. The LLM agents
therefore deliberate almost entirely over names the quant layer already favors.
The Research Analyst's "variant perception" mandate is consequently bounded to a
momentum- and low-volatility-filtered slice of the universe: a genuinely
contrarian, low-momentum, deep-value thesis cannot be expressed because such
names rarely enter the candidate set. The LLM layer's effective degrees of
freedom are narrower than the "find where the market is wrong" framing implies.
Widening candidate selection — e.g., reserving slots for low-score names — is
untested future work.

**Candidate universe and survivorship.** The universe from which top-scoring
names are drawn is the union of a fixed analyst watchlist and the *current*
membership of the S&P 500. This is a static, present-day membership list: it is
not reconstructed point-in-time, so it embeds survivorship and universe-level
look-ahead bias — names that failed or were delisted before today never appear,
and the names present are present because they are prominent today. For the
live forward-trading path this bias is bounded (the system can only trade what
is investable now), but any *historical* information coefficient computed over
this universe is biased upward and must be read with that caveat. A
point-in-time universe is a prerequisite for trustworthy cross-sectional skill
estimates; see Section 6.

### 3.4 Seven-Agent Deliberative Pipeline

The pipeline runs seven agents in a structured sequence. Agents 2–5 analyze
individual tickers in parallel; agents 1, 6, and 7 operate at the portfolio
level in sequence.

**Table 1. The seven-agent pipeline.**

| # | Agent | Model | Scope | Output |
|---|-------|-------|-------|--------|
| 1 | Market Regime Strategist | Sonnet | Portfolio | Risk-On / Neutral / Risk-Off + macro factors |
| 2 | Research Analyst | Haiku | Per-ticker | Bull thesis, variant perception, catalysts, `invalidates_if` |
| 3 | Earnings & Catalyst Analyst | Haiku | Per-ticker | 90-day events, `earnings_alpha_score` |
| 4 | Devil's Advocate | Haiku | Per-ticker | Bear case, `recommend_reject` flag |
| 5 | Position Review Analyst | Haiku | Per-holding | Hold score (1–10), HOLD / REDUCE / EXIT |
| 6 | Portfolio Manager | Sonnet | Portfolio | `target_weight` for each position, trade list |
| 7 | Chief Risk Officer | Sonnet | Portfolio | Approve / veto, `rejected_tickers` |

**Model selection rationale.** The system uses Sonnet for the three
portfolio-level agents (Regime Strategist, Portfolio Manager, CRO) and Haiku
for the four per-ticker agents where throughput over reasoning depth is
preferred. Haiku agents use prompt caching to reduce cost: the system prompt is
a cache-eligible prefix, so repeated calls across 15–20 tickers incur only one
uncached call per agent role per run, achieving measured cache hit rates above
90%. The Sonnet/Haiku split has not been validated by ablation; the
possibility that an all-Sonnet pipeline produces materially better decisions
has not been tested. One asymmetry deserves specific scrutiny: the
idea-synthesizing Portfolio Manager runs on the stronger model (Sonnet) while
the Devil's Advocate — the agent charged with constructing the *strongest* bear
case — runs on the weaker model (Haiku). If adversarial review carries value,
assigning it the less capable model may systematically weaken the bear side
relative to the bull side. The model ablation (Section 6.3) should test
bull/bear model parity, not only the aggregate mix.

**Agent 1 — Market Regime Strategist** runs first and sets the macro context.
Its output — the regime label, growth vs. value leadership, liquidity
conditions, and volatility environment — is injected into the Portfolio
Manager's prompt. It is deliberately **not** passed to the Chief Risk Officer,
whose information isolation (Agent 7, below) excludes the regime narrative along
with the rest of the research state. A Risk-Off regime raises the bar for new
BUYs and prompts defensive positioning in the PM's instructions.

Because a single Sonnet call sets this macro frame for the entire day's
portfolio construction, the regime agent is a high-leverage single point of
influence within the LLM layer: an incorrect regime label (e.g., classifying a
risk-off tape as risk-on) systematically biases every BUY/SELL the PM proposes.
Unlike the per-ticker agents — whose 15–20 parallel calls let individual errors
partially average out across names — the regime call has no such averaging. The
deterministic guardrails (Section 3.5) bound the consequences of a wrong macro
call but do not detect or correct one; whether the regime agent improves
decisions at all, relative to omitting it, is one of the untested ablations
(Section 6.3).

**Agents 2–4** analyze each candidate ticker in parallel using a
ThreadPoolExecutor.

The Research Analyst (Agent 2) is instructed to look for **variant perception**
— where the market consensus may be wrong — rather than confirming popular
narratives. Its output includes an `invalidates_if` field: a concrete condition
that, if observed, would falsify the bull thesis. This field is stored in the
decision journal and fed back into future Agent 2 calls for the same ticker.
Whether this instruction produces genuinely contrarian analysis versus a
contrarian-sounding narrative is an open empirical question that would require
domain-expert evaluation or realized-return attribution by `invalidates_if`
quality.

The Devil's Advocate (Agent 4) operates under an adversarial system prompt that
instructs it to assume the market is correct and find the strongest bear case.
It produces a `recommend_reject` boolean that the portfolio manager sees.
The hypothesis behind this design — that naming an adversarial role produces
more critical output than asking a single agent to "consider risks" — is
motivated by the AI debate literature [7] but has not been tested in this
system. No comparison against a single-agent baseline has been conducted.

**Agent 5 — Position Review Analyst** reviews only current holdings. It
receives the original trade thesis from the decision journal, the current hold
score, and whether any `invalidates_if` conditions have materialized. It is
instructed never to anchor to purchase price.

**Agent 6 — Portfolio Manager** receives the full pipeline state: regime
analysis, per-ticker research, earnings events, devil's advocate flags,
position reviews, quant scores, current portfolio weights, cash balance, and
the recent decision history from the journal. Its default action is HOLD — it
must articulate a specific reason why trading improves portfolio expected value
before recommending any BUY or SELL. Each BUY must name the position it
displaces and the source of capital.

**Agent 7 — Chief Risk Officer** receives the PM's trade list and reviews it
for portfolio-level concentration and correlation risk. If it sets
`approved: false`, all trades are dropped. It may also emit a `rejected_tickers`
list to drop individual names while approving the remainder.

The CRO receives the PM's trade list, the resulting projected portfolio with
per-name volatility and beta, a deterministically computed correlation–
concentration block, and the cash balance — but **not** the per-ticker research
narratives, the regime analysis, or the Devil's Advocate output the PM saw. Its
inputs are therefore the proposed action plus quantitative risk metrics, not the
qualitative case for any name. The design intent is to reduce the likelihood
that the CRO's risk assessment is anchored by the research narrative constructed
for each ticker. Whether this information restriction actually changes the CRO's output
in practice, compared with receiving full research context, has not been tested.
It is equally plausible that a CRO with full context would make better-informed
risk decisions.

A further consequence of this isolation is specific to the adversarial design:
the Devil's Advocate's bear case and `recommend_reject` flag (Agent 4) are
surfaced to the Portfolio Manager but **not** to the CRO — the only agent with
veto authority. The adversarial signal therefore informs idea synthesis but
never reaches the layer empowered to block a trade. Whether routing adversarial
flags into the CRO's input would improve veto quality, or would simply
reintroduce the anchoring the isolation was meant to prevent, is untested.

### 3.5 Deterministic Guardrails

After the LLM pipeline produces a trade list, four deterministic guardrails
execute unconditionally:

1. **Validation and clamp**: Each decision is validated against a universe
   whitelist. A hard-blocked ticker list (TSLA is the configured example) causes
   any BUY to be rejected regardless of LLM reasoning. Position targets are
   clamped to [0%, 10%] and notional values are capped at 12%. Orders below $5
   minimum are dropped.

2. **Minimum holding period**: A SELL is blocked if the position was entered
   fewer than 5 trading days ago. This prevents intraweek momentum-chasing that
   would generate wash-sale complications and poor tax treatment. The 5-day
   threshold was chosen heuristically and has not been optimized.

3. **Wash-sale re-entry blocking**: A BUY is blocked for any ticker exited
   within the prior 30 calendar days. Under IRS Section 1091, the wash-sale
   rule disallows a loss deduction if a substantially identical security is
   acquired within 30 days *before or after* the loss sale. The current
   implementation enforces the post-sale 30-day window; it does not separately
   check whether a current position was purchased within 30 days before a
   prospective loss exit. Full wash-sale compliance therefore requires that
   positions not be sold at a loss within 30 days of their purchase date — a
   constraint currently enforced by the 5-day minimum holding period only
   partially. Complete wash-sale logic requires tracking both the pre-sale and
   post-sale windows explicitly.

4. **Sector cap**: Aggregate sector allocation is capped at 25%. SELLs are
   processed first (freeing budget) before BUYs are evaluated against the cap.

A circuit breaker executes after guardrails: if the total SELL notional exceeds
50% of portfolio value, all trading halts and the run is marked FAILED. A
separate kill switch blocks all BUYs when drawdown from the equity peak exceeds
20%, permitting only further SELLs. The 20% drawdown threshold was chosen to
permit meaningful exposure during moderate corrections while halting aggressive
buying in severe drawdowns; in a concentrated portfolio, 20% can be reached in
a short period during extreme market events.

Guardrails execute on the full trade list, not per-decision. Their outputs are
logged with reasons, so every dropped decision is auditable.

### 3.6 Idempotent Execution

Duplicate order execution in a live account is potentially unrecoverable.
The system uses a stamp-first idempotency protocol designed to bias every
failure mode toward a missed trade rather than a duplicate.

Before the first order is placed, `execution_started_at` is written to
`pending_decisions.json` and committed to the repository. If the process
crashes mid-execution, the pre-flight gate on the next run detects an
`execution_started_at` with no `executed_at` and exits with code 20 (SKIP/DONE),
preventing any re-execution. **This state requires manual recovery**: a human
must inspect the broker's actual positions versus the pending decisions and
clear the flag before the next scheduled run. In an ostensibly autonomous
system, this represents a gap: there is no automated reconciliation or
alerting specific to the `execution_started_at` / no `executed_at` condition.
A GitHub issue is opened on any non-OK run status (Section 4.4), but the
specific guidance for manual recovery in this state is not automated.

After all orders complete, `executed_at` is stamped and committed. Orders
execute in SELL-before-BUY order to free capital for purchases. Each order is
wrapped in a per-order try/except so a single rejected order does not abort
the remaining trades.

**Execution quality.** The system executes via Robinhood, which routes orders
through a payment-for-order-flow (PFOF) model. PFOF arrangements can result
in execution quality below the national best bid/offer (NBBO). Transaction
cost analysis — including effective spread costs and PFOF-driven price
improvement shortfall — has not been conducted. For small order sizes, the
dollar impact is likely negligible; at scale this would require explicit
measurement.

### 3.7 Memory and Feedback

The decision journal records, for each trade: the full agent-generated thesis,
the `invalidates_if` condition from the Research Analyst, the devil's advocate
bear case, and the CRO's rationale for any veto. On exit, the realized return
and a `thesis_correct` flag are recorded. We correct an earlier description
here: `thesis_correct` is *not* a check of whether the `invalidates_if`
condition materialized — it is a deterministic function of the realized return
(positive, or, when an explicit expected return was set, clearing at least half
of it). Tying the flag to actual materialization of the stated invalidation
condition — the stronger, falsifiability-based evaluation the `invalidates_if`
field was designed to enable — remains unimplemented and is tracked as future
work.

This journal feeds back into future runs: the portfolio manager receives a
summary of positions held with their original theses, and the research analyst
receives prior `invalidates_if` conditions for the same ticker. This is
structurally a retrieval-augmented generation (RAG) pattern — injecting
historical text into future prompts — rather than a learning mechanism. It
does not modify model weights or produce any persistent internal state. Whether
this injection improves decision quality over a stateless baseline is untested.

A separate forecast ledger (calibration module) logs the numeric outputs of
each agent (research confidence, hold score, earnings-alpha score, devil's-
advocate risk score) plus the quant composite at decision time, and scores them
against realized 21-day forward returns. It is observational only — no output
sizes or gates a trade. Several measurement caveats apply, and we state them
explicitly rather than let the ledger produce falsely precise skill estimates:

- **Entry-price look-ahead.** The current implementation records the
  signal-day closing price as the entry and measures the forward return from
  that close. That close is not executable — the signal is computed from it and
  fills occur the following morning — so the realized return is measured from a
  price one bar ahead of the first tradable price. This inflates apparent skill
  and must be corrected by basing forward returns on the next executable price.
- **Overlapping windows.** Forecasts are logged daily but evaluated over a
  21-day horizon, so consecutive observations share 20 of 21 days of return and
  are heavily autocorrelated. The effective sample size is far below the raw
  count (of order n/21). The current confidence interval (1.96/√n) treats
  observations as independent and overstates precision; a Newey-West or
  non-overlapping-block estimator is required.
- **Range restriction.** The ledger scores the candidate universe, which is
  already pre-filtered to the top quant scores plus holdings. An IC measured on
  this restricted, mutually-correlated sample is not the signal's IC on the
  broad market and is biased by the conditioning.
- **Multiple comparisons.** One IC and one sign-hit-rate are computed per
  (agent, field). Reporting the best of these as evidence of skill, without a
  multiplicity correction or a single pre-registered primary metric, would be data
  dredging. This is now controlled: a single primary metric (the block-sampled
  quant-composite IC at 21 days) is **externally pre-registered** (AsPredicted
  #296637), and all other agent/field metrics are exploratory and reported with a
  Benjamini-Hochberg adjustment.
- **Shrinkage scope.** Per-agent ICs are shrunk toward a no-skill prior
  (`ic · n/(n+k)`). This dampens small-sample magnitude only; it does not
  address any of the four biases above.

These caveats mean the calibration ledger is the *least* trustworthy component
on day one, not the most. The required fixes are enumerated in Section 6.3.

---

## 4. Operational Design Principles

### 4.1 LLM-Quant Separation

The system maintains a strict boundary between the LLM deliberation layer and
the deterministic layers. The quant engine, guardrails, idempotency protocol,
kill switch, and circuit breaker are written entirely without LLM calls. The
LLM pipeline can produce any output, including hallucinations or format errors,
and the deterministic layers will still execute correctly. If the LLM produces
malformed JSON, the pipeline logs an error and falls through to HOLD; if the
LLM recommends a blocked ticker, the guardrail drops it silently.

The benefit is that the system's safety properties can be reasoned about
independently of the LLM's behavior. The kill switch fires at 20% drawdown
regardless of what the LLM recommends. The wash-sale block fires regardless of
whether the LLM knows the rule. The sector cap enforces regardless of the PM
agent's portfolio theory.

### 4.2 Fail Toward Missed Trades

Every ambiguous state in the system is designed to produce an absence of
trading, not an action. A stale snapshot aborts. A degraded Anthropic API
(HTTP 529) causes the pre-flight gate to skip and retry rather than proceeding
with a reduced pipeline. A mid-execution crash sets a manual-recovery flag
rather than allowing a re-run. A CRO veto drops the entire trade list.

This is not the only valid design choice — a system optimizing for maximum
execution would make different tradeoffs. The choice reflects the asymmetry of
outcomes: a missed trade forfeits potential alpha; a duplicate trade or an
order on stale data can produce immediate and concrete losses.

### 4.3 Model Selection and Cost

The system uses claude-sonnet-4-6 for the three portfolio-level agents and
claude-haiku-4-5 for the four per-ticker agents. Per-ticker Haiku agents use
prompt caching: the system prompt is declared as a cache-eligible prefix, so
repeated calls across 15–20 tickers incur only one uncached prompt per agent
role. Measured cache hit rates on these agents exceed 90% after the first
ticker in each parallel batch.

A pre-flight canary call using 50-token Haiku validates API health before the
full pipeline runs. This is deliberately heavier than a 1-token ping: under
genuine API load, minimal-token calls can succeed while full-context calls fail.

### 4.4 Observability and Metric Honesty

The system writes `system_health.json` at the end of every run, with a status
of OK, DEGRADED, FAILED, or ABORTED and structured records for each pipeline
step. A GitHub Actions workflow reads this file and opens a GitHub issue on
any non-OK status.

The portfolio equity curve and a comparison against SPY are published to a live
web dashboard. Two accounting facts make the *current* comparison favorable to
the portfolio, and we correct the record here rather than leave the asymmetry
implicit. (1) The portfolio curve is the brokerage account's total value
(positions marked at close plus cash). Because dividends are paid into the
account as cash, the portfolio curve captures dividend income — it is
effectively a total-return series. (2) The SPY series is price return only,
excluding dividends (~1.3%/yr). Comparing a dividend-inclusive portfolio against
a dividend-excluding benchmark *flatters* the portfolio by approximately the
dividend-yield differential; an earlier version of this paper described the
comparison as "conservative against the portfolio," which was incorrect — the
bias runs the other way. The fix is a like-for-like basis: SPY total return
(from adjusted close) against the portfolio total-value curve.

A second, independent issue is exposure. The portfolio holds cash (8–15
positions plus dividend and residual cash) and therefore runs below full market
exposure, so raw return against a fully-invested SPY is not risk-matched — it
flatters the portfolio in down markets and penalizes it in up markets,
independent of skill. Correct reporting requires an exposure- or beta-matched
benchmark and disclosure of average net exposure and realized beta. Both
corrections are dashboard/report specification changes, tracked in Section 6.3.

A third issue is benchmark *appropriateness*. Because the security-selection
core is a factor composite (Section 3.3), SPY is not a sufficient benchmark on
its own: the relevant comparison is against factor ETFs (momentum,
low-volatility, quality) and a factor-model attribution that isolates any
residual alpha. This is developed in Sections 6.3 and 6.9 and is a prerequisite
for any claim that the system does more than capture known premia.

**Performance reporting threshold and pre-registration.** We require a minimum
of 60 trading days of matured, non-overlapping observations before attaching any
performance annotation to the dashboard or reporting any per-agent skill number.
This threshold — together with the single primary metric, its 21-day horizon, the
executable (next-session-open) return basis, the total-return SPY benchmark, and
the Benjamini-Hochberg multiplicity control — is **externally pre-registered**
(AsPredicted #296637, https://aspredicted.org/zm7a2p.pdf), so it cannot be
modified after the fact without detection. The primary metric is the
block-sampled rank information coefficient of the deterministic quant composite
score; all other agent/field metrics are exploratory and BH-adjusted. The
threshold reflects a judgment that sample sizes below this level are too small to
distinguish skill from luck in a concentrated equity portfolio, not a
statistically derived significance boundary; strong inference is expected to
require 252+ trading days.

After-tax performance reporting applies California top-bracket marginal rates
(approximately 54% combined for short-term, 37% for long-term gains) to
realized returns, as these represent the economically relevant returns for the
account.

---

## 5. Preliminary Results and Observations

The system has been operating on a live brokerage account since deployment.
The investment universe is US common stocks and ADRs; the portfolio maintains
8–15 positions with a maximum 10% weight per position and 25% per sector.
The account uses only long equity positions with no leverage, options, or
short selling.

**No investment performance claims are made in this paper.** The sample size
is below the pre-registered 60-trading-day threshold, and no statistically
meaningful inference can yet be drawn about investment edge, Sharpe ratio, or
benchmark-relative return.

What we *can* report is the measured behavior of the pipeline. Earlier drafts
described this behavior with denominator-free phrases ("fired on several
occasions", "set `recommend_reject` on some names") — exactly the kind of
hit-only anecdote this paper criticizes. We replace those here with base rates
computed from the agent and run logs (`deliberation_stats.py`). These are
descriptive statistics of the deliberation, not evidence of skill, and the
sample is small: **10 logged pipeline runs over 2026-06-08 to 2026-06-12 (5
trading days; multiple runs per day reflect the retry schedule).** They should be
read as base rates with wide error, and they will firm up as the deployment
accumulates runs.

**Table 2. Deliberation and operational base rates** (10 logged runs).

| Measure | Value | Denominator |
|---|---|---|
| CRO full-veto rate | 30% | 3 / 10 runs |
| Devil's-Advocate `recommend_reject` rate | 0% | 0 / 132 ticker-evaluations |
| Position-review REDUCE/EXIT rate | 49% | 37 / 75 reviews |
| PM proposed-trade rate vs candidate slots | 22% | 43 / 200 |
| Trades after deterministic guardrails | 19 | 43 proposed → 19 executed |
| No-trade run rate | 70% | 7 / 10 runs |
| Executed trades per run | 1.9 | 19 / 10 |
| One-way portfolio turnover (period) | ≈ 0.63 | sell notional / avg book |
| Kill-switch-active runs | 0 | 0 / 10 |
| Regime mix | NEUTRAL 8, RISK_ON 2 | 10 runs |

Three observations follow directly, and we state them without spin:

- **The CRO is an active gate, not decoration.** It fully vetoed the trade list on
  30% of runs. We do *not* characterize these as good or bad calls — judging
  vetoes from outcomes requires the full vetoed-versus-approved comparison (the
  CRO ablation, Section 6.3); a 30% intervention rate only establishes that the
  layer is materially engaged.
- **The Devil's Advocate's veto signal was vacuous in this window.** It set
  `recommend_reject` zero times across 132 ticker-evaluations. There is therefore
  currently *no* reject signal whose information coefficient could be measured
  (Section 6.3, item 5 is blocked until the flag fires). This is a measured fact
  that bears on the untested "adversarial framing adds value" hypothesis
  (Section 6.2): either the flag's threshold/prompt is mis-calibrated, or the bear
  case is, in practice, never decisive enough to recommend rejection — and it is
  worth noting the Devil's Advocate runs on the weaker model (Section 3.4), which
  the model-parity ablation (Section 6.3) should probe.
- **The deterministic layer materially reshapes LLM output.** The Portfolio
  Manager proposed 43 trades; 19 survived the guardrails (turnover, wash-sale,
  sector, and net-edge filters). The separation in Section 4.1 is not cosmetic:
  more than half of proposed trades were dropped by code, not by an LLM.

Safety-path observations (these remain qualitative — the relevant events are rare
by design):

- The pre-flight gate has correctly aborted runs on days with stale market
  snapshots caused by GitHub Actions scheduling delays; no stale-data trades have
  been executed. (Aborted runs are not yet counted with a denominator: aborts
  exit before writing to the agent log, and `system_health.json` is overwritten
  each run, so an abort rate requires health-history retention — noted as a
  near-term logging fix.)
- No duplicate execution has occurred; the stamp-first protocol has correctly
  prevented re-execution on retry attempts following a successful primary run.
- The `execution_started_at` / no-`executed_at` crash-recovery state has not been
  triggered in production. Recovery is now automated rather than manual: the
  pre-flight gate runs a reconciliation pass (`reconcile.py`) that diffs live
  broker positions against the intended orders and emits a specific,
  diff-driven recovery classification (no-fill / all-filled / manual); it remains
  fail-safe (it never re-trades and, in the gate, never mutates state). This path
  has been tested with synthetic crash states; it has not fired in production.

The realized short-term/long-term holding-period split — which would replace the
worst-case tax assumption in Section 6.6 with a measured number — is not yet
reportable: every closed position to date was opened before transaction logging
began, so it has no in-log cost basis to match (9 such "uncovered" sells). The
split will populate as positions opened under logging are later sold.

---

## 6. Limitations and Future Work

### 6.1 Statistical Validity

The fundamental limitation of the current system is small sample size. A live
account with approximately one trade cycle per day requires months to accumulate
a meaningful performance record. A parallel paper account at approximately 100×
the live notional is being built to accelerate statistical power, using
identical pipeline logic but independent fills. Even a 100× account cannot
substitute for time: it provides capital-scale evidence but not additional
independent trade cycles.

We note that 60 trading days (~3 months) is insufficient for strong statistical
inference in a concentrated equity portfolio. A 15-stock portfolio's realized
returns are dominated by idiosyncratic single-name risk, meaning the variance
of outcomes is high and the confidence interval on any performance estimate is
wide. Meaningful inference likely requires 252+ trading days, or equivalently
a larger universe tracked through the calibration ledger.

### 6.2 Unvalidated Architectural Claims

Several design choices in this system are hypotheses, not proven facts. We
enumerate them explicitly:

**Multi-agent superiority over single-agent.** The system uses seven agents.
Whether seven agents produce better investment decisions than a single LLM
given identical information and the same structured output schema has not been
tested. A single-agent baseline that sequentially addresses regime, research,
devil's advocacy, and risk review in one prompt chain is the minimal missing
experiment.

**Adversarial role naming improves output quality.** The claim that naming an
agent "Devil's Advocate" and tying its identity to finding weaknesses produces
materially more critical analysis than asking a single agent to "consider risks"
is plausible but undemonstrated. Validation would require side-by-side
comparison of outputs evaluated by domain experts or a held-out realized-return
attribution study.

**CRO information isolation reduces anchoring.** The CRO receives only the
PM's trade list rather than full research context. The design intent is to
reduce anchoring from the research narrative. Whether this information
restriction actually changes the CRO's decisions — and whether the change is
directionally beneficial — is an open empirical question. A CRO with full
context may make better-informed risk decisions despite anchoring risk.

**Memory feedback loop improves decisions.** Injecting prior `invalidates_if`
conditions and thesis summaries into future agent prompts is structurally a RAG
pattern. Whether this injection improves decision quality over a stateless
pipeline has not been measured.

**Quant layer adds value over the LLM layer alone.** The quant scores shape
candidate selection and appear in the PM's prompt. Whether the LLM uses them
beneficially — versus ignoring them or being anchored to them disadvantageously
— is untested.

### 6.3 Missing Experiments

The following experiments are required before any architectural claim can be
considered validated, ranked by priority. A cross-cutting methodological
requirement applies to every experiment below that compares LLM configurations
(items 1, 4, 5, 6): because the agents are sampled stochastically, a single run
per arm is uninterpretable — apparent differences may be sampling noise. Each
arm must be run over multiple seeds (and, where feasible, on a shared frozen set
of inputs), with the comparison reported as a distribution with a significance
test, not a point estimate.

1. **Single-agent baseline** *(critical)*: Replace the seven-agent pipeline
   with a single Sonnet agent receiving all the same context. Compare decision
   quality and (eventually) realized returns.

2. **Quant-only baseline** *(critical)*: Run the quant scoring layer alone,
   allocating to the top-N names by composite score without any LLM calls.
   This establishes the floor above which LLM deliberation must demonstrate
   incremental value.

3. **Quant layer point-in-time backtest** *(partially built; critical to
   complete)*: A deterministic backtest of the momentum + inverse-volatility
   strategy already exists. It reuses the production scorer unchanged, computes
   signals on close(t) and fills at open(t+1) (no look-ahead), and charges
   round-trip costs from the shared cost model. Its limitations are that it runs
   on the ~210-bar committed snapshot over the static, present-day universe
   (Section 3.3), so it (a) spans no bear market or momentum crash, (b) inherits
   the survivorship bias of the universe, and (c) cannot test the quality and
   valuation factors, which require point-in-time fundamentals the free feed
   does not provide. Completing this experiment requires a point-in-time
   universe, a point-in-time fundamental source, and a longer history archive
   (which the daily snapshot commits accumulate over time).

4. **CRO ablation** *(important)*: Compare outcomes with and without the CRO
   veto layer. Were the names the CRO rejected subsequently worse performers
   than those it approved?

5. **Devil's Advocate ablation** *(important)*: Measure the information
   coefficient of the `recommend_reject` signal against subsequent 21-day
   returns. A signal with a negative IC (rejected names outperform) would argue
   for removing this agent.

6. **Model ablation** *(important)*: Compare all-Sonnet, all-Haiku, and the
   current mixed configuration. Determine whether the Sonnet/Haiku split is
   empirically justified.

7. **Factor weight sensitivity** *(moderate)*: Test composite score
   performance across plausible weight ranges (e.g., equal-weighting all four
   factors, doubling momentum weight, dropping volatility). Establish whether
   the chosen weights are robust or sensitive.

8. **Random portfolio baseline** *(moderate)*: A randomly selected equal-weight
   10-stock portfolio rebalanced monthly establishes the return achievable by
   chance in the live period, controlling for market beta.

9. **Market regime robustness** *(moderate)*: The system has operated during
   one market regime. Out-of-distribution regime performance (momentum crashes,
   bear markets, high-volatility regimes) is entirely unknown.

10. **Factor attribution** *(critical for any alpha claim)*: Regress portfolio
    excess returns on the Fama-French factors plus momentum and
    betting-against-beta, and report the intercept (alpha) with its t-statistic
    and the factor loadings. This is the single decisive test of whether the
    system produces anything beyond factor exposure (Section 6.9). It is the
    experiment a finance reviewer will demand first.

11. **Factor-benchmark comparison** *(critical)*: Compare against low-cost
    factor ETFs (momentum, low-volatility, quality) and an equal-weight index,
    not only SPY. Beating SPY while underperforming a momentum ETF would mean the
    system merely captures the momentum premium at higher cost.

12. **Deliberation descriptive statistics** *(done — reported in Section 5,
    Table 2)*: The behavioral statistics of the pipeline — CRO veto rate,
    Devil's-Advocate `recommend_reject` rate, PM trade/no-trade rate, the
    proposed-versus-executed gap after guardrails, position-review REDUCE/EXIT
    rate, turnover, and regime mix — are now computed from the existing logs
    (`deliberation_stats.py`) and reported as base rates. Remaining gap:
    per-agent token/cost is not yet logged (it lands with the reproducibility
    package, Section 6.11), and an abort/uptime rate needs health-history
    retention.

### 6.4 LLM Backtest Contamination

The LLM models used in this system have training data cutoffs that postdate any
historical period we could backtest against. A simulation asking the LLM to
analyze a stock in 2022 is not truly out-of-sample. We therefore do not
backtest the LLM deliberation layer. This constraint is permanent given current
models; future work may use models with defined and older training cutoffs, or
evaluate on truly real-time decisions only.

### 6.5 Data Quality

The current live deployment uses free-tier fundamental data from SEC EDGAR
filings, available with a filing lag and without confirmed earnings calendars.
Estimated earnings dates may be wrong, materially affecting the Earnings
Analyst's output. Upgrading to a real-time fundamental data feed is the
highest-priority data improvement. Until then, the quality and valuation
factors are unreliable for a significant fraction of the candidate universe.

Two further point-in-time hazards compound this. First, the candidate universe
is current-membership (Section 3.3), so any historical skill estimate over it is
survivorship-biased. Second, EDGAR's company-facts endpoint returns
currently-on-file values, which incorporate later restatements; the live
quality/valuation factors and any historical IC derived from them therefore use
restatement-contaminated fundamentals until a true as-filed point-in-time source
is adopted.

### 6.6 After-Tax Economics

For a taxable account at California's top income bracket, short-term capital
gains are taxed at approximately 54% combined marginal rate. The tax arithmetic
creates a high bar for active management to be economically rational.

Specifically: SPY's historical annualized total return of approximately 10%
translates to approximately 6.3%/year after long-term capital gains taxes
(37%). For this system — assuming, as a worst case, that it generates
predominantly *short-term* gains — to match that after-tax return, it must
generate approximately 13.7%/year in pre-tax *return* (we write "return," not
"alpha":
13.7% is the gross return needed to tie the index after tax, of which only the
~3.7 percentage points above SPY's ~10% is alpha). Sustained alpha of that
magnitude, earned while paying the short-term rate, is exceptional by any
benchmark; no active strategy can be assumed to achieve it without substantial
evidence. This comparison is, if anything, conservative in
the passive direction: a true buy-and-hold index investor defers long-term gains
indefinitely (and may receive a cost-basis step-up at death), so the effective
tax rate on the passive alternative is below the 37% assumed here, and the real
pre-tax hurdle for the active system is therefore even higher than 13.7%/year.

This arithmetic argues for an explicit net-edge gate that will not recommend
a trade unless the expected pre-tax alpha, after estimated transaction costs
and estimated holding-period tax drag, exceeds a defined hurdle. Such a gate
is designed but not yet validated. In the interim, the most tax-efficient
operating mode for this system is one with low turnover and long holding
periods — a constraint that partially conflicts with the daily deliberation
cycle the architecture was designed around.

We flag that the short-term-gain assumption is exactly that — an assumption.
The realized holding-period distribution (and hence the true split between
short- and long-term gains) is not reported here, even though it is directly
computable from the existing trade log. The Portfolio Manager's default action
is HOLD and a 5-day minimum holding period applies (Section 3.5), so effective
turnover may be far lower than "daily deliberation" implies; the short-term
worst case used above brackets the tax hurdle from the unfavorable side until
realized turnover is measured (Section 6.3, item 12).

### 6.7 Hallucination in Qualitative Reasoning

LLM agents can produce confident-sounding but incorrect statements about
specific management decisions, regulatory filings, or competitive dynamics.
The `invalidates_if` field surfaces these retrospectively but does not prevent
them from influencing decisions at the time. The quant layer's data-grounded
prior and the Devil's Advocate's structural opposition reduce but do not
eliminate this risk.

### 6.8 Infrastructure and Operational Risk

The system's dependence on GitHub Actions for both data acquisition and
execution triggering is an acknowledged operational risk. Financial trading
infrastructure with real capital commitments warrants dedicated scheduling
infrastructure with guaranteed SLA and automated failover. Migration to
dedicated cloud scheduling is planned.

Similarly, the manual recovery requirement for the `execution_started_at` /
no `executed_at` crash state is a gap in the "autonomous" claim. Production
deployment of this system at meaningful scale would require automated
position reconciliation against the broker's actual state.

### 6.9 Sources of Return: Factor Exposure versus Alpha

The most important economic caveat is one the architecture cannot resolve on its
own. The quantitative core (Section 3.3) is a composite of momentum, low
volatility, quality, and value — each a documented factor premium [10, 11, 12,
13, 14]. A long-only tilt toward these factors will earn (and lose) the
corresponding factor returns regardless of any LLM reasoning. Therefore the
*default* explanation for any return this system produces is factor exposure,
not skill. The only candidate for genuine alpha is the LLM layer's marginal
contribution *over and above* the factor composite, and that quantity is exactly
what the system does not yet measure.

This framing has two consequences we accept explicitly:

1. **The correct benchmark is a factor benchmark, not just SPY.** Beating SPY
   would be uninformative if a low-cost momentum or low-volatility ETF (e.g.,
   MTUM, USMV) earns the same return. Any future performance claim must be
   evaluated against (a) factor ETFs and an equal-weight index (RSP), and (b) a
   factor-model attribution — a regression of portfolio excess returns on the
   Fama-French factors plus momentum and betting-against-beta — reporting the
   intercept (alpha) and its t-statistic. Only a statistically significant,
   positive, factor-adjusted intercept would constitute evidence of edge. These
   are listed as required experiments in Section 6.3.

2. **There is no a priori economic reason the LLM layer should generate
   mispricing-based alpha.** The Research Analyst's "variant perception"
   objective presumes the market is sometimes wrong in a way the agent can
   detect from public filings and prices. Under semi-strong-form market
   efficiency, public information is already impounded in prices, so this is a
   strong claim that requires evidence, not a design assumption. We do not claim
   the LLM layer adds alpha; we claim only that the question is well-posed and
   testable via the factor attribution above. The prior, consistent with the
   asset-pricing literature, is that it does not.

### 6.10 Decision Cadence and the Absence of Intraday Risk Control

The pipeline runs once per market day (9:45 AM ET, plus three hourly retries),
and all risk checks — including the 20% drawdown kill switch and the 50%
sell-notional circuit breaker — are evaluated only at that time. Between runs the
system does not monitor positions, prices, or its own drawdown. This has a
direct risk consequence: an intraday event that occurs after the daily run — a
flash crash, a single-name gap on news, a sector dislocation, or a fast
drawdown — cannot trigger any system response until the next scheduled cycle,
up to a full trading day later. The system has no position-level stop-loss and
no intraday monitoring loop; the kill switch is a once-daily gate on new BUYs,
not a real-time protective mechanism. Single-name exposure is bounded by the
10% position / 12% notional cap and sector exposure by the 25% cap, so a single
intraday blow-up is bounded in portfolio terms by those caps, but a correlated
intraday move across a concentrated, partially-overlapping book is not
similarly bounded. A production deployment intended to survive fast tail events
would require an always-on monitoring process with intraday halt/liquidation
authority — infrastructure this system does not have. This is tracked as
required future work.

### 6.11 Reproducibility and Non-Determinism

Two factors limit exact reproducibility, and we state them so that neither a
reader nor a future ablation mistakes sampling noise for a result. First, the
LLM agents are sampled stochastically: identical inputs do not guarantee
identical outputs, so a single run of the pipeline — or of any of its proposed
ablations (Section 6.3) — is one draw from a distribution, not a fixed point.
Any claim that one configuration beats another must be established over multiple
seeds with a significance test, not from a single comparison. Second, exact
replay of a historical run depends on the specific model snapshots
(claude-sonnet-4-6, claude-haiku-4-5), the full agent prompts, and the sampling
parameters all being held fixed and published; the released code permits
inspection of the pipeline, but a complete reproducibility package — pinned
model snapshots, verbatim prompts, and decoding parameters — is required for
another researcher to reproduce decisions exactly. The market snapshots
themselves are committed and versioned (Section 3.2), so the *data* path is
reproducible; the *model* path is not yet pinned to that standard.

---

## 7. Conclusion

We have described an autonomous equity portfolio management system that
combines a seven-role deliberative LLM pipeline with deterministic quantitative
scoring and deterministic safety guardrails. The architecture separates
deliberation from execution: LLM agents reason about theses, regime context,
adversarial bear cases, and portfolio construction, while deterministic layers
handle all safety-critical decisions unconditionally. The stamp-first
idempotency protocol ensures that failures default to missed trades rather
than duplicate orders.

**What this paper establishes:** The architecture is coherent and operational.
The deterministic safety layers function correctly. The system produces auditable
decisions and has executed live trades without operational failures attributable
to architectural design.

**What this paper does not establish:** That seven agents outperform one. That
adversarial role framing improves output quality. That the CRO's information
isolation reduces anchoring in practice. That the quantitative scoring layer
generates alpha. That the system produces returns above the after-tax hurdle
required to justify active management over index investing. These remain open
empirical questions, and we have enumerated the experiments required to answer
them.

The system is accumulating the live track record and agent calibration data
required to evaluate these claims. The first reportable results will not be
performance numbers but two things that need no return history and carry none of
the calibration ledger's biases: the descriptive statistics of the deliberation
itself (veto rates, reject rates, inter-agent disagreement), computable now from
the agent log, and — once a return series exists — a factor attribution that
tests whether the system produces anything beyond the known premia its
quantitative core is built from. Per-agent information coefficients from the
calibration ledger will follow only after their documented measurement biases
(Section 3.7) are corrected, and should be read as the least, not the most,
reliable evidence the system generates.

We release the system architecture and code for inspection by the research
community, in the hope that the architecture and the honest enumeration of its
unvalidated assumptions will be useful to others building in this space.

---

## References

[1] Lopez-Lira, A., & Tang, Y. (2023). Can ChatGPT forecast stock price
movements? *arXiv:2304.07619*.

[2] Malo, P., et al. (2014). Good debt or bad debt: Detecting semantic
orientations in economic texts. *Journal of the American Society for
Information Science and Technology*, 65(4), 782–796.

[3] Wu, S., et al. (2023). BloombergGPT: A large language model for finance.
*arXiv:2303.17564*.

[4] Amrouni, S., et al. (2021). ABIDES-Gym: Gym environments for multi-agent
discrete event simulation and application to financial markets. *ICAIF 2021*.

[5] Yang, H., et al. (2023). FinGPT: Open-source financial large language
models. *arXiv:2306.06031*.

[6] Zhang, Y., et al. (2024). FinAgent: A multimodal foundation agent for
financial trading. *arXiv:2402.18485*.

[7] Irving, G., Christiano, P., & Amodei, D. (2018). AI safety via debate.
*arXiv:1805.00899*.

[8] Bai, Y., et al. (2022). Constitutional AI: Harmlessness from AI feedback.
*arXiv:2212.08073*.

[9] Aldridge, I. (2013). *High-Frequency Trading: A Practical Guide to
Algorithmic Strategies and Trading Systems*. Wiley.

[10] Fama, E. F., & French, K. R. (1993). Common risk factors in the returns
on stocks and bonds. *Journal of Financial Economics*, 33(1), 3–56.

[11] Carhart, M. M. (1997). On persistence in mutual fund performance.
*Journal of Finance*, 52(1), 57–82.

[12] Asness, C. S., Moskowitz, T. J., & Pedersen, L. H. (2013). Value and
momentum everywhere. *Journal of Finance*, 68(3), 929–985.

[13] Jegadeesh, N., & Titman, S. (1993). Returns to buying winners and selling
losers: Implications for stock market efficiency. *Journal of Finance*, 48(1),
65–91.

[14] Frazzini, A., & Pedersen, L. H. (2014). Betting against beta. *Journal of
Financial Economics*, 111(1), 1–25.

---

*Code available at: https://github.com/parthchoksi7/ai-investor*
