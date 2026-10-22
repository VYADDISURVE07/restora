# Plug-ins

Every `.py` file in this folder is loaded when Restora starts (files starting with `_` are skipped).
A plug-in can change anything Restora does, with a few lines of Python:

```python
from restora import plugin

@plugin.generator("pan", "panNumber")          # your own test data for a field name
def my_pan(field, hint):
    return "ABCPE1234F"

@plugin.before_request                         # change every request just before it is sent
def add_channel(request, row):
    request.headers.setdefault("X-Channel", "WEB")

@plugin.after_response                         # look at every response
def log(response, row):
    print(row.name, response.status)

@plugin.healer                                 # your own healing rule, tried before the built-in ones
def needs_channel(attempt):
    if attempt.status == 400 and "channel" in attempt.text.lower():
        return attempt.patch(header={"X-Channel": "MOBILE"}, reason="this API needs a channel header")

@plugin.dynamic                                # more response fields that change on every call
def ignore_nonce(path, expected, actual):
    return "a nonce changes every call" if path.endswith(".nonce") else None
```

A complete, working example (an HMAC request signature that no AI could guess) is in
`examples/power_user/plugins/sign_requests.py`.
