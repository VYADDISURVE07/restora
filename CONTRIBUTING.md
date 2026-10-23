# Contributing to Restora

Thank you for helping. Bug reports, new healing rules, error-message formats, test data
generators and documentation fixes are all welcome.

## Before you start

- Open an issue first for anything larger than a small fix, so we can agree on the approach.
- Keep the project's ground rules. A change must never make a negative test pass, heal a server
  error (5xx), invent credentials or send test data off the machine. Rules come first; the local AI
  is used only when the rules have no answer.

## Making a change

1. Fork the repo and create a branch.
2. Install: `pip install -r requirements.txt`
3. Add or update a self-test in `selftest/` for your change.
4. Run the self-tests: `python run.py --selftest` (they must all pass).
5. Open a pull request that says what changed and why.

## Sign your commits

Every commit needs a `Signed-off-by` line, which you add with `git commit -s`. It certifies the
[Developer Certificate of Origin](https://developercertificate.org/): that you wrote the change, or
have the right to submit it, under the project's licence.

## Licence and credit

Restora is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that your
contribution is licensed under the same terms (section 5 of the licence). Keep the copyright and
licence lines at the top of each file, and the [NOTICE](NOTICE) file, in any copy or fork.
