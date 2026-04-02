# n8n Email Filing Instructions — Phase 2

Extends the "Email to Alfred Ingest" workflow to categorize and file financial emails in Outlook folders automatically.

## Prerequisites

### 1. Create Outlook Folders

In Outlook (web or desktop), create these folders:

```
Inbox/
├── Business/
│   ├── Invoices
│   └── Receipts
└── Finance/
    ├── Personal
    └── Tax
```

### 2. Get Folder IDs

Create a temporary n8n workflow to discover folder IDs:

1. Add a **Manual Trigger** node
2. Add an **HTTP Request** node:
   - Method: GET
   - URL: `https://graph.microsoft.com/v1.0/me/mailFolders?$top=50&$expand=childFolders`
   - Authentication: Predefined Credential Type → Microsoft Outlook OAuth2
   - Credential: Select "Microsoft Outlook account - andrew.newton@live.ca"
3. Execute once
4. In the output, find `Business`, `Finance` and their children — copy each folder's `id` field
5. You'll need 4 IDs — paste them into the Code node below where marked `PASTE_ID_HERE`
6. Delete the temporary workflow

## Workflow Changes

Open the **Email to Alfred Ingest** workflow in the n8n editor.

### Node 1: Restore Context & Categorize - Code

**Add after:** "POST to Alfred Ingest - HTTP Request"
**Type:** Code node
**Name:** `Restore Context & Categorize - Code`
**Mode:** Run Once for All Items

**Paste this code:**

```javascript
// ---- FOLDER ID MAP ----
// Replace PASTE_ID_HERE with actual Outlook folder IDs from the discovery step
const FOLDER_IDS = {
  'Business/Invoices': 'PASTE_ID_HERE',
  'Business/Receipts': 'PASTE_ID_HERE',
  'Finance/Tax': 'PASTE_ID_HERE',
  'Finance/Personal': 'PASTE_ID_HERE',
};

// Restore original email context (mandatory — HTTP node replaces $json)
// WARNING: If you rename "New Email - Outlook Trigger", update this reference
const email = $('New Email - Outlook Trigger').first().json;
const webhookResult = $input.first().json;

// Skip filing if webhook failed
if (!webhookResult || webhookResult.status !== 'ok') {
  return [{ json: { _route: 'skip', _reason: 'webhook_failed' } }];
}

const from = (email.from?.emailAddress?.address || '').toLowerCase();
const subject = (email.subject || '').toLowerCase();
const domain = from.split('@')[1] || '';

// ---- TRIAGE RULES ----
// First matching rule wins. To update: add/remove/reorder rules.
// Keep in sync with vault/process/Email Triage Rules.md
const rules = [

  // === Business/Invoices ===
  {
    folder: 'Business/Invoices',
    match: () =>
      domain === 'digitalocean.com' ||
      domain === 'railway.app' ||
      domain === 'cloudflare.com' ||
      domain === 'supabase.com' ||
      domain === 'n8n.io' ||
      (subject.includes('invoice') && from.includes('noreply')) ||
      subject.includes('your invoice') ||
      subject.includes('billing statement')
  },

  // === Business/Receipts ===
  {
    folder: 'Business/Receipts',
    match: () =>
      (subject.includes('receipt') && (
        domain === 'digitalocean.com' ||
        domain === 'railway.app' ||
        domain === 'cloudflare.com' ||
        domain === 'github.com'
      )) ||
      (subject.includes('payment confirmation') && (
        domain === 'digitalocean.com' ||
        domain === 'railway.app' ||
        domain === 'cloudflare.com'
      )) ||
      subject.includes('software license')
  },

  // === Finance/Tax (before Personal — CRA/T4 must not fall through) ===
  {
    folder: 'Finance/Tax',
    match: () =>
      domain === 'cra-arc.gc.ca' ||
      from.includes('canada.ca') ||
      subject.includes('t4 ') ||
      subject.includes('t4a ') ||
      subject.includes('tax slip') ||
      subject.includes('tax receipt') ||
      subject.includes('charitable donation') ||
      subject.includes('donation receipt') ||
      subject.includes('rrsp') ||
      subject.includes('investment statement') ||
      subject.includes('contribution receipt')
  },

  // === Finance/Personal ===
  {
    folder: 'Finance/Personal',
    match: () =>
      (domain === 'patreon.com' && subject.includes('receipt')) ||
      (from.includes('apple.com') && (subject.includes('receipt') || subject.includes('invoice'))) ||
      (from.includes('microsoft.com') && subject.includes('receipt')) ||
      domain === 'costco.ca' || domain === 'costco.com' ||
      (from.includes('amazon') && (subject.includes('order') || subject.includes('receipt'))) ||
      from.includes('doordash') || from.includes('ubereats') ||
      from.includes('skipthedishes') ||
      subject.includes('bank statement') ||
      subject.includes('credit card statement') ||
      subject.includes('interac') ||
      subject.includes('e-transfer') ||
      (from.includes('trulocal') && subject.includes('receipt'))
  }

];

const matched = rules.find(r => r.match());

if (matched) {
  const folderId = FOLDER_IDS[matched.folder];
  if (!folderId || folderId === 'PASTE_ID_HERE') {
    return [{ json: { _route: 'skip', _reason: 'folder_id_not_configured' } }];
  }
  return [{ json: {
    _route: 'file',
    _folder: matched.folder,
    _folderId: folderId,
    _messageId: email.id,
    _subject: email.subject,
    _from: from
  }}];
}

return [{ json: { _route: 'skip', _reason: 'no_matching_rule' } }];
```

**Connect:** Output of "POST to Alfred Ingest - HTTP Request" → this node

### Node 2: Route Filing - Switch

**Add after:** "Restore Context & Categorize - Code"
**Type:** Switch node
**Name:** `Route Filing - Switch`

**Configuration:**
- Add Rule 1:
  - Name: `file`
  - Condition: String → `{{ $json._route }}` → equals → `file`
- Rename fallback output to `skip` (or leave as default)

**Connect:** Output of Code node → this node

### Node 3: Move Email - Microsoft Outlook

**Add after:** "Route Filing - Switch" (connect to the "file" output only)
**Type:** Microsoft Outlook node
**Name:** `Move Email - Microsoft Outlook`

**Configuration:**
- Credential: "Microsoft Outlook account - andrew.newton@live.ca"
- Resource: **Message**
- Operation: **Move**
- Message ID: `{{ $json._messageId }}`
- Folder ID: `{{ $json._folderId }}`

**Settings (gear icon):**
- Enable **Continue On Fail** — filing is best-effort

**Connect:** "file" output of Switch → this node

## Testing

1. **Save the workflow** (don't activate yet)
2. **Test with a real invoice email:**
   - Forward a DigitalOcean or Railway invoice to yourself
   - Click "Test Workflow" or wait for the trigger
   - Check the Code node output: should show `_route: "file"` and `_folder: "Business/Invoices"`
   - Check the Move node: should show success
   - Verify the email moved in Outlook
3. **Test with a normal email:**
   - Send a regular email
   - Code node should show `_route: "skip"`
   - Switch should route to the skip output
   - No move attempted
4. **Test with a personal receipt:**
   - Forward a Costco or Apple receipt
   - Should route to `Finance/Personal`
5. **Activate** the workflow once all tests pass

## Updating Rules

When triage rules change:

1. Open the "Restore Context & Categorize - Code" node
2. Edit the `rules` array — add/remove/reorder rules
3. If a new folder is needed:
   - Create the folder in Outlook
   - Get its ID (re-run the discovery workflow)
   - Add it to `FOLDER_IDS`
4. **Also update** `vault/process/Email Triage Rules.md` to keep in sync

## Important Notes

- The Code node references the trigger by name: `$('New Email - Outlook Trigger')`. If you rename the trigger node, update this reference or context restoration will fail silently.
- Rule order matters — first match wins. Tax rules come before Personal to prevent CRA emails from matching the broader personal finance patterns.
- This filing is independent of Alfred's curator processing. Both run on every email: n8n files instantly, curator processes deeper later.
