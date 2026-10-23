# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
"""Understanding field names the way a person does.

`accessToken`, `access_token`, `Access-Token` and `<access token>` are the same thing to a tester,
so the brain compares names by their *words*, not their spelling. Synonym groups cover the common
cases where different words mean the same value (token / jwt, phone / mobile, zip / pincode ...).
"""
from __future__ import annotations

import re
from functools import lru_cache

# Each group is a set of normalised names that mean the same value.
_GROUPS = [
    {"token", "accesstoken", "authtoken", "bearertoken", "jwt", "idtoken", "sessiontoken", "apitoken",
     "authorization", "auth", "bearer", "authkey"},
    {"refreshtoken", "refresh"},
    {"phone", "mobile", "mobileno", "mobilenumber", "phonenumber", "phoneno", "msisdn", "contactnumber",
     "contactno", "cell", "cellphone", "telephone", "tel"},
    {"email", "emailaddress", "mail", "emailid", "mailid"},
    {"name", "fullname", "displayname"},
    {"firstname", "givenname", "fname"},
    {"lastname", "surname", "familyname", "lname"},
    {"username", "loginid", "loginname"},
    {"pincode", "zip", "zipcode", "postalcode", "postcode"},
    {"dob", "dateofbirth", "birthdate", "birthday"},
    {"accountnumber", "accountno", "acno", "accno", "acctno", "accountnum"},
    {"ifsc", "ifsccode"},
    {"pan", "pannumber", "panno"},
    {"amount", "amt", "txnamount", "transactionamount"},
    {"requestid", "xrequestid", "correlationid", "xcorrelationid", "traceid", "idempotencykey",
     "idempotencyid", "xidempotencykey"},
    {"remarks", "remark", "narration", "description", "note", "notes", "comment", "comments", "memo", "purpose"},
    {"address", "addressline", "addressline1", "street", "streetaddress"},
]
_GROUP_OF = {}
for _i, _g in enumerate(_GROUPS):
    for _n in _g:
        _GROUP_OF[_n] = _i

# Words that carry no meaning on their own when matching names.
_FILLER = {"the", "a", "an", "your", "my", "our", "here", "value", "of", "for", "new", "enter", "fill", "put",
           "insert", "type", "this", "that", "current", "valid", "actual", "real"}


def add_synonyms(extra: dict) -> None:
    """restora.toml [synonyms] -> {"token": ["sessionKey", "authKey"]}: makes those names mean the same."""
    for main, others in (extra or {}).items():
        names = {norm(main)} | {norm(o) for o in (others if isinstance(others, (list, tuple)) else [others])}
        names.discard("")
        group = next((_GROUP_OF[n] for n in names if n in _GROUP_OF), None)
        if group is None:
            _GROUPS.append(set())
            group = len(_GROUPS) - 1
        for n in names:
            _GROUPS[group].add(n)
            _GROUP_OF[n] = group
    words.cache_clear()


@lru_cache(maxsize=4096)
def words(identifier: str) -> tuple:
    """'accessToken' -> ('access', 'token');  'X-Request-ID' -> ('x', 'request', 'id');  'users' -> ('user',)"""
    s = str(identifier or "")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)        # camelCase -> camel Case
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)     # HTTPServer -> HTTP Server
    parts = [p.lower() for p in re.split(r"[^A-Za-z0-9]+", s) if p]
    return tuple(singular(p) for p in parts if p not in _FILLER) or tuple(singular(p) for p in parts)


def singular(word: str) -> str:
    w = word.lower()
    if len(w) <= 3 or w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


def norm(identifier: str) -> str:
    return "".join(words(identifier))


def same_group(a: str, b: str) -> bool:
    ga, gb = _GROUP_OF.get(norm(a)), _GROUP_OF.get(norm(b))
    return ga is not None and ga == gb


def group_of(name: str):
    return _GROUP_OF.get(norm(name))


def is_token_name(name: str) -> bool:
    n = norm(name)
    return _GROUP_OF.get(n) == 0 or n.endswith("token") or n in ("jwt",)


def is_id_name(name: str) -> bool:
    w = words(name)
    return bool(w) and w[-1] in ("id", "uuid", "guid", "number", "no", "ref", "reference", "code")


def similarity(a: str, b: str) -> float:
    """0..1: how sure we are that two field names mean the same value."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if same_group(a, b):
        return 0.9
    wa, wb = words(a), words(b)
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if long_[-len(short):] == short:                     # 'token' ~ 'access token', 'id' ~ 'customer id'
        return 0.62 if short == ("id",) else 0.8
    if long_[:len(short)] == short and len(short) >= 1 and short != ("id",):   # 'customer' ~ 'customer id' is weak
        return 0.55
    ca, cb = core(a), core(b)
    if ca and ca == cb and (set(wa) | set(wb)) & _ID_WORDS:   # 'fromAccount' ~ 'accountNumber' (same thing, roles differ)
        return 0.72
    common = set(wa) & set(wb)
    if not common or common <= {"id"}:
        return 0.0
    return 0.7 * len(common) / len(set(wa) | set(wb))


_ROLE_WORDS = {"from", "to", "source", "target", "src", "dst", "dest", "destination", "payer", "payee", "sender",
               "receiver", "parent", "primary", "secondary", "old", "selected", "default", "linked", "debit",
               "credit", "beneficiary"}
_ID_WORDS = {"id", "number", "no", "num", "code", "ref", "reference", "uuid", "guid"}


def core(name: str) -> tuple:
    """The thing a name refers to, without roles and id-suffixes: 'fromAccountNo' -> ('account',)."""
    return tuple(w for w in words(name) if w not in _ROLE_WORDS and w not in _ID_WORDS)


def resource_of_path(path: str) -> str:
    """'/api/v1/customers/{id}/accounts' -> 'account' (the last collection name in the path)."""
    segs = [s for s in re.split(r"[/?#]", path or "") if s]
    for seg in reversed(segs):
        if re.fullmatch(r"v\d+|api|\d+|[{<:$].*|.*[}>]", seg, re.I):
            continue
        if re.fullmatch(r"[A-Za-z][\w\-]*", seg):
            return singular(norm(seg))
    return ""


def resource_before(path: str, marker: str) -> str:
    """The collection name just before a placeholder in a path: '/customers/{id}' + '{id}' -> 'customer'."""
    idx = (path or "").find(marker)
    if idx < 0:
        return ""
    return resource_of_path(path[:idx])
