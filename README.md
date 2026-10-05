# AI Agent Operations — field notes

Notes from running a personal AI agent system in production, written by its operator. Not tutorials:
each case is something that broke, the fix, and the measured result.

The agent runs 24/7 on a home Debian box: it schedules its own work, talks to a chat app, classifies
data, and is expected to keep working when a provider dies or a free quota runs out.

## 1. A decision layer with five fallbacks

**Problem.** The agent uses decision models: instead of generating text, they take a state plus typed
questions and return the chosen option with a probability. Fast and cheap, but everything depended on
one provider. Quota exhausted or service down meant the whole pipeline stopped.

**What I built.** A five-level chain that degrades on its own:

1. Primary model.
2. Same provider, different machine (one host can fail while the other serves).
3. Smaller model of the same provider, for when an answer matters more than nuance.
4. Local model on the same box — no network, no quota.
5. A paid API, only when explicitly authorised for that single call.

Two mechanisms do the real work: a **per-level circuit breaker** (two consecutive failures park a
level for 15 minutes; a quota or credential error parks it for 6 hours, while the other levels keep
serving) and a **local quota counter** with alerts at 65 %, 80 % and 95 %, because the provider
exposes no usage endpoint.

**Measured.** 60 decision calls, three questions each:

- 0 failures.
- 0.66 s median, against 2.40 s for a chat model on the same tasks.
- 1.04 s at p95, against 10.24 s for the chat model — an order of magnitude better exactly where a
  pipeline decides whether to wait or to drop the task.
- 25,047 of 100 million free tokens consumed (≈ 240,000 calls available).
- Local fallback: 2.5 s for three questions, no network.

**What it taught me.**

- A 0.8B local model cannot fill a multi-question JSON. It answered one key, with the wrong name.
  One question per call and a tolerant value matcher works.
- Local reasoning models will "think" for over four minutes unless reasoning is explicitly disabled.
  A slow fallback is not a fallback.
- Network errors and quota errors are not the same event: retry the first, park the second for hours.
- Degradation has to be tested. Breaking the credential on purpose was the only thing that proved the
  chain worked.

## 2. Where the agent's money was going

**Problem.** The agent bills tokens. The intuition says the cost lives in which model you pick.

**What I did.** Pulled per-session billing out of the agent's own database (input, output, cache-read,
message and tool-call counts) and grouped it by source: chat, scheduled jobs, subagents.

**Measured.**

- 245M cache-read tokens in a month against 6.3M fresh input tokens: re-sent history was 97 % of the
  volume.
- 5–6 sessions held the bulk of it. One long thread alone accumulated 372k input tokens and 54M
  cache-read across 200+ messages. On a long thread, every new message costs hundreds of thousands of
  input tokens.
- Two scheduled jobs accounted for ~4.3M input tokens a month (~90 % of scheduled spend). They were
  wrapping scripts that already did the work. Converted to script-only jobs, their token cost is zero.

**What it taught me.** The cost is not in the model, it is in how many times the context is re-read.
Closing a thread per task saves more than switching models, and anything a script can do without
interpretation does not need a model at all.

## 3. The scraper that returned zero and was not "empty"

**Problem.** A marketplace scraper that had worked for months started returning **0 results**. No
error, no exception. "Nothing to report" and "I am broken" looked exactly the same.

**What I did.** Diagnosed it in two layers: a WAF was serving a 923-byte error page to any client
identifying itself as headless, and the listing markup had changed from links with titles to cards
whose title and price live in nested attributes. Replaced the user agent with a realistic one and
rewrote the parser to extract by structure, including the Spanish thousands separator ("33.900 €" is
33900, not 33.9).

**What it taught me.** A scraper returning zero is broken, not empty. Instrument it to tell the
difference: response size, expected markers, minimum result count. A silent zero costs more than an
exception.

## Repository layout

```
cases/     long-form notes for each case (Spanish originals kept internally)
code/      sanitised scripts from the system: decision chain, command guard, cost audit
```

## About

Operator profile: agent orchestration in production, fallbacks and failure modes, cost control,
pre-execution guardrails. Python, Linux, chat-platform bots, scheduled automation. Languages: Spanish
and Catalan native, English C1, German B2.
