"""Realistic test data for fields a tester left blank.

The brain picks a generator from the field's name ('email', 'mobileNumber', 'ifscCode' ...).
Values look real but are safe: emails use example.com (reserved for documentation, RFC 2606), IPs use
TEST-NET (RFC 5737), cards use the industry test numbers. National ID numbers (Aadhaar, SSN) are never
invented - type those yourself.

Use them explicitly in a sheet as {{$email}}, {{$phone}}, {{$uuid}} ... (see KINDS below).
"""
from __future__ import annotations

import datetime as _dt
import random
import re
import string
import uuid as _uuid

from .names import norm, words

FIRST = ["Priya", "Arjun", "Ananya", "Rohan", "Kavya", "Aditya", "Meera", "Vikram", "Sneha", "Rahul",
         "Isha", "Karan", "Diya", "Nikhil", "Pooja", "Siddharth", "Tanvi", "Varun", "Riya", "Aman"]
LAST = ["Sharma", "Mehta", "Iyer", "Desai", "Nair", "Rao", "Joshi", "Singh", "Kulkarni", "Verma",
        "Patel", "Reddy", "Menon", "Gupta", "Bose", "Kapoor", "Shetty", "Pillai", "Chopra", "Malhotra"]
CITIES = [("Mumbai", "Maharashtra", "400001"), ("Pune", "Maharashtra", "411001"), ("Bengaluru", "Karnataka", "560001"),
          ("Chennai", "Tamil Nadu", "600001"), ("Hyderabad", "Telangana", "500001"), ("New Delhi", "Delhi", "110001")]
STREETS = ["MG Road", "Linking Road", "Park Street", "Brigade Road", "FC Road", "Anna Salai"]

# kind -> words that point to it (matched against the field name's words / normalised form)
_RULES = [
    ("email", lambda n, w: "email" in w or "mail" in w or n in ("emailid", "mailid")),
    ("phone", lambda n, w: bool({"phone", "mobile", "msisdn", "cell", "telephone", "tel"} & set(w)) or n in ("contactno", "contactnumber")),
    ("first_name", lambda n, w: n in ("firstname", "givenname", "fname")),
    ("last_name", lambda n, w: n in ("lastname", "surname", "familyname", "lname")),
    ("username", lambda n, w: n in ("username", "loginid", "loginname", "handle", "userhandle")),
    ("password", lambda n, w: bool({"password", "passwd", "pwd", "passcode"} & set(w))),
    ("otp", lambda n, w: "otp" in w),
    ("pin", lambda n, w: n in ("pin", "mpin", "tpin", "upipin")),
    ("pan", lambda n, w: n in ("pan", "panno", "pannumber", "pancard")),
    ("ifsc", lambda n, w: "ifsc" in w),
    ("gstin", lambda n, w: n in ("gstin", "gst", "gstno", "gstnumber")),
    ("upi", lambda n, w: bool({"upi", "vpa"} & set(w)) and "pin" not in w),
    ("card_number", lambda n, w: n in ("cardnumber", "cardno", "pan16", "creditcard", "debitcard", "cardnum")),
    ("cvv", lambda n, w: n in ("cvv", "cvc", "cvv2", "securitycode")),
    ("expiry", lambda n, w: n in ("expiry", "expirydate", "cardexpiry", "exp", "expdate", "validthru")),
    ("account_number", lambda n, w: "account" in w and bool({"number", "no", "num"} & set(w)) or n in ("acno", "accno", "acctno")),
    ("pincode", lambda n, w: n in ("pincode", "zip", "zipcode", "postalcode", "postcode")),
    ("city", lambda n, w: "city" in w or n == "town"),
    ("state", lambda n, w: n in ("state", "province", "region")),
    ("country_code", lambda n, w: n in ("countrycode", "countryiso", "iso2")),
    ("country", lambda n, w: n == "country" or n == "nationality"),
    ("address", lambda n, w: "address" in w or n in ("street", "addressline", "line1")),
    ("dob", lambda n, w: n in ("dob", "dateofbirth", "birthdate", "birthday")),
    ("timestamp", lambda n, w: bool({"timestamp", "datetime"} & set(w)) or n.endswith("at") and len(w) > 1 and w[-1] == "at"),
    ("date", lambda n, w: "date" in w or n in ("day", "valuedate")),
    ("time", lambda n, w: w[-1:] == ("time",)),
    ("currency", lambda n, w: "currency" in w or n in ("ccy", "curr")),
    ("amount", lambda n, w: bool({"amount", "amt", "price", "cost", "fee", "total", "balance", "deposit", "salary",
                                   "income", "limit"} & set(w))),
    ("quantity", lambda n, w: bool({"quantity", "qty", "count", "unit"} & set(w))),
    ("age", lambda n, w: n == "age"),
    ("uuid", lambda n, w: bool({"uuid", "guid", "nonce", "idempotency", "correlation", "trace"} & set(w))
     or n in ("requestid", "xrequestid", "deviceid", "sessionid", "clientreference", "referenceid")),
    ("url", lambda n, w: bool({"url", "uri", "website", "link", "callback", "webhook"} & set(w))),
    ("ip", lambda n, w: n in ("ip", "ipaddress", "clientip")),
    ("latitude", lambda n, w: n in ("lat", "latitude")),
    ("longitude", lambda n, w: n in ("lng", "lon", "long", "longitude")),
    ("remarks", lambda n, w: bool({"remark", "narration", "description", "note", "comment", "memo", "purpose",
                                   "message", "reason", "summary", "detail", "bio"} & set(w))),
    ("title", lambda n, w: n in ("title", "subject", "heading", "label")),
    ("company", lambda n, w: bool({"company", "employer", "organisation", "organization", "merchant", "business"} & set(w))),
    ("bank_name", lambda n, w: n in ("bankname", "bank")),
    ("file_name", lambda n, w: n in ("filename", "file", "documentname")),
    ("host_name", lambda n, w: n in ("hostname", "host", "domain")),
    ("nickname", lambda n, w: n in ("nickname", "alias", "shortname")),
    ("full_name", lambda n, w: "name" in w),
    ("boolean", lambda n, w: bool(w) and w[0] in ("is", "has", "can", "allow", "enable", "agree", "accept", "consent")
     or n in ("active", "enabled", "verified", "consent", "terms")),
    ("language", lambda n, w: n in ("language", "lang", "locale")),
    ("timezone", lambda n, w: n in ("timezone", "tz")),
    ("status", lambda n, w: n == "status"),
    ("channel", lambda n, w: n == "channel"),
]

# never invented: these need real values from the tester
NEVER_GENERATE = {"aadhaar", "aadhar", "uid", "ssn", "apikey", "xapikey", "clientsecret", "secret", "token",
                  "accesstoken", "refreshtoken", "authorization", "privatekey", "signature"}

FORMAT_KINDS = {"email": "email", "e-mail": "email", "mail": "email", "url": "url", "uri": "url", "uuid": "uuid",
                "guid": "uuid", "date": "date", "datetime": "timestamp", "date-time": "timestamp", "timestamp": "timestamp",
                "phone": "phone", "phonenumber": "phone", "mobile": "phone", "mobilenumber": "phone", "ip": "ip",
                "ipaddress": "ip", "pan": "pan", "ifsc": "ifsc", "pincode": "pincode", "zip": "pincode", "gstin": "gstin",
                "upi": "upi", "vpa": "upi", "time": "time"}

KINDS = sorted({k for k, _ in _RULES} | {"uuid", "int", "number", "string", "random_int"})


def kind_for(field: str) -> str | None:
    n, w = norm(field), words(field)
    if not n or n in NEVER_GENERATE or n.endswith(("token", "secret", "apikey")):
        return None
    for kind, test in _RULES:
        try:
            if test(n, w):
                return kind
        except IndexError:
            continue
    return None


class Generator:
    """One per run. seed=0 -> different data every run (unique emails avoid 'already exists' errors)."""

    def __init__(self, seed: int = 0, overrides: dict | None = None, today: _dt.date | None = None):
        self.rng = random.Random(seed or None)
        self.overrides = {norm(k): v for k, v in (overrides or {}).items()}
        self.today = today or _dt.date.today()
        self._person = None
        self.custom = {}                 # plugin generators: normalised field name -> fn(field, ctx)

    # a consistent fake person per run, so name / email / username agree with each other
    def person(self, fresh: bool = False) -> dict:
        if self._person is None or fresh:
            first, last = self.rng.choice(FIRST), self.rng.choice(LAST)
            tag = self.rng.randint(1000, 9999)
            city, state, pin = self.rng.choice(CITIES)
            self._person = {"first": first, "last": last, "tag": tag, "city": city, "state": state, "pin": pin}
        return self._person

    def value(self, field: str, kind: str | None = None, hint: dict | None = None, siblings: dict | None = None):
        """A value for `field`, or None when it must come from the tester (secrets, national ids).
        hint: {'type': 'number'|'string'|..., 'format': 'email'|..., 'min':..., 'max':..., 'min_length':...,
        'enum': [...], 'fresh': True}.  siblings: the other fields of the same JSON object, so that a generated
        email matches a name typed next to it."""
        hint = hint or {}
        n = norm(field)
        if n in self.custom:
            return self.custom[n](field, hint)
        if n in self.overrides:
            return self.overrides[n]
        if hint.get("enum"):
            return hint["enum"][0]
        kind = kind or FORMAT_KINDS.get(str(hint.get("format") or "").lower().replace(" ", "")) or kind_for(field)
        if kind is None and is_secret_field(field):
            return None
        value = self._make(kind or "string", field, hint, _person_from(siblings))
        return _coerce(value, hint)

    def _make(self, kind: str, field: str, hint: dict, typed: dict | None = None):
        r = self.rng
        p = dict(self.person())
        if hint.get("fresh"):
            p.update(first=r.choice(FIRST), last=r.choice(LAST), tag=r.randint(1000, 9999))
        if typed:
            p.update(typed)
        tag = p["tag"]
        if kind == "email":
            return f"{p['first'].lower()}.{p['last'].lower()}.{tag}@example.com"
        if kind == "phone":
            digits = str(r.choice([9, 8, 7])) + "".join(r.choice(string.digits) for _ in range(9))
            return digits if hint.get("digits_only", True) else "+91 " + digits
        if kind == "full_name":
            return f"{p['first']} {p['last']}"
        if kind == "first_name":
            return p["first"]
        if kind == "last_name":
            return p["last"]
        if kind == "username":
            return f"{p['first'].lower()}_{p['last'].lower()}_{tag}"
        if kind == "password":
            return "Test@" + str(r.randint(10000, 99999))
        if kind == "otp":
            return "".join(r.choice(string.digits) for _ in range(6))
        if kind == "pin":
            return "".join(r.choice(string.digits) for _ in range(4))
        if kind == "pan":
            letters = "".join(r.choice(string.ascii_uppercase) for _ in range(3))
            return f"{letters}P{p['last'][0].upper()}{r.randint(1000, 9999)}{r.choice(string.ascii_uppercase)}"
        if kind == "ifsc":
            return "DEMO0" + "".join(r.choice(string.digits) for _ in range(6))
        if kind == "gstin":
            return _gstin(r)
        if kind == "upi":
            return f"{p['first'].lower()}{tag}@upi"
        if kind == "card_number":
            return r.choice(["4111111111111111", "5555555555554444", "4012888888881881"])
        if kind == "cvv":
            return str(r.randint(100, 999))
        if kind == "expiry":
            return f"{self.today.month:02d}/{(self.today.year + 3) % 100:02d}"
        if kind == "account_number":
            return "5000" + "".join(r.choice(string.digits) for _ in range(8))
        if kind == "pincode":
            return p["pin"]
        if kind == "city":
            return p["city"]
        if kind == "state":
            return p["state"]
        if kind == "country":
            return "India"
        if kind == "country_code":
            return "IN"
        if kind == "address":
            return f"{r.randint(1, 250)}, {r.choice(STREETS)}, {p['city']}"
        if kind == "dob":
            return (self.today.replace(year=self.today.year - r.randint(25, 45)) - _dt.timedelta(days=r.randint(0, 300))).isoformat()
        if kind == "date":
            fmt = hint.get("date_format")
            d = self.today + _dt.timedelta(days=7)
            return _format_date(d, fmt) if fmt else d.isoformat()
        if kind == "timestamp":
            return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if kind == "time":
            return "10:30:00"
        if kind == "currency":
            return "INR"
        if kind == "amount":
            lo = float(hint.get("min", 0) or 0)
            v = max(lo + 1, 1500.0) if not hint.get("max") else min(max(lo + 1, 1500.0), float(hint["max"]))
            return int(v) if float(v).is_integer() and hint.get("type") == "integer" else v
        if kind == "quantity":
            return max(int(hint.get("min", 0) or 0) + 1, 2)
        if kind == "age":
            return 30
        if kind == "uuid":
            return str(_uuid.UUID(int=r.getrandbits(128), version=4))
        if kind == "url":
            return "https://example.com/restora-test"
        if kind == "ip":
            return f"192.0.2.{r.randint(1, 254)}"
        if kind == "latitude":
            return 19.076
        if kind == "longitude":
            return 72.8777
        if kind == "bank_name":
            return "Demo Bank"
        if kind == "file_name":
            return "restora-test.txt"
        if kind == "host_name":
            return "example.com"
        if kind == "nickname":
            return p["first"]
        if kind == "remarks":
            return "Automated test by Restora"
        if kind == "title":
            return "Restora test"
        if kind == "company":
            return "Acme Test Pvt Ltd"
        if kind == "boolean":
            return True
        if kind == "language":
            return "en"
        if kind == "timezone":
            return "Asia/Kolkata"
        if kind == "status":
            return "ACTIVE"
        if kind == "channel":
            return "MOBILE"
        if kind in ("int", "random_int"):
            return r.randint(1, 1000)
        if kind == "number":
            return 100
        # unknown: a readable string, sized to any length rule
        base = f"test-{re.sub(r'[^a-z0-9]+', '-', field.lower()).strip('-') or 'value'}"
        return base

    def by_kind(self, kind: str):
        """For explicit {{$kind}} references (also accepts common Postman names: $guid, $randomEmail ...)."""
        k = norm(kind.lstrip("$"))
        aliases = {"guid": "uuid", "randomuuid": "uuid", "randomemail": "email", "randomfirstname": "first_name",
                   "randomlastname": "last_name", "randomfullname": "full_name", "randomphonenumber": "phone",
                   "randomint": "int", "timestamp": "epoch", "isotimestamp": "timestamp", "randomusername": "username",
                   "randomcity": "city", "randomstreetaddress": "address", "randomprice": "amount",
                   "randomwords": "remarks", "randomloremsentence": "remarks", "randomboolean": "boolean",
                   "randomip": "ip", "randomurl": "url", "name": "full_name", "fullname": "full_name",
                   "firstname": "first_name", "lastname": "last_name", "mobile": "phone", "accountnumber": "account_number",
                   "cardnumber": "card_number", "countrycode": "country_code", "today": "date_today", "now": "timestamp"}
        k = aliases.get(k, k)
        if k == "epoch":
            return int(_dt.datetime.now().timestamp())
        if k == "datetoday":
            return self.today.isoformat()
        if k == "email":
            return self._make("email", "email", {"fresh": True})
        if k == "username":
            return self._make("username", "username", {"fresh": True})
        known = {norm(x): x for x in KINDS}
        if k in known:
            return self._make(known[k], kind, {})
        return None


def is_secret_field(field: str) -> bool:
    n = norm(field)
    return n in NEVER_GENERATE or n.endswith(("token", "secret", "apikey", "password")) and n != "password"


def _person_from(siblings: dict | None) -> dict | None:
    """Use a name the tester typed in the same object: {'name': 'Priya Sharma', 'email': '<email>'}."""
    if not isinstance(siblings, dict):
        return None
    first = last = None
    for k, v in siblings.items():
        if not isinstance(v, str) or not v.strip() or "<" in v or "{{" in v:
            continue
        nk = norm(k)
        if nk in ("firstname", "givenname"):
            first = v.strip().split()[0]
        elif nk in ("lastname", "surname", "familyname"):
            last = v.strip().split()[-1]
        elif nk in ("name", "fullname", "customername", "displayname", "accountholdername") and " " in v.strip():
            parts = v.strip().split()
            first, last = first or parts[0], last or parts[-1]
    if first and last and re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", first) and re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", last):
        return {"first": first, "last": last}
    return None


def _format_date(d: _dt.date, fmt: str) -> str:
    f = fmt.upper().replace("YYYY", "%Y").replace("YY", "%y").replace("MM", "%m").replace("DD", "%d")
    return d.strftime(f)


def _gstin(r: random.Random) -> str:
    chars = string.digits + string.ascii_uppercase
    pan = "".join(r.choice(string.ascii_uppercase) for _ in range(5)) + f"{r.randint(1000, 9999)}" + r.choice(string.ascii_uppercase)
    body = f"27{pan}1Z"
    total = 0
    for i, ch in enumerate(body):
        v = chars.index(ch) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return body + chars[(36 - total % 36) % 36]


def _coerce(value, hint: dict):
    t = (hint.get("type") or "").lower()
    try:
        if t in ("number", "numeric", "float", "decimal", "double"):
            return float(value) if not isinstance(value, bool) else value
        if t in ("integer", "int", "long"):
            return int(float(value))
        if t in ("string", "text", "str"):
            return str(value)
        if t in ("boolean", "bool"):
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "y")
    except (TypeError, ValueError):
        return value
    length = hint.get("min_length")
    if length and isinstance(value, str) and len(value) < int(length):
        value = (value + "x" * int(length))[: max(int(length), len(value))]
    max_len = hint.get("max_length")
    if max_len and isinstance(value, str) and len(value) > int(max_len):
        value = value[: int(max_len)]
    exact = hint.get("length")
    if exact and isinstance(value, str) and value.isdigit():
        value = (value * 3)[: int(exact)]
    return value
