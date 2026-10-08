# Chotu Line

### Instinct-ize your Claude Code or Codex. No new subscription.

Text the AI agent you already pay for on Telegram, and it runs your errands
on your own computer. It books, buys, cancels, fills forms, follows up and
reminds you, using your own browser and your own logins.

- **No new subscription.** It uses the Claude Code or Codex plan you already
  have. There's no API key, no server, and no monthly fee.
- **Your computer, your data.** It works in your own logged-in Chrome. What it
  learns about you stays on your machine.
- **It asks before it spends.** Purchases, bookings and messages to anyone
  else need a tap on your phone. On payment pages the software enforces this,
  not the AI's judgement.
- **Open source.** Apache-2.0. Read it, fork it, run it.

|  | Hosted assistants (Instinct and similar) | Chotu Line |
|---|---|---|
| Cost | A separate service to join (Instinct is invite-only today) | $0 on top of Claude Code or Codex |
| Where the agent works | Their cloud, with access to your accounts | Your computer, your own browser |
| Your data | On their servers | On your computer; passwords in your OS keychain |
| Approvals | Their policy | Your phone; payment pages gated in code |
| Code | Closed | Open source |

<sub>Not affiliated with Instinct or Spear Street Technology. "Instinct" is used
only to describe the kind of product this is.</sub>

An example conversation:

```
you      › find 2 seats for Dune tonight near Koramangala, aisle, under ₹800
chotu    › PVR Nexus, 9:40pm, 2 aisle seats (F11–F12), ₹740 total. book it?
           [Allow] [Deny]
you      › (taps Allow)
chotu    › booked. the confirmation and tickets are in your email.
```

## Install (macOS; Linux in beta; 5 minutes)

You need Python 3.11+, Google Chrome, Node.js 18+ (for browser control), and
one brain: [Claude Code](https://claude.com/claude-code) signed in, or
[Codex](https://github.com/openai/codex) signed in.

```bash
uv tool install git+https://github.com/Ashish-dwi99/chotu-line
# or: pipx install git+https://github.com/Ashish-dwi99/chotu-line

chotu-line setup              # pick the brain, paste a Telegram bot token
chotu-line service install    # runs in the background and starts at login
```

`setup` prints a link. Open it on your phone to pair: only your Telegram
account can talk to your line.

**Getting a Telegram bot token:** in Telegram, open
[@BotFather](https://t.me/BotFather), send `/newbot`, pick a name, and copy
the token it gives you.

**Let it use your accounts:** run `chotu-line browser` and sign in to the
sites you want it to use (Amazon, Swiggy, BookMyShow…) in the Chrome window
that opens. Those sessions persist.

Something not working? `chotu-line doctor` checks everything and tells you
the fix.

## What it does

- **Errands on real websites**, using your logged-in browser.
- **Asks before it spends.** Purchases, bookings, cancellations, and messages
  to anyone else need your tap first.
- **Questions when stuck**, such as an OTP or a choice only you can make.
- **Remembers you**: your name, address, and preferences go into forms
  automatically.
- **Workstreams:** a trip or a renewal keeps its decisions and next step
  across conversations.
- **Reminders and monitors**, for example "check the price every morning".
  A monitor with nothing new stays quiet.
- **Saved logins** live in your OS keychain
  (`chotu-line vault add --label GitHub --origin https://github.com`). The
  agent fills them in without ever seeing the password, and only on that
  exact site.

Phone commands: `/stop`, `/new`, `/brain claude|codex|chotu`, `/status`,
`/forget`.

## Brains

| Brain | Status | What you need |
|---|---|---|
| Claude Code | **Stable**, tested end to end | Claude Pro or Max, `claude` signed in |
| Codex | Beta | ChatGPT plan, `codex` signed in |
| [Chotu](https://www.sankhyaailabs.com) | Beta | The Chotu app with a Chotu plan, or your own OpenRouter key |

**No Claude or Codex subscription?** Use the
[Chotu app](https://www.sankhyaailabs.com) as the brain. It also handles
voice, your screen, and native Mac apps.

## Safety, enforced in code

With Claude Code as the brain:
- Clicking or typing on a page that asks for card details needs your
  approval, unless you approved that purchase in the last 20 minutes.
- Browser tools that run page code, upload files, or read raw network traffic
  ask every time.
- Shell commands other than single read-only ones ask every time.

Card details are never stored. Payments use the method already saved on the
merchant's site, after you approve. See [SECURITY.md](SECURITY.md).

## Privacy

Everything the line learns about you stays on your computer, in
`~/.chotu-line`, and saved passwords stay in your OS keychain. Telegram chats
with bots are **not end-to-end encrypted**, so Telegram can read your
messages; never send passwords or card numbers there. `/forget` erases what
the line knows about you.

## Limits

- Your computer must be awake.
- Card fields inside a payment provider's frame aren't filled. You pay, or
  the site uses your saved card.
- Some sites block automated browsers. The line uses a real Chrome window,
  which gets through most of them; Google Search sometimes still shows a
  CAPTCHA.
- Windows isn't supported yet.

## How well does it work?

We ran 30 real [WebVoyager](https://github.com/MinorJerry/WebVoyager)
errands (Amazon, Booking, Google Flights, GitHub, Apple…) through Claude
Sonnet with a real Chrome, the setup this line uses. It completed 26 of 30,
with a median of 38 seconds per task. Each run was judged with WebVoyager's
own prompt, using Claude as the judge. With only 30 tasks, treat this as a
rough guide, not a leaderboard score.

## License

Apache-2.0. Made by Sankhya AI Labs, the team behind
[Chotu](https://www.sankhyaailabs.com).
