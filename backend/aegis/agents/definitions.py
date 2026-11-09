"""The six agents. Each has its own system prompt, responsibilities, input/output schema and validation."""
from __future__ import annotations

from aegis.agents.base import Agent
from aegis.agents.schemas import (
    AnalystInput,
    AnalystOutput,
    NewsInput,
    NewsOutput,
    RiskManagerInput,
    RiskManagerOutput,
    ScoutInput,
    ScoutOutput,
    SkepticInput,
    SkepticOutput,
    TechnicalInput,
    TechnicalOutput,
)


class ScoutAgent(Agent[ScoutInput, ScoutOutput]):
    name, title = "scout", "Scout"
    role = "Finds and ranks potential trading opportunities from the live market scan."
    tools = ["market_scanner", "ticker_cache"]
    input_model, output_model = ScoutInput, ScoutOutput
    system_prompt = """
You are SCOUT, the opportunity-finding agent of a spot crypto trading desk (long-only, no leverage).
You receive candidates produced by a deterministic scanner over real exchange data: 24h volume, volume ratio and
z-score versus the 20-bar average, breakouts of the 20-bar range, 1h and 24h price change, and bid/ask spread.

Your job:
1. Judge which candidates are genuine opportunities worth a full (expensive) analysis: unusual volume that confirms
   a price move, clean breakouts, sustained momentum. Prefer liquid markets with tight spreads.
2. Be selective. Reject thin or erratic markets, moves that look exhausted (very large 24h change with fading
   volume), wide spreads, and symbols that already have an open position.
3. Rank your picks (rank 1 = best). Pick at most `max_picks`. Picking nothing is a valid, often correct answer.
4. Only use symbols that appear in `candidates`. Give every non-picked candidate a short rejection reason.
"""

    def skip(self, inp: ScoutInput) -> tuple[ScoutOutput, str] | None:
        if not inp.candidates:
            return ScoutOutput(market_summary="Scanner found no unusual activity.", picks=[], rejected=[]), "no scanner candidates"
        return None

    def post_validate(self, inp: ScoutInput, out: ScoutOutput) -> ScoutOutput:
        allowed = {c["symbol"] for c in inp.candidates} - set(inp.open_position_symbols)
        seen: set[str] = set()
        picks = []
        for p in sorted(out.picks, key=lambda p: p.rank):
            if p.symbol in allowed and p.symbol not in seen:  # hallucinated / duplicate / held symbols are dropped
                seen.add(p.symbol)
                picks.append(p)
        out.picks = picks[: inp.max_picks]
        for i, p in enumerate(out.picks, start=1):
            p.rank = i
        return out

    def confidence_of(self, out: ScoutOutput) -> float | None:
        return max((p.confidence for p in out.picks), default=None)

    def symbol_of(self, inp: ScoutInput) -> str | None:
        return None


class NewsAgent(Agent[NewsInput, NewsOutput]):
    name, title = "news", "News & Sentiment"
    role = "Classifies sentiment from real news items and the Fear & Greed index."
    tools = ["news_store", "fear_greed_index"]
    input_model, output_model = NewsInput, NewsOutput
    system_prompt = """
You are NEWS & SENTIMENT, the information analyst of a crypto trading desk.
You receive real, timestamped news items (each with a numeric `id` and source) collected from RSS/API feeds for
one asset and for the wider market, plus the Fear & Greed index when available.

Your job:
1. Identify market-moving events (listings, hacks, regulation, ETF flows, unlocks, macro) relevant to the asset.
2. Every key event MUST cite the `id` of at least one provided item in `source_ids`. Never cite an id that was
   not provided and never describe an event that is not in the items.
3. Detect conflicting information between sources and list it in `conflicts`.
4. Classify overall sentiment as bullish, bearish or neutral with a score in [-1, 1].
5. Set `data_quality`: "none" if there are no relevant items, "low" if only generic market news, "medium"/"high"
   when multiple credible asset-specific items agree. With little data, stay neutral with low confidence.
The Fear & Greed index is a market-wide social/behavioural gauge; treat it as context, not as asset news.
"""

    def skip(self, inp: NewsInput) -> tuple[NewsOutput, str] | None:
        if not inp.asset_news and not inp.market_news and not inp.fear_greed:
            return NewsOutput(sentiment="neutral", score=0.0, confidence=0.0, data_quality="none",
                              summary="No news or sentiment data was available from any source; no assessment made."), "no news data available"
        return None

    def post_validate(self, inp: NewsInput, out: NewsOutput) -> NewsOutput:
        valid = {int(n["id"]) for n in [*inp.asset_news, *inp.market_news] if "id" in n}
        kept = []
        for ev in out.key_events:
            ev.source_ids = [i for i in ev.source_ids if i in valid]
            if ev.source_ids:  # events without a real source are treated as fabricated and removed
                kept.append(ev)
        out.key_events = kept
        if not inp.asset_news and not inp.market_news:
            out.data_quality = "none" if not inp.fear_greed else "low"
        if out.data_quality == "none":
            out.sentiment, out.score, out.confidence = "neutral", 0.0, min(out.confidence, 0.2)
        return out


class TechnicalAgent(Agent[TechnicalInput, TechnicalOutput]):
    name, title = "technical", "Technical Analyst"
    role = "Interprets deterministic indicators (RSI, MACD, EMA 20/50/200, Bollinger, volume, momentum, volatility, S/R)."
    tools = ["candle_store", "indicator_engine"]
    input_model, output_model = TechnicalInput, TechnicalOutput
    system_prompt = """
You are TECHNICAL ANALYST. All indicator values you receive were computed deterministically from real exchange
candles (closed bars only) for several timeframes: RSI(14), MACD(12,26,9) with cross state, EMA 20/50/200 and trend
stack, Bollinger Bands(20,2) with %B and width, volume versus its 20-bar average, rate-of-change momentum,
ATR(14) and realised volatility, and swing support/resistance levels.

Your job:
1. Interpret the readings; never recompute or alter them, and never quote a value that is not in the input.
2. Assess directional bias (bullish/bearish/neutral), its strength, and whether timeframes are aligned.
3. List the specific signals that drive your view, each tied to an indicator and timeframe.
4. Choose `key_support` and `key_resistance` from the provided levels (or null if none are relevant).
5. Classify volatility using ATR% and realised volatility.
A timeframe with "sufficient_data": false must be ignored. Mixed evidence means neutral with low strength.
"""

    def skip(self, inp: TechnicalInput) -> tuple[TechnicalOutput, str] | None:
        if not any(tf.get("sufficient_data") for tf in inp.timeframes.values()):
            return TechnicalOutput(bias="neutral", strength=0.0, confidence=0.0, timeframe_alignment="mixed", volatility="normal",
                                   summary="Insufficient candle history to compute indicators."), "insufficient candle data"
        return None

    def post_validate(self, inp: TechnicalInput, out: TechnicalOutput) -> TechnicalOutput:
        levels = {"support": set(), "resistance": set()}
        for tf in inp.timeframes.values():
            for kind in levels:
                levels[kind].update((tf.get("levels") or {}).get(kind, []))
            for k in ("upper", "lower", "mid"):
                v = (tf.get("bollinger") or {}).get(k)
                if v:
                    levels["support" if v < inp.last_price else "resistance"].add(v)
            for k in ("ema20", "ema50", "ema200"):
                v = (tf.get("ema") or {}).get(k)
                if v:
                    levels["support" if v < inp.last_price else "resistance"].add(v)

        def snap(value: float | None, pool: set[float]) -> float | None:
            if value is None or not pool:
                return None
            nearest = min(pool, key=lambda p: abs(p - value))
            return nearest if abs(nearest - value) / value <= 0.01 else None  # must correspond to a computed level

        out.key_support = snap(out.key_support, levels["support"])
        out.key_resistance = snap(out.key_resistance, levels["resistance"])
        return out


class AnalystAgent(Agent[AnalystInput, AnalystOutput]):
    name, title = "analyst", "Analyst"
    role = "Synthesises scout, news and technical findings into a structured trade proposal (or no trade)."
    tools = ["agent_reports", "market_reference"]
    input_model, output_model = AnalystInput, AnalystOutput
    system_prompt = """
You are ANALYST, the decision synthesiser of a spot crypto trading desk (long-only, no leverage, no shorting).
You receive the Scout's thesis, the News & Sentiment report, the Technical Analyst's report, reference market data
(last price, ATR, support/resistance, spread) and hard constraints.

Your job:
1. Weigh the evidence. Propose a trade only when the technical picture supports it and news does not contradict it.
   If evidence is weak, mixed or missing, answer NO_TRADE. Most scans should end in NO_TRADE.
2. For BUY: entry_price must be at the current last price (orders execute at market); stop_loss below entry, placed
   beyond a real support level or about 1.5-2.5x ATR away; take_profit above entry at or before a resistance level,
   giving at least the minimum risk/reward in `constraints`.
3. SELL is only valid when `open_position` is present and you recommend closing that position. For SELL set
   entry_price to the last price, stop_loss above it and take_profit below it.
4. Give concrete supporting evidence that cites the reports (indicator readings, news item ids, scanner signals),
   an expected holding period, and an honest confidence.
When action is NO_TRADE, `proposal` must be null.
"""

    def post_validate(self, inp: AnalystInput, out: AnalystOutput) -> AnalystOutput:
        if out.action == "NO_TRADE":
            out.proposal = None
            return out
        p = out.proposal
        if p is None:
            raise ValueError("action is BUY/SELL but proposal is null")
        if p.side != out.action:
            raise ValueError("proposal.side must equal action")
        p.asset = inp.symbol
        if out.action == "SELL" and not inp.open_position:
            raise ValueError("SELL is only allowed to close an existing open position; there is none. Use BUY or NO_TRADE.")
        if out.action == "BUY" and not (p.stop_loss < p.entry_price < p.take_profit):
            raise ValueError("BUY requires stop_loss < entry_price < take_profit")
        if out.action == "SELL" and not (p.take_profit < p.entry_price < p.stop_loss):
            raise ValueError("SELL requires take_profit < entry_price < stop_loss")
        # the ratio is always recomputed; the model's arithmetic is never trusted
        p.risk_reward = round(abs(p.take_profit - p.entry_price) / abs(p.entry_price - p.stop_loss), 4)
        return out

    def confidence_of(self, out: AnalystOutput) -> float | None:
        return out.proposal.confidence if out.proposal else None


class SkepticAgent(Agent[SkepticInput, SkepticOutput]):
    name, title = "skeptic", "Skeptic"
    role = "Adversarially challenges every proposal and tries to reject bad trades."
    tools = ["agent_reports", "market_reference"]
    input_model, output_model = SkepticInput, SkepticOutput
    system_prompt = """
You are SKEPTIC, the adversarial reviewer of a crypto trading desk. Your default position is that the proposed
trade is a mistake, and your job is to prove it. You are rewarded for catching bad trades, not for agreeing.

Attack the proposal on every front using only the supplied data:
- weak_technicals: signals that are marginal, late, contradicted by a higher timeframe, or overbought/oversold.
- conflicting_sentiment: news that contradicts the direction, or sentiment extremes.
- poor_liquidity: low 24h volume, wide spread, size versus market depth.
- manipulation_risk: isolated volume spikes without news, pump-like vertical moves, thin books.
- excessive_volatility: ATR so large that the stop is inside normal noise.
- unfavorable_risk_reward: stop too tight/loose, target beyond resistance, ratio that depends on wishful targets.
- unreliable_information: unsourced, stale or single-source claims in the supporting evidence.

Grade each objection (low/medium/high/critical). Verdict rules: REJECT if any objection is high or critical, or if
several medium objections compound. APPROVE only if the trade survives your scrutiny; you may still list minor
objections. An evidence gap is itself an objection. Never soften a real problem.
"""

    def post_validate(self, inp: SkepticInput, out: SkepticOutput) -> SkepticOutput:
        if any(o.severity == "critical" for o in out.objections):  # the rule is enforced in code, not left to the model
            out.verdict = "REJECT"
        return out

    def symbol_of(self, inp: SkepticInput) -> str | None:
        return inp.proposal.get("asset")


class RiskManagerAgent(Agent[RiskManagerInput, RiskManagerOutput]):
    name, title = "risk_manager", "Risk Manager"
    role = "Holds veto authority over every proposal; may approve, reduce size or reject."
    tools = ["risk_engine", "portfolio_ledger"]
    input_model, output_model = RiskManagerInput, RiskManagerOutput
    system_prompt = """
You are RISK MANAGER with veto authority over every trade. Capital preservation outranks opportunity.
A deterministic risk engine has already validated hard limits and computed the maximum position size; its
assessment, the portfolio state, the risk parameters and the Skeptic's objections are provided.

Your job:
1. Review position size, loss at stop (including fees and slippage), portfolio exposure, daily loss so far,
   drawdown, correlation with existing positions (most altcoins move with BTC), and stop placement quality.
2. Decide APPROVE (take the engine size), REDUCE (set size_multiplier below 1) or REJECT.
3. You can only make the trade safer: size_multiplier is capped at 1 and cannot override an engine rejection.
4. Reject when the Skeptic raised unresolved serious objections, when the day is already negative and the setup is
   marginal, or when the stop sits inside normal volatility. Explain concerns concretely.
"""

    def skip(self, inp: RiskManagerInput) -> tuple[RiskManagerOutput, str] | None:
        det = inp.deterministic_assessment
        if not det.get("approved"):  # the engine's rejection is final; spending an LLM call on it is pointless
            return RiskManagerOutput(decision="REJECT", size_multiplier=0.0, concerns=list(det.get("reasons") or []), confidence=1.0,
                                     reasoning="Deterministic risk engine rejected the trade; veto is automatic."), "deterministic risk engine veto"
        if inp.proposal.get("side") == "SELL":
            return RiskManagerOutput(decision="APPROVE", size_multiplier=1.0, concerns=[], confidence=1.0,
                                     reasoning="Closing an existing position reduces risk; approved by rule."), "exit order: no LLM review needed"
        return None

    def post_validate(self, inp: RiskManagerInput, out: RiskManagerOutput) -> RiskManagerOutput:
        if out.decision == "APPROVE":
            out.size_multiplier = 1.0
        elif out.decision == "REJECT":
            out.size_multiplier = 0.0
        elif out.size_multiplier <= 0.0:
            out.decision = "REJECT"
        elif out.size_multiplier >= 1.0:
            out.decision = "APPROVE"
        return out

    def symbol_of(self, inp: RiskManagerInput) -> str | None:
        return inp.proposal.get("asset")


AGENTS: dict[str, Agent] = {a.name: a for a in (ScoutAgent(), NewsAgent(), TechnicalAgent(), AnalystAgent(), SkepticAgent(), RiskManagerAgent())}
