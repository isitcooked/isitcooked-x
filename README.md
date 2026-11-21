# IS IT COOKED? — X Editorial Engine

Editorial infrastructure for **[@isitcooked1](https://x.com/isitcooked1)**.

## Purpose

This repository discovers selective Solana/Base momentum, applies strict quality
filters and prepares natural English X drafts. It supports the wider ecosystem:

- **X:** discovery, analysis and conversation
- **Instagram:** visual education and proof
- **Telegram:** live alerts and community
- **The Receipt:** transparent outcome tracking

The engine does **not** pretend to be a person and does not publish to X through
unofficial browser automation. Every X draft requires review until official API
access is connected.

## Operating model

GitHub Actions runs at 08:17, 12:17, 16:17 and 20:17 UTC. It:

1. reads free DexScreener data;
2. rejects weak or late-stage candidates;
3. generates one concise English draft;
4. saves `outbox/latest.json`;
5. optionally delivers the draft to a private Telegram chat;
6. keeps a bounded anti-duplication history.

No clean signal is a valid result. The brand never forces a token call merely to
fill a content slot.

## Optional private editorial delivery

Add these GitHub Actions secrets only if private Telegram delivery is desired:

- `EDITOR_TELEGRAM_TOKEN` — a private editorial bot token
- `EDITOR_CHAT_ID` — your private Telegram user/chat ID

Never use a public channel as the editorial destination. Never commit secrets.

## Manual test

Open **Actions → X Editorial Engine → Run workflow**, leave `dry_run` enabled,
and inspect the logs and `outbox/latest.json`. A dry run never sends Telegram or
changes permanent state.

## Editorial principles

- Evidence before hype.
- Publish risks alongside momentum.
- Never promise returns.
- Never accept payment to alter a score.
- Label sponsorships clearly.
- Track wins and losses publicly.
- Prefer silence over a weak signal.

Public slogan: **Signal Before Noise.**
