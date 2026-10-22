# How Restora works

Restora runs every sheet through the same short pipeline. The brain sits around the plain request runner:
it plans before the run, fills each request just before it is sent, heals the request if the API rejects it,
and learns from every response so later rows can use the values.

```mermaid
sequenceDiagram
    participant S as Sheet
    participant P as Planner
    participant M as Memory
    participant R as Runner
    participant A as API
    participant H as Healer
    S->>P: rows as typed
    P->>P: repair JSON, method, URL; find gaps; spot negative tests
    P->>R: rows in producer-before-consumer order
    loop every row
        R->>M: fill <token>, {id}, TBD, {{Login.token}}
        R->>A: send
        A-->>R: response
        alt rejected (4xx) and the row is meant to succeed
            R->>H: error response
            H->>H: rules: parse the error, build a patch (local AI only if the rules have none)
            H->>A: retry with the patch
            A-->>H: response
        end
        R->>M: learn every value in the response
    end
    R->>S: report + corrected sheet
```

## 1. Plan (before anything is sent) - `restora/brain/planner.py`

* **Repairs.** Almost-JSON bodies are repaired: smart quotes, single quotes, trailing commas, unquoted keys,
  `True`/`None`, comments, missing brackets. A blank Method is chosen from the row name. The first verb wins,
  so "Get the new customer" is GET and "Open an account" is POST. Short URLs (`/api/...`) are joined to the
  address used by the other rows.
* **Gaps.** Every request is scanned for things a person left for later:
  `<token>`, `{customerId}`, `:id`, `TBD`, `???`, `CHANGE_ME`, `your-api-key`, and explicit references such as
  `{{Login.access_token}}`, `{{token}}` and `{{$email}}`.
* **Intent.** A row is a negative test when it expects a 4xx/5xx status (an exact code, or a class such as
  `4xx`), when its name says so ("wrong", "without", "invalid", "rejected" ...), or when its sample response is
  an error. Negative tests are never healed into passing (see step 4b).
* **Order.** For each gap that must come from another response (tokens, ids, account numbers, explicit
  references), the planner looks for the row that will produce it. It uses that row's sample response and
  what the row does: a POST to a login endpoint returns a token, and a POST to `/customers` returns a
  customer id. When a producer sits below its consumer, it is pulled up just before it. Nothing else moves.
  Plain test data (email, phone ...) never moves a row, because it is generated.

## 2. Fill (just before sending) - `restora/brain/__init__.py`, `memory.py`, `generate.py`

Values come from, in order:

1. the producer the planner chose (exact), or the best earlier value with the same meaning;
2. the row's own sample response (for example `currency: "USD"` in a rates sample);
3. realistic test data, matched to the field name, consistent with names typed nearby, and in Indian formats
   where that matters:
   * a mobile number that starts with 6 to 9
   * a PAN of the form 5 letters, 4 digits, 1 letter
   * an IFSC code (`DEMO0` plus 6 digits)
   * a GSTIN with a valid checksum
   * email addresses at `example.com`
4. the local AI model, for field names the rules do not recognise.

Names are compared by meaning, not spelling. `accessToken`, `access_token` and `<token>` match, so do
`mobileNumber` and `phone`, and so do `fromAccount` and `accountNumber`. Secrets are never generated:
passwords, API keys and national IDs must be typed, and tokens can only come from earlier responses.

## 3. Compare - `restora/compare.py`, `brain/volatile.py`

The sample response is compared exactly: same keys, same values, lists the same length. There are two exceptions:

* `"*"` in the sample accepts any value (the key must still be there);
* with smart comparison on, a difference is accepted with a stated reason when the value is clearly
  generated per call. That covers UUIDs, timestamps, tokens, generated ids, countdowns, and values that
  Restora itself filled in or generated for this run.

A value that echoes what you typed (the `1` in `GET /users/1`) is always compared strictly.

## 4. Heal (when a request that should succeed is rejected) - `restora/brain/heal.py`, `errors.py`

Never for negative tests, never for 5xx, at most 3 attempts (configurable), and never the same patch twice.

| The API says | The patch |
|---|---|
| 401 / 403 and an earlier row returned a token | add `Authorization: Bearer <that token>` |
| 415, or "Content-Type must be application/json" | set the content type the body really has |
| 405 with `Allow: GET, PUT` | use the allowed method that matches the row name ("Update" means PUT) |
| "invalid JSON" while the body is almost-JSON | send the repaired body |
| "phone is required" / "must not be blank" | add the field with a value found as in step 2 |
| "must be one of [SAVINGS, CURRENT]" | pick the allowed value closest to what was sent (`savings` becomes `SAVINGS`) |
| "must be a number" / Pydantic `float_parsing` | convert the value (`"5000"` becomes `5000`) |
| "must be a valid email" / "Invalid IFSC code" | regenerate a valid value of that kind |
| "at least 8 characters", "greater than 0", "format DD-MM-YYYY" | adjust length, number or date format |
| "X is not allowed" | remove the field |
| 409 / "already exists" | regenerate the unique fields (email, username, phone ...) |
| "Header 'X-Request-Id' is required" | add the header (a new UUID for request ids) |
| "currency query parameter is required" | add the query parameter |
| 429 with `Retry-After` | wait, then retry |

The error parser understands these shapes: plain messages; the field-error lists of Spring, Express
(validator) and Joi; FastAPI/Pydantic `detail` lists, including `type` and `ctx`; the field-error maps of Rails
and ASP.NET; RFC 7807 `invalid-params`; and JSON:API `source.pointer`.

Plug-in healers run before the built-in rules. The local AI is asked last, only when nothing else had a
patch. Its answer is schema-checked, and it may not set credentials.

## 4b. Refused for the right reason? (negative tests) - `restora/brain/guard.py`

A negative test can pass by accident. "A transfer with a negative amount is rejected" passes in most tools
when the API answers 400 because a required header is missing, even though the API never looked at the
amount. So after a negative test is refused, Restora reads why. When the reason is a setup problem that the
test's name does not mention, it fixes only that and asks again:

* a missing header, other than credentials and signatures: added (a new UUID for request ids);
* a 401 on a test that is not about login, tokens or passwords: the token from an earlier row is added;
* the wrong content type: corrected.

If the API refuses again, now for the reason the test is about, the row is HEALED. If it accepts the
request, the row FAILS: without the fix the test would have hidden a bug. The body, which is what a negative
test is about, is never changed. Turn it off with `check_negative = false` in `[heal]`.

## 5. Learn and report - `restora/session.py`, `report.py`, `brain/fixed_sheet.py`

Every value in every successful response is remembered for later rows. At the end of the run:

* `report.html`: per row, the plain-English explanation, how it was healed, the gaps filled, the
  differences accepted, the request sent and the response received (secrets masked);
* the **corrected sheet**: your rows in the order that worked, with every fix applied and every gap written as an
  explicit reference (`{{Login.access_token}}`, `{{$email}}`, `{{$uuid}}`). Changing values are marked `*`,
  and missing samples are captured. It runs even with the brain off;
* `brain.json`: the plan and the counts, for tools.

## The local AI - `restora/brain/llm.py`, `ai.py`

* Talks to Ollama (`/api/chat` with `format` = a JSON schema and `think: false`). It also works with any
  OpenAI-compatible server (`response_format: json_schema`).
* Four jobs, each with its own small prompt and schema:
  * a value for an unknown field
  * a patch for an unknown error
  * a plain explanation
  * test rows written from plain English
* Secrets are masked before the model sees anything. Answers are cached for the run, and the number of
  calls per run is capped (`ai.max_calls`).
* If the model is not installed or not running, Restora says so once and continues with rules.
* The default is an instruct model (`qwen3:4b-instruct`). Thinking models also work, since their reasoning is
  removed from the answer, but they are much slower and gain nothing on these small, schema-bound jobs.
