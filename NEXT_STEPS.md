# Lucky Bingo — Next Steps

The application features are ready for continued testing. Adding the third
administrator and turning on real-money mode are important final configuration
steps, but they should not be done until the safety checks below are complete.

## 1. Finish testing first

- Start the testing system with `./start-testing.sh`.
- Test the 2 birr, 5 birr, and 10 birr rooms.
- Test buying one to five cartelas.
- Test valid and invalid Bingo claims.
- Test one to four simultaneous winners and equal prize division.
- Test the five-or-more-winners dismissal and stake refunds.
- Test deposits, withdrawals, disputes, and admin evidence.
- Test from several real Telegram accounts and different mobile networks.

Keep these settings during this stage:

```dotenv
ENABLE_REAL_MONEY=false
TEST_SINGLE_PLAYER_START=true
```

Test mode does not debit cartela prices or pay winnings.

## 2. Add the third administrator

Ask the third administrator to send `/myid` to the bot. Put all three numeric
Telegram IDs in `.env`, separated by commas:

```dotenv
ADMIN_TELEGRAM_IDS=FIRST_ADMIN_ID,SECOND_ADMIN_ID,THIRD_ADMIN_ID
```

Restart Lucky and ask each administrator to send `/admin`. Confirm that all three
can open the protected admin board. Never give admin access using a username;
Telegram numeric IDs are required.

## 3. Complete the payment settings

Verify these values in `.env`:

```dotenv
TELEBIRR_ACCOUNT=your_verified_telebirr_account
CBE_BIRR_ACCOUNT=your_verified_cbe_account
PAYMENT_ACCOUNT_NAME=the_exact_account_holder_name
MINIMUM_DEPOSIT_BIRR=10
MINIMUM_WITHDRAWAL_BIRR=100
DEFAULT_TRANSFER_COST_BIRR=your_confirmed_transfer_cost
```

The three administrators should follow the same written process for checking a
deposit transaction ID in the real banking application. A submitted reference
number alone is not proof that money was received.

## 4. Use permanent hosting

Do not use a `trycloudflare.com` Quick Tunnel for real customers. It is temporary,
changes after restart, and runs only while the WSL computer and terminal remain
open.

Before launch, deploy the web server, Telegram bot, and database to an always-on
server with:

- a stable HTTPS domain;
- automatic process restart;
- restricted server access;
- database backups stored in a separate location;
- monitoring for downtime and application errors.

Set `PUBLIC_URL` to the permanent HTTPS address and configure the same address in
BotFather. Test `/start` and `/admin` again after deployment.

## 5. Complete security checks

Before real money is enabled:

- replace `APP_SECRET` and `ADMIN_KEY` with separate, long random secrets;
- keep `.env`, the bot token, bank details, and database private;
- keep `ALLOW_DEV_AUTH=false` on the public server;
- restrict database and server access to trusted operators;
- verify that backup restoration works;
- decide how administrators will respond if a phone or Telegram account is lost;
- rotate the bot token immediately if it is ever exposed.

Recommended production settings:

```dotenv
ALLOW_DEV_AUTH=false
TEST_SINGLE_PLAYER_START=false
```

With `TEST_SINGLE_PLAYER_START=false`, a round needs at least five different
Telegram players. Five cartelas from one player do not satisfy this requirement.

## 6. Complete legal and provider approval

Before accepting real stakes, obtain professional advice for every place where
the game will be available. Confirm:

- that operating paid Bingo is licensed and legal;
- minimum player age and identity-verification requirements;
- tax, accounting, record-retention, and reporting requirements;
- responsible-gambling controls, limits, and self-exclusion rules;
- Telebirr, CBE, Telegram, hosting-provider, and app-platform rules;
- a written privacy policy, game rules, dispute policy, and withdrawal policy.

Do not launch if any required approval is missing.

## 7. Perform a controlled money test

After steps 1–6 are complete:

1. Back up the database.
2. Stop the application.
3. Change the production `.env` to:

   ```dotenv
   ENABLE_REAL_MONEY=true
   TEST_SINGLE_PLAYER_START=false
   ALLOW_DEV_AUTH=false
   ```

4. Restart the application.
5. Use small amounts with the three administrators only.
6. Confirm that purchasing cartelas immediately reduces available balance.
7. Confirm that settlement credits the correct winner amount after the 5%
   commission and configured transfer cost.
8. Confirm that a dismissed round refunds every stake.
9. Confirm that disputed results freeze payment.
10. Confirm that withdrawal requests reserve balance and require admin approval.

If any result is wrong, turn `ENABLE_REAL_MONEY` back to `false`, restart the
application, preserve the round evidence, and investigate before continuing.

## 8. Launch gradually

- Begin with a small invited group and low transaction limits.
- Have all three administrators available during the first games.
- Review balances, wallet entries, deposits, withdrawals, and round evidence each
  day.
- Keep a documented support and dispute process.
- Increase player access only after several successful reconciliations between the
  application records and the actual bank accounts.

## Final launch checklist

- [ ] Full multiplayer and settlement testing passed
- [ ] Three administrator Telegram IDs configured and verified
- [ ] Payment accounts and transfer cost confirmed
- [ ] Permanent HTTPS hosting and domain working
- [ ] Database backups and monitoring working
- [ ] Production secrets replaced and protected
- [ ] Development authentication disabled
- [ ] Single-player test mode disabled
- [ ] Legal, licensing, age, tax, provider, and policy requirements approved
- [ ] Controlled real-money test reconciled correctly
- [ ] `ENABLE_REAL_MONEY=true` enabled only on the production server

