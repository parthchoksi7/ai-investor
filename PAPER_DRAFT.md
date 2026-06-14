# Deliberative Multi-Agent LLM Architecture for Autonomous Equity Portfolio Management

**Parth Choksi**  
Independent Researcher  
parthchoksi007@gmail.com

---

## Abstract

We present a fully autonomous equity trading system that executes a seven-agent
deliberative pipeline every market day without human intervention. The system
separates concerns cleanly: deterministic quantitative scoring provides a
data-grounded prior on each candidate security, while a sequence of specialized
large language model (LLM) agents reasons about regime context, investment
theses, adversarial bear cases, and position reviews before a portfolio manager
synthesizes capital allocation decisions. A chief risk officer agent exercises
independent veto power over the portfolio manager's output. Deterministic
guardrails — sector caps, minimum holding periods, wash-sale re-entry blocking,
and a kill switch triggered by drawdown — execute after all LLM deliberation,
so no model output can directly produce an illegal or policy-violating order.
An idempotency protocol biases every failure mode toward missed trades rather
than duplicate trades. The system is deployed on a live brokerage account,
running daily with real capital. We describe the architecture, design principles,
operational infrastructure, and early observational results, and discuss the
limitations that must be resolved before statistical claims about edge can be
made.

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

We describe such a system, deployed live on a Robinhood brokerage account. The
system makes no claim to have discovered a persistent edge — its live history is
too short for statistical significance. What it demonstrates is an architectural
pattern: how to combine LLM deliberation with deterministic quantitative scoring
and layered deterministic safety controls to produce a system that trades
autonomously, fails safely, and maintains an auditable decision journal.

**Contributions:**

1. A seven-role deliberative agent architecture for portfolio management,
   with adversarial agents (Devil's Advocate, Chief Risk Officer) structurally
   independent from the idea-generating agents.
2. A principled separation between the LLM deliberation layer and the
   deterministic execution layer, so guardrails cannot be circumvented by model
   output.
3. A stamp-first idempotency protocol that biases all failure modes toward missed
   trades rather than duplicate trades.
4. A cost-efficient parallelization strategy using prompt caching across per-ticker
   agents.
5. A memory feedback loop that injects decision journal history into future
   pipeline runs, enabling conditional learning without fine-tuning.
6. An honest observability design: after-tax scorecards that account for
   California top-bracket marginal rates, and a pre-registered significance
   threshold of 60 trading days before any performance claim is reported.

---

## 2. Related Work

**LLMs for financial analysis.** Several papers demonstrate that LLMs produce
useful sentiment signals from financial text [1, 2] and can outperform
specialized fine-tuned models on certain NLP finance tasks [3]. These approaches
treat the LLM as a predictor rather than a deliberative agent and do not address
autonomous execution.

**Multi-agent systems in finance.** Reinforcement learning multi-agent systems
for market simulation are well-studied [4]. Hybrid LLM architectures for
financial question answering have been explored [5], but these operate in
retrieval-augmented generation (RAG) settings without execution pipelines.
FinAgent [6] and similar recent work describe LLM-based trading agents but
typically evaluate on simulation rather than live accounts and do not address
operational safety, idempotency, or after-cost evaluation.

**Adversarial debate in AI.** The use of competing agents to improve reasoning
quality was proposed by Irving et al. [7] as a scalable oversight mechanism.
Constitutional AI [8] introduced independent critic roles. Our Devil's Advocate
and Chief Risk Officer agents instantiate this debate structure in a production
financial context.

**Algorithmic trading systems.** Systematic execution systems [9] emphasize
operational concerns — idempotency, order sequencing (SELL before BUY to free
capital), circuit breakers — that are largely absent from academic ML trading
literature. We treat these as first-class requirements.

Our work differs from prior work in three ways: (1) the system executes on a
live brokerage account with real capital; (2) adversarial agents are structurally
independent veto actors, not mere prompts in a single-agent chain; (3) the
design explicitly separates LLM deliberation from deterministic guardrails and
gives guardrails unconditional priority.

---

## 3. System Architecture

### 3.1 Overview

The system runs a complete investment pipeline each market day. It is triggered
by a scheduled cloud routine at 9:45 AM Eastern Time, with three hourly retries.
Before any LLM call, a pre-flight gate checks data freshness, API health, and
idempotency. If the gate passes, the pipeline runs a deterministic quantitative
scoring layer, then the seven-agent deliberative pipeline, then deterministic
guardrails, then execution via a brokerage API. Artifacts are committed to a git
repository and published to a real-time web dashboard.

```
GitHub Actions (8:00 AM ET)
  → Fetch 210-day OHLCV + fundamentals (Polygon, EDGAR/FMP)
  → Commit market_snapshot.json

Cloud Routine (9:45 AM ET, + 3 retries)
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

### 3.2 Data Pipeline

Market data is fetched by a GitHub Actions job that runs independently of the
trading routine. Decoupling data acquisition from the trading pipeline ensures
that the trading routine always reads from a committed, versioned snapshot rather
than making live API calls that could be rate-limited or inconsistent. The
snapshot contains up to 210 days of OHLCV bars per ticker and fundamental
enrichment from SEC EDGAR (free tier) and Financial Modeling Prep (keyed tier).
The snapshot is committed to the repository, making every historical run
reproducible from the repository state.

The trading routine's pre-flight gate verifies that the snapshot date matches
today's market date and that every ticker has at least 22 bars of history before
any LLM call is made. This prevents the quantitative scoring layer from
producing degenerate scores on incomplete data.

### 3.3 Quantitative Scoring Layer

Before any LLM agent runs, a deterministic quantitative engine scores all
candidate tickers on four factors:

**Momentum** (weight 0.30): Three time-horizon momentum signals (1-month,
3-month, 6-month returns) combined with moving-average filters (price vs.
50-day and 200-day MA). Scored 0–100 using cross-sectional ranks.

**Quality** (weight 0.25): Gross profit margin and return on assets, sourced
from SEC EDGAR fundamentals when available.

**Valuation** (weight 0.20): Revenue-based price-to-sales and earnings-yield
proxies, cross-sectionally ranked.

**Volatility** (weight 0.25, inverted): Annualized standard deviation of
log-returns computed from the OHLCV history.

A critical design choice: the composite score is honest about missing data.
Each factor carries an `available` flag. When fundamentals are absent (as they
are on the free-tier data feed today), quality and valuation factors are dropped
and the remaining weights are renormalized. The composite score does not impute
a neutral 50 for unavailable factors, which would produce an advertised
"4-factor" score that silently contains only 2 factors. The honesty about data
availability propagates into the pipeline state that LLM agents receive.

Quant scores enter the LLM pipeline as structured context, not as hard
constraints. They inform candidate selection (the top-scoring candidates plus all
current holdings, up to 20 tickers total) and appear in the portfolio manager's
prompt. The LLM may allocate to a lower-scoring name for qualitative reasons,
but this is visible in the decision journal.

### 3.4 Seven-Agent Deliberative Pipeline

The pipeline runs seven agents in a structured sequence. Agents 2–5 analyze
individual tickers in parallel; agents 1, 6, and 7 operate at the portfolio
level in sequence.

| # | Agent | Model | Scope | Output |
|---|-------|-------|-------|--------|
| 1 | Market Regime Strategist | Sonnet | Portfolio | Risk-On / Neutral / Risk-Off + macro factors |
| 2 | Research Analyst | Haiku | Per-ticker | Bull thesis, variant perception, catalysts, `invalidates_if` |
| 3 | Earnings & Catalyst Analyst | Haiku | Per-ticker | 90-day events, `earnings_alpha_score` |
| 4 | Devil's Advocate | Haiku | Per-ticker | Bear case, `recommend_reject` flag |
| 5 | Position Review Analyst | Haiku | Per-holding | Hold score (1–10), HOLD / REDUCE / EXIT |
| 6 | Portfolio Manager | Sonnet | Portfolio | `target_weight` for each position, trade list |
| 7 | Chief Risk Officer | Sonnet | Portfolio | Approve / veto, `rejected_tickers` |

**Agent 1 — Market Regime Strategist** runs first and sets the macro context.
Its output — the regime label, growth vs. value leadership, liquidity conditions,
and volatility environment — is injected into every subsequent portfolio-level
agent prompt. Regime shapes the PM's allocation philosophy: a Risk-Off regime
raises the bar for new BUYs and prompts defensive positioning.

**Agents 2–4** analyze each candidate ticker in parallel using a
ThreadPoolExecutor. All three Haiku agents use Anthropic prompt caching: the
system prompt is a cache-eligible prefix, so repeated calls across 15–20 tickers
incur only one uncached call per agent role per run. This reduces token cost by
approximately 70–80% at the per-ticker analysis level.

The Research Analyst (Agent 2) is explicitly instructed to look for **variant
perception** — where the market consensus may be wrong — rather than confirming
popular narratives. Its output includes an `invalidates_if` field: a concrete
condition that, if observed, would falsify the bull thesis. This field is stored
in the decision journal and feeds back into future Agent 2 calls for the same
ticker.

The Devil's Advocate (Agent 4) operates under an adversarial system prompt that
instructs it to assume the market is correct and find the strongest bear case.
It produces a `recommend_reject` boolean that the portfolio manager sees. Naming
the adversarial role explicitly, rather than asking a single agent to "consider
risks," produces materially more critical output: the agent's identity is tied to
finding weaknesses, not protecting a thesis.

**Agent 5 — Position Review Analyst** reviews only current holdings. It receives
the original trade thesis from the decision journal, the current hold score, and
whether any `invalidates_if` conditions have materialized. It is explicitly told
never to anchor to purchase price — its role is prospective, not retrospective.

**Agent 6 — Portfolio Manager** receives the full pipeline state: regime
analysis, per-ticker research, earnings events, devil's advocate flags, position
reviews, quant scores, current portfolio weights, cash balance, and the recent
decision history from the journal. Its default action is HOLD — it must
articulate a specific reason why trading improves portfolio expected value before
recommending any BUY or SELL. Each BUY must name the position it displaces and
the source of capital.

**Agent 7 — Chief Risk Officer** receives the PM's trade list and reviews it for
portfolio-level concentration and correlation risk. It has unconditional veto
power. If it sets `approved: false`, all trades are dropped. It may also emit a
`rejected_tickers` list to drop individual names while approving the remainder.
The CRO is structurally independent: it receives only the PM's output plus the
current portfolio, not the full research state. This prevents the research
narrative from anchoring the risk review.

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
   would generate wash-sale complications and poor tax treatment.

3. **Wash-sale re-entry blocking**: A BUY is blocked for any ticker exited
   within the prior 30 calendar days. This enforces wash-sale compliance and
   prevents the system from re-entering positions before a realized loss can be
   recognized for tax purposes.

4. **Sector cap**: Aggregate sector allocation is capped at 25%. SELLs are
   processed first (freeing budget) before BUYs are evaluated against the cap.

A circuit breaker executes after guardrails: if the total SELL notional exceeds
50% of portfolio value, all trading halts and the run is marked FAILED. A
separate kill switch blocks all BUYs when drawdown from the equity peak exceeds
20%, permitting only further SELLs.

Guardrails execute on the full trade list, not per-decision. Their outputs are
logged with reasons, so every dropped decision is auditable.

### 3.6 Idempotent Execution

Duplicate order execution in a live account is potentially unrecoverable: a
second BUY at the wrong time can cause significant unintended exposure. The
system uses a stamp-first idempotency protocol designed to bias every failure
mode toward a missed trade rather than a duplicate.

Before the first order is placed, `execution_started_at` is written to
`pending_decisions.json` and committed to the repository. If the process
crashes mid-execution, the pre-flight gate on the next run detects an
`execution_started_at` with no `executed_at` and exits with code 20 (SKIP/DONE),
preventing any re-execution. Recovery requires manual inspection of the broker's
actual positions versus the pending decisions.

After all orders complete, `executed_at` is stamped and committed. A run that
completes cleanly produces both timestamps; the pre-flight gate's exit-20
condition on subsequent runs uses the presence of `executed_at`.

Orders execute in SELL-before-BUY order to free capital for purchases. Each
order is wrapped in a per-order try/except so a single rejected order does not
abort the remaining trades.

### 3.7 Memory and Feedback

The decision journal records, for each trade: the full agent-generated thesis,
the `invalidates_if` condition from the Research Analyst, the devil's advocate
bear case, and the CRO's rationale for any veto. On exit, the realized return
and a `thesis_correct` flag (based on whether the stated invalidation condition
materialized) are recorded.

This journal feeds back into future runs in two ways. The portfolio manager
receives a summary of positions held with their original theses, enabling
continuity of reasoning across daily runs. The research analyst receives the
`invalidates_if` conditions from prior coverage of the same ticker, so it can
check whether bear case triggers have fired. This creates a soft memory
mechanism without any model fine-tuning.

A separate forecast ledger (calibration module) logs the numeric outputs of each
agent (confidence scores, hold scores, earnings alpha scores, risk scores) at
the time of decision and scores them against realized 21-day forward returns. The
ledger scores the full candidate universe, not just executed trades, to accumulate
calibration data faster. Per-agent information coefficients and sign-hit-rates are
computed with shrinkage toward a no-skill prior, so a handful of observations
cannot produce misleadingly high skill estimates.

---

## 4. Operational Design Principles

### 4.1 LLM-Quant Separation

The system maintains a strict boundary between the LLM deliberation layer and
the deterministic layers. The quant engine, guardrails, idempotency protocol,
kill switch, and circuit breaker are written entirely without LLM calls. The
LLM pipeline can produce any output, including hallucinations or format errors,
and the deterministic layers will still execute correctly. This is enforced by
architecture rather than by relying on LLM instruction-following: if the LLM
produces malformed JSON, the pipeline logs an error and falls through to HOLD;
if the LLM recommends a blocked ticker, the guardrail silently drops it.

The benefit is that the system's safety properties can be reasoned about
independently of the LLM's behavior. The kill switch fires at 20% drawdown
regardless of what the LLM recommends. The wash-sale block fires regardless of
whether the LLM knows the rule. The sector cap enforces regardless of the PM
agent's portfolio theory.

### 4.2 Fail Toward Missed Trades

Every ambiguous state in the system is designed to produce an absence of trading,
not an action. A stale snapshot aborts. A degraded Anthropic API (HTTP 529)
causes the pre-flight gate to skip and retry rather than proceeding with a
reduced pipeline. A mid-execution crash sets a manual-recovery flag rather than
allowing a re-run. A CRO veto drops the entire trade list.

This is not the only valid design choice — a system optimizing for maximum
execution would make different tradeoffs. The choice reflects the asymmetry of
outcomes: a missed trade forfeits potential alpha; a duplicate trade or an order
on stale data can produce immediate and concrete losses.

### 4.3 Model Selection and Cost

The system uses two Claude models: claude-sonnet-4-6 (Sonnet) for the three
portfolio-level agents requiring deeper reasoning, and claude-haiku-4-5 (Haiku)
for the four per-ticker agents where throughput matters more than reasoning depth.
Per-ticker Haiku agents use prompt caching: the system prompt (typically 300–500
tokens) is declared as a cache-eligible prefix, so the repeated calls across 15–20
tickers incur only one uncached prompt per agent role. Measured cache hit rates on
these agents exceed 90% after the first ticker in each parallel batch.

A pre-flight canary call using 50-token Haiku validates API health before the
full pipeline runs. This is deliberately heavier than a 1-token ping: under
genuine API load, minimal-token calls can succeed while full-context calls fail
silently with empty responses.

### 4.4 Observability and Honest Metrics

The system writes `system_health.json` at the end of every run, with a status of
OK, DEGRADED, FAILED, or ABORTED and structured records for each pipeline step.
A GitHub Actions workflow reads this file and opens a GitHub issue on any
non-OK status. The portfolio equity curve and a comparison against SPY price
return are published to a live web dashboard.

All performance reporting carries explicit caveats: SPY comparison uses price
return (understating the index by ~1.3%/yr from excluded dividends), the
portfolio figure includes cash drag, and a pre-registered threshold of 60 trading
days is required before any performance annotation is presented as statistically
meaningful. The after-tax scorecard applies California top-bracket marginal rates
(approximately 54% for short-term gains, 37% for long-term) to net realized
returns, as these represent the economically relevant returns for the account.

---

## 5. Preliminary Results and Observations

The system has been operating on a live brokerage account since deployment. The
investment universe is US common stocks and ADRs; the portfolio maintains 8–15
positions with a maximum 10% weight per position and 25% per sector. The account
uses only long equity positions with no leverage, options, or short selling.

Below the pre-registered 60-trading-day significance threshold, we do not make
claims about investment edge. We report observational system behavior:

- The pre-flight gate has correctly skipped runs on all days with stale market
  snapshots (due to GitHub Actions scheduling delays) without any missed
  idempotency guarantees.
- The CRO veto has fired on several occasions, dropping trades recommended by the
  PM due to concentration risk in the same sector.
- The Devil's Advocate `recommend_reject` flag has correctly identified names
  where subsequent price action was consistent with the stated bear case,
  though sample size is insufficient to estimate information coefficient.
- No duplicate execution has occurred; the stamp-first protocol has been triggered
  in its skip-done mode on multiple runs (e.g., retry attempts after a successful
  primary run).

Full quantitative results will be reported when the pre-registered sample
threshold is reached.

---

## 6. Limitations and Future Work

**Statistical validity.** The fundamental limitation of the current system is
small sample size. A live account with approximately one trade cycle per day
requires months to accumulate a statistically meaningful performance record.
We are building a parallel paper account at approximately 100× the live
notional to accelerate statistical power, using identical pipeline logic but
independent fills.

**LLM backtest contamination.** The LLM models used in this system have training
data cutoffs that postdate any historical period we could backtest against. A
simulation that asks the LLM to analyze a stock in 2022 is not truly out-of-sample
— the model may have absorbed return outcomes from that period in training. We
therefore do not backtest the LLM deliberation layer. The deterministic quant
layer has a separate point-in-time backtest that does not involve LLM calls.

**Data quality.** The current live deployment uses free-tier fundamental data
from SEC EDGAR filings, which are available with a filing lag and may not reflect
the most recent earnings estimates or consensus. Earnings dates in the snapshot
are estimated, not confirmed. A significant fraction of the Earnings Analyst's
output may therefore be based on imprecise calendar information. Upgrading to a
real-time fundamental data feed with confirmed earnings calendars is the highest-
priority data improvement.

**After-tax drag.** For a taxable account in California's top income bracket,
short-term capital gains are taxed at approximately 54% combined marginal rate,
compared with approximately 37% for long-term gains. A system that generates
frequent turnover must produce substantially higher pre-tax returns than a
buy-and-hold index strategy to deliver equivalent after-tax wealth. This tax
arithmetic argues for reducing turnover and explicitly incorporating holding-
period cost into the allocation decision. The cost model and net-edge gate
described in Section 3.5 are designed to enforce this, but these components
are still being validated.

**Hallucination in qualitative reasoning.** LLM agents can produce confident-
sounding but incorrect statements about specific management decisions, regulatory
filings, or competitive dynamics. The `invalidates_if` field in the decision
journal is designed to surface these later, but does not prevent them from
influencing the decision at the time. The presence of a Quant layer that
provides a data-grounded prior, and a Devil's Advocate that structurally opposes
the thesis, reduces but does not eliminate this risk.

**Single-account deployment.** The system operates on a single Robinhood account
with limited capital. Order sizes are small enough that market impact is
negligible, but the account cannot demonstrate viability at institutional scale.
The 100× paper account is the mechanism for evaluating scalability without
committing additional capital.

---

## 7. Conclusion

We have described an autonomous equity portfolio management system that combines
a seven-role deliberative LLM pipeline with deterministic quantitative scoring
and deterministic safety guardrails. The core architectural insight is the
separation of deliberation from execution: LLM agents reason about theses,
regime context, adversarial bear cases, and portfolio construction, while
deterministic layers handle all safety-critical decisions unconditionally. The
stamp-first idempotency protocol ensures that failures default to missed trades
rather than duplicate orders.

The adversarial structure — a Devil's Advocate agent that attempts to falsify
each investment thesis, and a Chief Risk Officer agent with unconditional veto
power — produces qualitatively different outputs than a single-agent or
chain-of-thought approach. The portfolio manager cannot rationalize past a
dedicated bear-case generator, and the risk officer cannot be captured by the
research narrative because it receives only the trade list, not the full
deliberation.

The system has been deployed live and is accumulating the track record required
to make statistical claims about investment edge. We expect these results to be
available after the pre-registered 60-trading-day threshold is met. We release
the system architecture and code for inspection by the research community.

---

## References

[1] Lopez-Lira, T., & Tang, Y. (2023). Can ChatGPT forecast stock price movements? *arXiv:2304.07619*.

[2] Malo, P., et al. (2014). Good debt or bad debt: Detecting semantic orientations in economic texts. *Journal of the American Society for Information Science and Technology*, 65(4), 782–796.

[3] Wu, S., et al. (2023). BloombergGPT: A large language model for finance. *arXiv:2303.17564*.

[4] Amrouni, S., et al. (2021). ABIDES-Gym: Gym environments for multi-agent discrete event simulation and application to financial markets. *ICAIF 2021*.

[5] Zhao, H., et al. (2024). FinQA: A dataset of numerical reasoning over financial data. *arXiv:2109.00122*.

[6] Zhang, Y., et al. (2024). FinAgent: A multimodal foundation agent for financial trading. *arXiv:2402.18485*.

[7] Irving, G., Christiano, P., & Amodei, D. (2018). AI safety via debate. *arXiv:1805.00899*.

[8] Bai, Y., et al. (2022). Constitutional AI: Harmlessness from AI feedback. *arXiv:2212.06950*.

[9] Aldridge, I. (2013). *High-Frequency Trading: A Practical Guide to Algorithmic Strategies and Trading Systems*. Wiley.

---

*Code available at: https://github.com/parthchoksi7/ai-investor*
