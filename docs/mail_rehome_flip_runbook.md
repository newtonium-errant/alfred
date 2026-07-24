# Gmail Mail Rehome — Flip Runbook (#7)

> Operational procedure for cutting `andrewnewton965@gmail.com` intake from the Railway-hosted
> n8n workflow to Alfred's native IMAP fetcher. Versioned with the code; run **on the box**
> (the Gmail app password is box-only). Nothing here touches real mail until you run it.

## 0. What this is and the one invariant

Today, one n8n workflow (**`Pb3Jh54bjYDoJgpi`** — "Email to Alfred Ingest - Gmail") does two jobs:
1. **Intake** — Gmail trigger → POST each email to Alfred's `/ingest` webhook.
2. **Filing** — categorize by sender/subject rules → apply a Gmail label (`Business/Receipts`,
   `Business/Invoices`, `Finance/Tax`, `Finance/Personal`) + archive out of INBOX.

The rehome replaces (1) with Alfred's native fetcher and (2) with an Algernon-side capability.

**The one invariant while rehoming:** the fetch loop ships **INERT** (`mail.fetch.enabled: false`).
Until the operator flips it on, the native fetcher never opens an IMAP connection and production is
byte-for-byte unchanged (the n8n webhook remains the only live intake path). The flip is a single,
reversible config change made *after* parity is proven and the filing capability (7c) has shipped.

## 1. Flip preconditions (do NOT flip until ALL are true)

- [ ] **7b parity PROVEN on-box** — the shadow parity proof (section 3) exits 0.
- [ ] **7c-i shipped** — topical classifier + vault-side `email_category` filing.
- [ ] **7c-ii shipped** — Gmail-side label re-application (`X-GM-LABELS`), so filing continuity is
      preserved (losing the `Business/*` + `Finance/*` labels would break tax-time findability).
- [ ] **App password present on the box** — `GMAIL_ANDREWNEWTON965_APP_PASSWORD` in the box `.env`.
- [ ] **Operator has read the behavior-change note** (section 5).

The parity proof (section 3) can and should be run as soon as 7b is on the box — it does not need
7c and touches no production state, so run it early to surface any real-mail divergence.

## 2. Box-time detail A — discover the "All Mail" folder name (locale-dependent)

The shadow fetch reads from Gmail's *All Mail* folder (it retains messages n8n has archived out of
INBOX, so the parity compare sees the complete set). The IMAP name of that folder is
**locale-dependent** — `[Gmail]/All Mail` on an English account, but localized otherwise. Discover
the real name before running the shadow fetch:

```bash
python3 - <<'PY'
import imaplib, os, ssl
pw = os.environ["GMAIL_ANDREWNEWTON965_APP_PASSWORD"]
c = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context())
c.login("andrewnewton965@gmail.com", pw)
for line in c.list()[1]:
    s = line.decode()
    if "All" in s or "\\All" in s:   # Gmail tags the All-Mail folder with the \All attribute
        print(s)
c.logout()
PY
```

Look for the entry carrying the `\All` attribute — that is the folder name to pass as `--folder`.
(This LIST probe is read-only; it changes nothing.)

## 3. The read-only shadow parity proof

The shadow fetch is **non-disruptive by construction** — four independent belts guarantee it cannot
alter Gmail state or the production inbox:
1. `EXAMINE` (read-only SELECT) — no server-side flag write is possible.
2. `BODY.PEEK[]` fetch (never `RFC822`) — never sets `\Seen`.
3. no `STORE` command is ever issued.
4. records are written under the gitignored `data/mail_shadow/`, never the vault inbox.

### 3a. Box-time detail B — capture the production side (transient records)

The n8n-delivered records land in `vault/inbox/email-gmail-*.md` but are **transient**: the curator
consumes and moves them into `vault/note/` within its poll interval. To compare against them, capture
a snapshot of the raw inbox records during a window. Either:

- **Snapshot on a cadence** (preferred — leaves production running):
  ```bash
  mkdir -p /tmp/mail_prod_snapshot
  # Run for a capture window (e.g. a few hours of real mail); copies each inbox record before the
  # curator moves it. Adjust the interval below the curator's poll interval.
  while true; do
    cp -n vault/inbox/email-gmail-*.md /tmp/mail_prod_snapshot/ 2>/dev/null || true
    sleep 20
  done
  ```
- **Or briefly pause the curator** during a short capture window so records accumulate in
  `vault/inbox/`, then copy them out and resume.

### 3b. Run the shadow fetch + compare

```bash
# Read-only capture of the last 7 days from All Mail into data/mail_shadow/
alfred mail fetch --shadow --lookback-days 7 --folder '<All-Mail-name-from-section-2>'

# Compare shadow vs the production snapshot, joined by Message-ID
alfred mail parity-compare \
  --production-dir /tmp/mail_prod_snapshot \
  --shadow-dir data/mail_shadow
```

### 3c. Interpreting the result

The compare proves parity **modulo the four accepted divergences** (the fetcher is the richer
superset; ratified 2026-07-23):
- **From** — fetcher emits the full raw header (display name + address); n8n reduced it to the bare
  address. The compare normalizes both to the bare address and still demands **address-equality** (a
  genuinely wrong address still fails).
- **To** — same as From (fetcher = all recipients; n8n = first bare address).
- **References** — the fetcher emits a `**References:**` line for threaded mail that n8n's POST omits.
- **Subject** — the fetcher decodes RFC2047 encoded-words (`=?UTF-8?...?=` → readable Unicode); n8n may
  pass the raw encoded header. The compare RFC2047-decodes **both** headings (idempotent — a no-op on an
  already-decoded/ASCII subject) and still demands the decoded subjects match.

Everything else must be byte-identical, so a **fifth** divergence fails the compare (fail-loud).

Two things the box run definitively resolves — the harness is robust to both by design, so neither
needs a code change:
- **Whether Gmail returns the Subject raw-encoded or pre-decoded.** The idempotent decode passes either
  way (it fixes an encoded subject and no-ops a decoded one).
- **Whether any fifth divergence appears on real mail.** The compare fails loud (exit 1 + a diff) if so,
  rather than letting it slip through — bring any such diff to the builder before flipping.

Exit codes:
- **0 — PARITY PROVEN.** Every matched pair is equivalent modulo the three accepted divergences.
- **1 — PARITY FAILED.** A real (fourth) divergence exists; the diff is printed. Do NOT flip —
  bring the diff to the builder.
- **2 — INCONCLUSIVE.** No Message-ID matched (nothing to compare). Check the date window, the
  All-Mail folder name, and that the production snapshot actually captured records.

## 4. The coordinated flip (only after section 1 preconditions all pass)

1. **Disable the n8n workflow** `Pb3Jh54bjYDoJgpi` (n8n UI → toggle off). This stops both n8n intake
   AND n8n filing. Do this first so the fetcher doesn't double-process alongside n8n.
2. **Enable the native fetch loop** — in the box `config.yaml`, set `mail.fetch.enabled: true` (the
   Gmail account already carries `fetch: true`). Restart the mail daemon (`alfred down && alfred up`,
   or restart just the mail slot).
3. **Confirm the loop started** — `grep mail.fetch.loop_started data/mail_webhook.log` (the fetch loop
   runs inside the mail-webhook process; its logs land in `mail_webhook.log`, not `mail.log`).
4. **Confirm intake** — after the next poll interval, new Gmail arrives as `email-gmail-*.md` in
   `vault/inbox/` and is picked up by the curator + `email_classifier` + the 7c topical filer.
5. **Confirm filing continuity** — the 7c-ii Gmail-side labeler re-applies the `Business/*` /
   `Finance/*` labels (hard-gated on the ratified `confidence.filing` flag; see the 7c docs). The
   live archive-semantics verification (does removing INBOX archive as expected) is done here, on a
   **throwaway/test message first**, never a blind write to real mail.

## 5. Behavior change to expect at the flip (name it, don't be surprised by it)

**Display-name alias-matching starts firing.** The `email_classifier`'s high-priority-sender override
(`_apply_high_priority_sender_override`) matches an operator-flagged contact's aliases against the
sender's **display name**. Today's n8n records carry only the **bare address** (n8n's `parseAddr`
stripped the display name), so that alias path rarely fires. The native fetcher preserves the full
`From` header, so **post-flip, alias-matches that never fired on n8n records will start firing** — some
emails may get a `high` priority they didn't get before. This is the operator-intended path (a latent
bug-fix, not a regression), but it is a real change in classifier behavior at the flip. Expect it.

## 6. Rollback

The flip is reversible with the same two levers, in reverse:
1. Set `mail.fetch.enabled: false` in `config.yaml` and restart the mail daemon (fetcher goes INERT —
   no IMAP connection).
2. Re-enable the n8n workflow `Pb3Jh54bjYDoJgpi`.

Because the fetcher marks fetched messages `\Seen` and 7c-ii applies labels, a rollback window where
both run would double-process; keep the disable/enable steps tight and in the stated order.
