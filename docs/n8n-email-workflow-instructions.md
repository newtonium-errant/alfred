# n8n Email Workflow Instructions

Build an n8n workflow that forwards incoming emails to an Alfred webhook.

## What this workflow does
Watches two email accounts for new messages and POSTs them as JSON to a webhook endpoint. The webhook writes them as markdown files into an Obsidian vault where they get processed by an AI tool called Alfred.

## Email accounts
1. **Microsoft Outlook** — andrew.newton@live.ca
2. **Gmail** — (address TBD, will be added later — build the Outlook one first)

## Trigger
Use the **Microsoft Outlook Trigger** node (or Gmail Trigger when adding that account). Trigger on new email received in Inbox.

## HTTP Request node
POST to the Alfred webhook endpoint.

- **Method:** POST
- **URL:** `https://webhook.ruralroutetransportation.ca/ingest`
- **Headers:**
  - `Content-Type: application/json`
  - `Authorization: Bearer {{webhook_token}}` — store this as an n8n credential or environment variable
- **Body (JSON):**

```json
{
  "subject": "{{ $json.subject }}",
  "from": "{{ $json.from.emailAddress.address }}",
  "to": "{{ $json.toRecipients[0].emailAddress.address }}",
  "date": "{{ $json.receivedDateTime }}",
  "body": "{{ $json.body.content }}",
  "account": "live",
  "message_id": "{{ $json.internetMessageId }}",
  "in_reply_to": "{{ $json.inReplyTo }}"
}
```

> **Note:** The exact field paths depend on which n8n email node is used. The fields above are based on the Microsoft Outlook node. Check the node's output and adjust the expressions to match. The important thing is that these JSON keys are sent: `subject`, `from`, `to`, `date`, `body`, `account`, `message_id`, `in_reply_to`.

## Webhook contract
The receiving endpoint expects:

- **Endpoint:** `POST /ingest`
- **Content-Type:** `application/json`
- **Auth:** Bearer token in Authorization header (optional — if no token is set on the server, it accepts all requests)
- **Required fields:** `subject`, `body`
- **Optional fields:** `from`, `to`, `date`, `account`, `message_id`, `in_reply_to`
- **Success response:** `200 {"status": "ok", "file": "email-live-20260326-192600-subject-slug.md"}`

## Error handling
- If the webhook returns non-200, retry once after 30 seconds
- If it still fails, continue (don't block future emails)

## Health check
The webhook also has `GET /health` which returns `200 {"status":"ok"}` — can be used to verify connectivity before activating the workflow.
