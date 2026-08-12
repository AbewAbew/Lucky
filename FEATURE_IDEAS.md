# Lucky Bingo — Feature Ideas

Ideas discussed for future development. Nothing here is built yet; this is a
backlog to revisit, not a commitment or a plan.

## Quick wins (reuse existing data/infrastructure)

- **Referral payout.** `users.referred_by` and `/invite` already track who
  referred whom, but nothing pays out on it. Add a reward (e.g. 5 birr) when
  a referred player makes their first approved deposit, using the same
  `wallet_entries` ledger pattern as the signup bonus.
- **Personal stats / history page.** Round evidence and the leaderboard
  already exist for admins and global ranking, but a player can't see their
  own "games played / won / total winnings" anywhere. Same underlying data,
  new player-facing view.
- **Daily login bonus / streak.** Same mechanism as the signup bonus
  (`wallet_entries` kind `bonus`, non-withdrawable), but recurring and
  escalating (small on day 1, bigger by day 7). Cheap retention lever.
- **Achievements / badges.** Cosmetic layer on data already collected
  ("first win," "10 games played," etc.) — gives players something to chase
  besides money.

## Bigger engagement plays

- **Progressive jackpot.** Carve a small slice of the 5% commission (e.g.
  1 point) into a separate pot that grows across rounds and only pays out on
  a rare pattern — e.g. bingo landing on call #4 or #5, which the game
  already records as `winning_sequence` for every round, so detecting it is
  nearly free. Pot resets to a seed amount after paying out. Show it live in
  the lobby ("🎰 Jackpot: 340 birr") as a marketing hook, refreshed the same
  way the admin revenue dashboard already refreshes.

  Open decisions before building:
  - **Global pot** (one pot across all three tiers, bigger number, uneven
    funding across stake sizes) vs. **per-tier pots** (fairer, grows slower).
  - **Trigger threshold** — call #4 only (rarer, more exciting, may go a
    long time unpaid) vs. #4–6 (hits more often, keeps it feeling alive).
  - **Contribution rate** from commission (1% conservative vs. higher for
    faster growth, at the cost of more baseline revenue).

- **Scheduled special-event rooms.** A recurring "Friday Night Big Game"
  with a bigger stake or better payout ratio at a fixed time. Creates
  appointment behavior and helps solve the 5-distinct-players cold-start
  problem by concentrating players into one time slot.
- **Voice/sound number calls.** Real Ethiopian bingo halls call numbers out
  loud ("B-4!"); a synthesized or pre-recorded voice call alongside the
  visual call would feel more authentic. Doable client-side in the Mini App.
- **Proactive "room is starting" bot push.** The bot currently only messages
  players already mid-flow. Pinging someone who joined a room that's about
  to hit its 5-player threshold could pull lapsed players back in.

## Bigger, more judgment-heavy

- **Amharic language toggle.** Already flagged as a known gap in the
  README's production-readiness notes ("translated copy"). Given the
  market, may matter more than any single feature above.
- **VIP / loyalty tiers.** Reduced commission or priority support for
  high-volume players — a standard retention lever in real-money gaming.
- **In-room chat or quick-reaction emojis.** A lighter social layer during
  a live round; Telegram already provides the chat surface, so this is
  about in-app reactions, not building a chat system from scratch.

## Recommended starting point

Referral payout and the personal stats page: both reuse data and
infrastructure that already exists, and are low-risk. The jackpot is the
best bet for a genuine growth/engagement lift if there's appetite for
something bigger.
