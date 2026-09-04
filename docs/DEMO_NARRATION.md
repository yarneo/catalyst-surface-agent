# Demo narration — natural recording script

Read this at a comfortable pace. Pause for two or three seconds between
sections. The finished video will be retimed around the recording, so there is
no need to chase a stopwatch.

## 1 · The hook

“This is Catalyst Surface Agent, built by Yar and Starboi. It finds scheduled
market events and trades them autonomously.

Before Broadcom reported earnings, our engine identified the setup as likely to
produce a large move. Broadcom then fell more than six percent by our exit.

So the interesting question is this: why did a correct signal still produce a
losing options trade?”

## 2 · Finding the opportunity

“This isn’t an AVGO-only bot.

At the start of the week it searched 64 liquid stocks. It studied their option
markets and looked for events that could produce an unusual move.

That list went from 64 stocks to 31 usable surfaces. Then nine possible events.
Then six confirmed candidates.

Broadcom was the only trade that survived every test. In another week the answer
could be a different stock. It could also be no trade at all.”

## 3 · What the model does

“Featherless runs several models at the same time. They interpret the news and
decide whether the event still looks intact.

The committee can catch leaked results or changed guidance. It can also describe
the likely direction and strength of the surprise.

But the AI has clear limits. It can stop a trade. It cannot invent one. It cannot
increase our position or ignore a risk rule.”

## 4 · From signal to trade

“Alpaca MCP connects the rest of the process.

It gives the agent market data, option history, the market clock, and the real
account state. When a trade qualifies, it handles the orders. The agent checks
the broker position and follows it through the exit.

So this isn’t an AI producing a recommendation. It’s a full autonomous loop.
The agent discovers, decides, trades, checks its work, and records the result.”

## 5 · What actually happened

“Every live condition passed on Wednesday afternoon.

The agent bought 13 Broadcom straddles at the 367-dollar-and-50-cent strike. The
position cost 38,480 dollars, or about 38 percent of the account.

The structure was direction-neutral. It could benefit from a sharp move either
way. The entry-time Featherless committee didn’t force a directional bet. It
kept the focus on whether the event was still intact.”

## 6 · The move thesis was right

“Broadcom was trading at 368 dollars and 56 cents when we entered.

By our exit it was around 345 dollars and 82 cents. That’s a 6.17 percent event
move.

The engine was right to expect unusual movement. The problem was the price of
the options. The straddle cost 8.04 percent of the stock price. A six-percent move was large, but
it wasn’t enough to repay that premium after implied volatility collapsed.”

## 7 · Why the exit helped

“The exit timing mattered too.

The position looked much worse shortly after the market opened. It recovered
into our planned 9:45 exit, and the order filled 18 seconds later.

After we sold, Broadcom reversed more than 11 dollars toward our strike. It
finished the day near 357. That cut its distance from our strike roughly in
half. With only one day left before expiration, the fixed exit captured more of
the event move before the reversal and time decay took over.

The fixed exit kept one bad trade from becoming a larger one.”

## 8 · What changes next

“The next version won’t simply use a lower price limit.

It will ask a better question: does the expected value of this position clearly
exceed what the options cost right now?

Position size will shrink when the evidence is uncertain. When a moderate move
is more likely, the agent can use a cheaper capped structure. And when the value
isn’t there, it stays flat.

Catalyst Surface Agent connected AI reasoning to a real autonomous trade. The
engine found the move. The execution captured it. Now the pricing model gets
sharper.”

## Recording notes

- Record the eight sections separately if that feels easier.
- Leave two or three seconds of silence between sections.
- If a sentence goes wrong, pause and say the whole sentence again.
- Pronounce AVGO as “A-V-G-O” and MCP as “M-C-P.”
- Do not rush. The visuals will be fitted to the final voice track.
