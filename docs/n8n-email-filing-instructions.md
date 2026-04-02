# n8n Email Filing Instructions — Phase 2

Extends the "Email to Alfred Ingest" workflow to categorize and file financial emails in Outlook folders automatically. Folders are created on first use — no manual setup needed.

## Prerequisites

None. The workflow creates Outlook folders automatically when a matching email arrives for the first time.

## Workflow Topology

```
New Email - Outlook Trigger           (existing, unchanged)
  → Build Request Body - Code         (existing, unchanged)
  → POST to Alfred Ingest - HTTP      (existing, unchanged)
  → Categorize Email - Code           (NEW)
  → Route Filing - Switch             (NEW)
      ├─ "file" → Resolve Folder - HTTP Request  (NEW)
      │         → Move Email - HTTP Request       (NEW)
      └─ "skip" → (workflow ends)
```

## Workflow Changes

Open the **Email to Alfred Ingest** workflow in the n8n editor.

### Node 1: Categorize Email - Code

**Add after:** "POST to Alfred Ingest - HTTP Request"
**Type:** Code node
**Name:** `Categorize Email - Code`
**Mode:** Run Once for All Items

**Paste this code:**

```javascript
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
    parent: 'Business',
    child: 'Invoices',
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
    parent: 'Business',
    child: 'Receipts',
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
    parent: 'Finance',
    child: 'Tax',
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
    parent: 'Finance',
    child: 'Personal',
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
  return [{ json: {
    _route: 'file',
    _parentFolder: matched.parent,
    _childFolder: matched.child,
    _folderDisplay: matched.parent + '/' + matched.child,
    _messageId: email.id,
    _subject: email.subject,
    _from: from
  }}];
}

return [{ json: { _route: 'skip', _reason: 'no_matching_rule' } }];
```

**Connect:** Output of "POST to Alfred Ingest - HTTP Request" → this node

### Node 2: Route Filing - Switch

**Add after:** "Categorize Email - Code"
**Type:** Switch node
**Name:** `Route Filing - Switch`

**Configuration:**
- Add Rule 1:
  - Name: `file`
  - Condition: String → `{{ $json._route }}` → equals → `file`
- Rename fallback output to `skip` (or leave as default)

**Connect:** Output of Code node → this node

### Node 3: Resolve Folder - Code

**Add after:** "Route Filing - Switch" (connect to the "file" output)
**Type:** Code node
**Name:** `Resolve Folder - Code`
**Mode:** Run Once for All Items

This node uses `this.helpers.httpRequest()` to call the Microsoft Graph API, find or create the target folder, and return the folder ID.

**Paste this code:**

```javascript
const item = $input.first().json;
const parentName = item._parentFolder;
const childName = item._childFolder;

// Helper: Graph API call via n8n's built-in HTTP helper
async function graphGet(url) {
  return await this.helpers.httpRequestWithAuthentication.call(
    this, 'microsoftOutlookOAuth2Api', {
      method: 'GET',
      url: url,
      json: true,
    }
  );
}

async function graphPost(url, body) {
  return await this.helpers.httpRequestWithAuthentication.call(
    this, 'microsoftOutlookOAuth2Api', {
      method: 'POST',
      url: url,
      body: body,
      json: true,
    }
  );
}

const baseUrl = 'https://graph.microsoft.com/v1.0/me/mailFolders';

// Step 1: Find or create parent folder
let parentId = null;
const topFolders = await graphGet.call(this, baseUrl + '?$top=100');
const existingParent = topFolders.value.find(
  f => f.displayName.toLowerCase() === parentName.toLowerCase()
);

if (existingParent) {
  parentId = existingParent.id;
} else {
  const created = await graphPost.call(this, baseUrl, {
    displayName: parentName
  });
  parentId = created.id;
}

// Step 2: Find or create child folder
let childId = null;
const childFolders = await graphGet.call(
  this, baseUrl + '/' + parentId + '/childFolders?$top=100'
);
const existingChild = childFolders.value.find(
  f => f.displayName.toLowerCase() === childName.toLowerCase()
);

if (existingChild) {
  childId = existingChild.id;
} else {
  const created = await graphPost.call(
    this, baseUrl + '/' + parentId + '/childFolders', {
      displayName: childName
    }
  );
  childId = created.id;
}

// Pass through all fields plus the resolved folder ID
return [{ json: { ...item, _folderId: childId } }];
```

**Important:** The credential name `microsoftOutlookOAuth2Api` must match the credential type used by your Outlook trigger. This is the internal n8n credential type name, not the display name.

**Connect:** "file" output of Switch → this node

### Node 4: Move Email - HTTP Request

**Add after:** "Resolve Folder - Code"
**Type:** HTTP Request node
**Name:** `Move Email - HTTP Request`

We use an HTTP Request instead of the native Outlook node because it gives us direct control over the Graph API call.

**Configuration:**
- Method: **POST**
- URL: `https://graph.microsoft.com/v1.0/me/messages/{{ $json._messageId }}/move`
- Authentication: **Predefined Credential Type** → Microsoft Outlook OAuth2
- Credential: "Microsoft Outlook account - andrew.newton@live.ca"
- Send Body: Yes
- Body Content Type: JSON
- Body:
```json
{
  "destinationId": "{{ $json._folderId }}"
}
```

**Settings (gear icon):**
- Enable **Continue On Fail** — filing is best-effort
- Timeout: 30000

**Connect:** Output of "Resolve Folder - Code" → this node

## Testing

1. **Save the workflow** (don't activate yet)
2. **Test with a real invoice email:**
   - Forward a DigitalOcean or Railway invoice to yourself
   - Click "Test Workflow" or wait for the trigger
   - Categorize node should show `_route: "file"` and `_parentFolder: "Business"`, `_childFolder: "Invoices"`
   - Resolve Folder node should find or create the folder and return `_folderId`
   - Move Email node should succeed
   - Verify the email moved in Outlook — the Business/Invoices folder should exist now
3. **Test with a normal email:**
   - Send a regular email
   - Categorize node should show `_route: "skip"`
   - Switch routes to skip, no further nodes run
4. **Test with a personal receipt:**
   - Forward a Costco or Apple receipt
   - Should create Finance/Personal folder and move the email there
5. **Activate** the workflow once all tests pass

## Updating Rules

When triage rules change:

1. Open the "Categorize Email - Code" node
2. Edit the `rules` array — add/remove/reorder rules
3. New folders are created automatically on first matching email — no manual folder creation needed
4. **Also update** `vault/process/Email Triage Rules.md` to keep in sync

## Important Notes

- The Categorize Code node references the trigger by name: `$('New Email - Outlook Trigger')`. If you rename the trigger node, update this reference or context restoration will fail silently.
- Rule order matters — first match wins. Tax rules come before Personal to prevent CRA emails from matching the broader personal finance patterns.
- This filing is independent of Alfred's curator processing. Both run on every email: n8n files instantly, curator processes deeper later.
- The Resolve Folder node makes 2-4 Graph API calls per filed email (list parent folders, optionally create parent, list child folders, optionally create child). This is fast (~200ms total) and well within rate limits.
- The `microsoftOutlookOAuth2Api` credential type name is n8n's internal name. If your credential shows a different type in n8n's credential editor, update the `httpRequestWithAuthentication` calls to match.
