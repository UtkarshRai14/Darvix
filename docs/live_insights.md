# Live insights and nudges from call audio (Part 4)

Code: `app/live/` · Dashboard: `/insights.html` · Test calls: `live_scenarios/*.json` · Report: `python -m app.live.evaluate`

## Streaming / simulation method

- Calls are **dual-channel** WAVs: agent on the left, customer on the right. Contact-centre platforms record this
  way, and it gives exact agent/customer separation without diarization.
- The four test calls are synthesised by `python -m app.live.build_scenarios` from scripted dialogues with native
  neural voices. The noisy call mixes street rumble, a 100 Hz hum, horn bursts and background chatter at 6 dB SNR
  onto the customer line.
- Calls recorded by the Part 1 voice agent (also stereo) can be replayed too, which reuses the Part 1 recordings.
- `LiveSession.run()` replays the file at **real-time speed in 250 ms chunks**, sleeping until each chunk's wall-clock
  time. The rest of the pipeline only sees chunks as they "arrive", which is the same code path a live media stream
  would feed. The dashboard plays the same audio in sync, so you hear the call while nudges appear.

## Pipeline

```
250 ms chunk ─► per-channel Segmenter (20 ms frames, rolling 15th-percentile noise floor + 8 dB,
                 550 ms hang-over end-pointing, 12 s max)
           ─► Whisper per segment (agent: large-v3-turbo, customer: large-v3; 4 concurrent)
           ─► confidence filter (empty / known hallucination / no_speech_prob > 0.5 / avg_logprob < -1.0)
           ─► agent text:    ComplianceMonitor (rules, ~1 ms) + nudge resolution / missed-opportunity check
              customer text: gpt-oss-20b signal extractor (strict JSON) + FrustrationTrend
           ─► NudgeController ─► WebSocket ─► dashboard renders ─► ack ─► delivery latency
```

Each channel uses a different Whisper model, so each draws on its own free-tier rate-limit bucket (20 req/min each).

## Signal design

| Signal | Source | Logic |
|---|---|---|
| Recording disclosure missing | rule (agent) | No "call … recorded" phrase in the agent's opening turn: fires when the 2nd agent turn starts, or at 30 s. Shown on the compliance checklist. |
| Waiting periods not disclosed before quote/close | rule (agent, sales calls) | The agent mentions a price/premium/payment link while the "waiting period" item is still pending. A negated mention ("there is **no** waiting period") does not count as a disclosure. |
| Risky statement | rule (agent) | Overstated coverage ("everything is covered from day one", "no waiting period"), guaranteed claims ("definitely be approved", "100% guaranteed"), "risk-free". Evidence = the sentence. |
| Cross-sell opportunity | LLM (customer) | Customer mentions people/needs outside the current discussion (parents, spouse, children, a baby). Grouped by topic (`parents`/`children`/`spouse`) and mapped to an approved offer (Senior Care, Family Floater). |
| Missed opportunity | rule over the cross-sell | The agent spends 2 turns without mentioning the cross-sell topic → one higher-priority repeat. |
| Buying signal | LLM | Asks how to buy/apply/pay, or agrees to proceed. |
| Payment difficulty | LLM | Cannot afford or pay now (job loss, money tight) → approved support options only. |
| Callback need | LLM | Asks to be called back / cannot talk now. |
| Rising frustration | LLM score → trend | Per-utterance frustration 0-1 → signal only when ≥ 0.85, or ≥ 0.6 and not lower than a previous score ≥ 0.4 (a trend, not one sharp word). Plotted live. |
| Topic / intent shift | LLM | Topic per customer utterance (pricing, coverage, claims, eligibility, family, payment, complaint…); a change is shown as an insight, not a nudge. |

Compliance is rule-based on purpose: it must be auditable, instant and identical every time. The LLM is used
where judgement is needed (customer intent and emotion), and it is told to return nothing for short, vague or
noisy utterances. Nudge **text comes from an approved playbook template**, not free LLM generation, so a
nudge can never invent an offer.

## Nudge control (`nudges.py`)

Applied in order to every signal. Suppressed signals and their reason are shown in the dashboard's
"Signals & suppression" log and counted in the report.

1. **Confidence threshold:** 0.65.
2. **Duplicate suppression by topic key:** e.g. `cross_sell:parents`, `risky:guaranteed_claim`. One nudge per topic per call.
3. **Per-type cooldown:** frustration 60 s, payment difficulty 90 s, cross-sell 30 s, buying/callback 60 s.
4. **Global spacing:** at most one non-critical nudge per 8 s.
5. **Cap of 3 active nudges:** a more important nudge replaces the least important one. Critical compliance nudges are never dropped.
6. **Priorities:** P1 compliance/risky, P2 frustration/payment/missed opportunity, P3 cross-sell/buying/callback.
7. **Expiry:** 40-60 s depending on type, shown as a draining bar.
8. **Resolution:** a nudge is marked *addressed* when the agent's speech matches its resolve pattern (e.g. an apology after a frustration nudge, "subject to waiting periods" after a risky statement).
9. **Repetition rule:** an unaddressed cross-sell is repeated once as a "missed opportunity".

## Latency measurement

Every nudge carries timings measured on the server clock from the moment the evidence speech **ended**:

| Component | Meaning |
|---|---|
| `endpoint_ms` | speech end → segment closed (the 550 ms hang-over plus chunk granularity) |
| `asr_queue_ms` | waiting for an ASR slot or rate-limit capacity |
| `asr_ms` | Whisper request time; transcription latency is also reported **per chunk** for every segment, with real-time factor |
| `rules_ms`, `llm_ms`, `nudge_ms` | signal extraction (rules), signal extraction (LLM), nudge control |
| `e2e_emit_ms` | speech end → nudge sent on the WebSocket |
| `delivery_ms` | send → dashboard rendered and acknowledged (round trip) |
| `e2e_display_ms` | speech end → acknowledged as displayed |

`python -m app.live.evaluate` replays all four scenarios at real-time speed and writes P50/P95 per component to
`reports/live_eval.md`. Delivery and display rows need a browser, so they come from dashboard sessions, which are
saved to `reports/live_sessions/` and merged into the report with `--report`.

Measured numbers require a Groq key and are produced by the command above; none are quoted here. The design
budget is end-pointing (~0.6 s) + ASR + LLM. In practice that should give nudges a few seconds after the
customer stops speaking, with free-tier rate-limit waits as the main variable. The report shows the actual
P50/P95, including the queue time.

## Test coverage and false-positive analysis

| Scenario | Required coverage | Ground truth |
|---|---|---|
| `missed_cross_sell` | missed cross-sell opportunity | cross-sell (turn 5), missed opportunity (turn 8), buying intent (turn 11); optional buying intent at turn 13. The agent's disclosures are correct, so **no compliance nudge expected**. |
| `compliance_risk` | skipped disclosure + risky statements | recording disclosure missing, 2 risky statements, waiting periods not disclosed before quote; optional buying intent / callback |
| `rising_frustration` | rising frustration | frustration (turn 7; turn 5 optional), payment difficulty, callback request |
| `noisy_ambiguous` | noisy, ambiguous call | **nothing**: a compliant agent and a non-committal customer on a noisy line |

Scoring (`LiveSession.evaluate`):
- A nudge is a **TP** if its type matches an expected nudge and it is emitted between 1 s before that turn starts and 20 s after it ends.
- Unmatched nudges are **FP**; unmatched required expectations are **FN**.
- "Optional" expectations are acceptable but not required, so they count as neither.

The report lists precision and recall per scenario, every FP/FN with its evidence, raw signals vs delivered nudges,
suppression counts by reason, and how many low-confidence segments were filtered.

The plumbing (end-pointing, rules, controller, scoring) was checked offline with mocked ASR/LLM: all four
scenarios matched their ground truth with mocked inputs. That check found and fixed three rule bugs:
- the negated "no waiting period" counted as a disclosure;
- the same phrase resolved the risky-statement nudge;
- critical nudges were capped away.

Real-model precision and recall come from `python -m app.live.evaluate`.

## Limitations at 10x scale

- **Rate limits:** free-tier Whisper allows 20 requests/min per model and the LLM 8K tokens/min. One call already
  uses a meaningful share, so 10 concurrent calls would queue (visible as `asr_queue_ms`). Fixes: paid tier,
  self-hosted streaming ASR (faster-whisper on GPU, ~1 GPU per few dozen concurrent streams), and running the
  LLM only on customer turns (already the case) with batching across calls.
- **Process model:** sessions live in one process's memory. At scale: one worker per N calls, sharding by call ID,
  session state and nudge history in Redis, a message bus (e.g. NATS/Kafka) between ASR, signal and delivery
  stages with back-pressure, and a WebSocket gateway for fan-out to supervisors.
- **Cost:** segment-level Whisper is billed at a 10 s minimum per request on Groq, so short segments are
  inefficient. A streaming ASR with partial results is cheaper and faster.

## Limitations with noisy audio

- The energy VAD adapts to steady noise but opens segments on horns and background speech, and it fragments real
  speech when SNR is low. That means more ASR calls and more partial utterances.
- Mitigations implemented:
  - rolling-percentile noise floor;
  - ASR confidence filter and hallucination blocklist;
  - at least 3 words before calling the LLM;
  - an LLM told to stay silent on vague or noisy text;
  - confidence threshold, de-duplication and spacing.
- Not implemented: neural VAD (e.g. Silero), noise suppression before ASR, and mono-call diarization. Background
  speech that Whisper transcribes confidently can still reach the LLM; the `noisy_ambiguous` scenario measures
  how often that produces a nudge.
