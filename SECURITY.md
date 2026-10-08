# Security

Gutsy runs an AI agent with access to your browser and your accounts.
Report a vulnerability privately via a GitHub security advisory on this
repository, not a public issue.

What Gutsy enforces in code, whatever the model says:
- Only the paired Telegram account is heard; everyone else is ignored.
- Saved passwords live in the OS keychain and are typed into a page only when
  the tab's exact https origin matches the saved login. The model never sees
  them.
- With Claude Code as the brain:
  - clicks and typing on a page that asks for card details need your
    approval, unless you approved that purchase in the last 20 minutes;
  - browser tools that run page code, upload files, or read raw network
    requests ask every time;
  - shell commands other than single read-only ones ask every time.
- Card details are never stored.

Known limits: Telegram bot chats are not end-to-end encrypted. With Codex or
Chotu as the brain, the payment gate relies on the assistant's instructions,
not on code.
