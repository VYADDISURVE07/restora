# Restora setup guide

Restora needs **Python 3.9 or newer** and about 100 MB of disk. The free local AI is optional and needs
about 4 GB more. Everything runs on your computer.

## 1. Get Restora

**Download:** on the GitHub page choose **Code → Download ZIP**, and unzip it anywhere, for example `D:\Restora`.

**Or with git:**

```text
git clone https://github.com/vnshdfc/restora.git
```

## 2. Install Python (skip if `python --version` already shows 3.9 or newer)

* **Windows:** download it from <https://www.python.org/downloads/>. In the installer, tick
  **"Add python.exe to PATH"**.
* **Mac:** use the installer from python.org, or `brew install python`.
* **Linux:** it is usually installed. On Debian or Ubuntu, add `sudo apt install python3-venv`.

## 3. Install Restora (once per computer)

| Windows | Mac / Linux |
|---|---|
| Double-click **`setup.bat`** | `sh setup.sh` |

This creates a private Python environment in the `.venv` folder, so nothing else on your computer changes.
It then installs four small packages (requests, pytest, allure-pytest, openpyxl) and runs Restora's own tests.
The last lines should say `... passed`.

## 4. Use it

| What | Windows | Mac / Linux |
|---|---|---|
| Point-and-click app (Studio) | double-click **`studio.bat`** | `sh studio.sh` |
| Try the Demo Bank in a terminal | `run.bat --demo` | `sh run.sh --demo` |
| Run your own sheet | `run.bat my_tests.xlsx` | `sh run.sh my_tests.xlsx` |

Studio opens in your browser at `http://127.0.0.1:8765`. Leave its black window open while you use it;
closing that window stops Studio.

**Your sheet** needs only a **URL** column. The other columns are optional:

* Name
* Method
* Headers (one `Name: value` per line)
* Body
* ExpectedStatus (a code such as `201`, or `4xx` for "any client error")
* ExpectedResponse (`*` as a value means "any value")

Column names are forgiving: `API URL`, `Request Body` and `Sample Response` are all understood. Start a row's
Name with `#` to switch the row off. Gaps can stay gaps: write `<token>`, `{customerId}` or `TBD` and the brain
fills them.

## 5. The free local AI (optional)

| Windows | Mac / Linux |
|---|---|
| Double-click **`ai_setup.bat`** | `sh ai_setup.sh` |

This installs [Ollama](https://ollama.com) from its official site. On Windows the installer's signature is
checked before it runs. It then downloads the `qwen3:4b-instruct` model (about 2.5 GB). For an older computer, use a
smaller model: `ai_setup.bat qwen3:1.7b`, then set `model = "qwen3:1.7b"` in `restora.toml`. Check it with
`run.bat --ai-check`.

Without the AI, Restora still fills gaps, orders rows, heals the common rejections and explains failures,
using rules. The AI adds three things: values for unusual field names, fixes for unusual error messages,
and tests written from plain English.

Already have LM Studio, llama.cpp or another OpenAI-compatible server? In `restora.toml` set
`provider = "openai"`, `url = "http://localhost:1234/v1"` and the model name.

## 6. Offline install (computers without internet, such as many bank networks)

On a computer **with** internet and the same Python version:

```text
pip download -r requirements.txt -d wheels
```

Copy the whole Restora folder, including the new `wheels` folder, to the offline computer and run `setup.bat`.
It notices the `wheels` folder and installs from it.

## 7. Company proxy

If `setup.bat` cannot download packages, set your proxy first in the same window:

```text
set HTTPS_PROXY=http://proxy.yourcompany.com:8080
setup.bat
```

If the proxy re-signs HTTPS traffic, also point Python at your company's certificate file:
`set REQUESTS_CA_BUNDLE=C:\path\to\company-ca.pem`. Then run `setup.bat` again.

## 8. Troubleshooting

| You see | Do this |
|---|---|
| `Python was not found` | Install Python (step 2) with "Add python.exe to PATH" ticked, then run setup again |
| `Run setup.bat first` | Run `setup.bat` once in this folder |
| `Port 8765 is busy` | Studio is already open. Use that window, or run `run.bat --studio --port 8766` |
| `SSL certificate problem` | A test server with its own certificate: add `--insecure`, or set `REQUESTS_CA_BUNDLE` |
| `No local AI found` | Run `ai_setup.bat`, or ignore it: rules still work |
| `the model 'qwen3:4b-instruct' is not installed` | `ollama pull qwen3:4b-instruct` |
| A row says "Could not fill <token>" | Add a Login row that returns a token, or type the token into Headers |
