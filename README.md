# EN-JP-Translator

LINE messages are translated with DeepL: Japanese → English, English → Japanese,
and other languages → Japanese + English.

## Setup

1. Install Python 3.10+ and `pip install -r requirements.txt`.
2. Set `DEEPL_API_KEY`, `LINE_CHANNEL_SECRET`, and `LINE_CHANNEL_ACCESS_TOKEN`
   in the hosting service's environment, or a local `.env` (see `.env.example`).
3. Run `uvicorn main:app --host 0.0.0.0 --port 8000` locally. On a host, use its
   assigned port, for example `uvicorn main:app --host 0.0.0.0 --port "$PORT"`.
4. Set the LINE webhook URL to `https://YOUR_HOST/callback`, verify it, and enable
   webhooks. Enable **Webhook redelivery** in LINE Developers Console as well.
5. To use the bot in groups, enable the official account's group participation.

DeepL API Free keys ending in `:fx` use `api-free.deepl.com`. Other keys use
`api.deepl.com`. Override `DEEPL_URL` only if necessary.

## Translation behavior

- Text containing hiragana or katakana is sent directly to English with Japanese
  as its source, avoiding a wasteful Japanese-to-Japanese request.
- Other text uses DeepL's source detection. Kanji alone is **not** assumed Japanese,
  because Chinese uses these characters too.
- To override an ambiguous short text or a mixed-language message, use `/en 本文`
  (translate to English) or `/ja text` (translate to Japanese). Detection of very
  short text, names, and mixed languages cannot always be correct.
- Whitespace, number-only, and emoji-only messages are ignored.
- Replies exceeding LINE's 5,000 UTF-16-unit limit are split into up to five text
  messages. Larger results prompt the sender to split the original input.
- DeepL errors produce a bilingual failure notice instead of silent non-response.
  429/5xx and connection errors get one bounded retry; quota/auth errors do not.

## Reliability and diagnosis

Blocking HTTP calls run in a thread pool, allowing other webhooks and health
checks to run while translations are in progress. Events within a webhook batch
are processed concurrently and independently. The callback waits for these
operations: it does not acknowledge uncompleted work in an in-memory background
queue. Reply failures return HTTP 500, allowing LINE redelivery when enabled.
Successful event IDs are deduplicated for 20 minutes (up to 10,000 entries).
Duplicate protection is per process; multiple workers/replicas and restarts need
shared durable storage/queue for stronger delivery guarantees. Reply network
failures are inherently ambiguous if LINE accepted a reply before the connection
failed. LINE reply tokens are single-use and normally need to be used within one
minute; deep backlogs, cold starts, or service outages can still prevent replies.

Logs contain event IDs, detected language, failure types and HTTP statuses, but
not message bodies or API credentials. Check hosting logs for:

- DeepL 401/403: key or endpoint configuration problem.
- DeepL 456: translation quota exhausted.
- DeepL 429: rate limit; persistent occurrences need reduced traffic.
- LINE 400: inspect the LINE Developers error statistics for invalid/expired reply
  tokens and payload problems; LINE 401: access token problem.
- No webhook log at all: check host availability, webhook URL/settings and group
  participation. Hosts that sleep when idle can delay or lose the first reply.

The repository contains no hosting configuration or production credentials.
Deploy the merged changes on the existing host (or verify that its GitHub auto
deploy ran), then test both `Hello, how are you?` and `今日は何時に集まる？` in LINE.

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests use mock DeepL/LINE API responses and signed webhook payloads with the real
LINE SDK and FastAPI. They do not spend API quota or send real LINE messages.

References:
- https://developers.line.biz/en/reference/messaging-api/
- https://developers.line.biz/en/docs/messaging-api/receiving-messages/
- https://developers.deepl.com/docs
