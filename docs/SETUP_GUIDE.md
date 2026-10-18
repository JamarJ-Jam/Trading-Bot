# Setup guide: getting the Deriv bot running

This is everything you need to do on your side, in order. Steps 1 and 2 can
happen at the same time: you can use the demo account while Deriv checks your
documents.

The bot starts on the **demo account** (virtual money) and will refuse to touch
a real account unless you deliberately switch it over (step 7).

Only ever log in at **deriv.com** (and **developers.deriv.com** for step 3).
Fake Deriv sites exist; check the address bar before typing your password.

---

## 1. Verify your Deriv account (ID and address)

You don't need this for the demo, but you will need it before you can deposit
or withdraw real money, so start it now.

1. Log in at deriv.com and open **Account settings**, then **Verification**
   (on some screens it appears as a banner asking you to verify, or under your
   profile icon).
2. **Proof of identity:** upload a passport, driver's licence or national ID
   card, and take the selfie it asks for. Make sure all four corners are
   visible, nothing is blurry, and the ID is not expired.
3. **Proof of address:** upload a document from the **last 6 months** showing
   your full name and home address, matching what you entered on Deriv. A
   utility bill, bank statement, or official government letter works. Screenshots
   of mobile banking apps are often rejected; a PDF statement is safer.
4. Wait for the review. Deriv emails you when it's approved or if they need a
   clearer copy. It usually takes a few business days.

While you're there, note which Deriv company your account is with (it is shown
in your account details or terms) and make sure using Deriv is allowed where
you live.

## 2. Switch to the demo account

1. On deriv.com, open **Trader's Hub** and switch the toggle at the top from
   **Real** to **Demo**.
2. You should see a demo balance (normally 10,000 virtual USD). If it's low, you
   can reset it from the same screen.
3. You don't need to "fund" the demo with real money. It is free and refills
   when you reset it.

## 3. Create your App ID and API token

The bot needs two things from Deriv: an **App ID** (identifies the bot) and an
**API token** (lets the bot trade on your account).

**App ID**

1. Go to **developers.deriv.com** and log in with your Deriv account.
2. Open the **Dashboard** and choose **Register application** (it may be
   called "Create app").
3. Name it something like `Jamar trading bot`. Set any markup to **0**. If it
   asks for a redirect URL, `https://localhost` is fine because the bot doesn't
   use web login.
4. Save it. Copy the **App ID** number it shows. This is not secret, but keep it
   handy.

**API token**

1. In the dashboard (or under **Account settings → API token** on deriv.com),
   create a new token.
2. Name it `trading-bot`.
3. Tick **Trade** (and **Read** if it's listed separately). **Do not tick Admin
   or Payments.** The bot never needs to move money.
4. Click Create and copy the token straight into your `.env` file (step 5).

Token rules:
- Never paste it into chat (including here), email, screenshots, or GitHub.
- It only goes in the `.env` file on your computer. That file is set up so git
  never uploads it.
- If you think it leaked, delete it in the same screen and make a new one.

Deriv sometimes moves these menus around. If a button isn't where this guide
says, look for "API token" in your account settings or "Applications" in the
developer dashboard.

## 4. Install Python and get the code

**Python (version 3.11 or newer)**

- **Windows:** download it from python.org. On the first installer screen,
  tick **"Add python.exe to PATH"**, then click Install.
- **Mac:** download it from python.org and run the installer.

Check it worked by opening a terminal (Windows: "Command Prompt"; Mac:
"Terminal") and typing `python --version` (Mac: `python3 --version`).

**The code**

Easiest: on the GitHub page for the repository, click the green **Code**
button, then **Download ZIP**, and unzip it somewhere like your Documents
folder.

If you have git installed, this does the same:

```
git clone https://github.com/JamarJ-Jam/Trading-Bot.git
cd Trading-Bot
```

### Using WSL Ubuntu instead (works fine)

If you already use WSL, skip installing Python on Windows and do steps 4 to 6 inside Ubuntu:

```
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
cd ~
git clone https://github.com/JamarJ-Jam/Trading-Bot.git
cd Trading-Bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
code .
```

- Keep the folder in your Linux home (`~/Trading-Bot`), not under `/mnt/c/...`. It's faster and avoids file-permission oddities.
- `code .` opens VS Code connected to WSL (install the **WSL** extension if it asks). Edit `.env` there, then pick the `.venv` interpreter when the Python extension prompts.
- Python 3.10 or newer is fine, which is what Ubuntu 22.04 and later ship.
- Run the bot from the VS Code terminal with `python -m bot --check`, then `python -m bot`. To use the kill switch, run `touch STOP` in a second terminal in the same folder.
- The bot stops if WSL shuts down, so keep that terminal open and the PC awake while it runs.

## 5. Set up and test the connection

Open a terminal **inside the Trading-Bot folder** and run these once.

**Windows**

```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

**Mac**

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
open -e .env
```

In the `.env` file that opens, fill in these two lines and save:

```
DERIV_API_TOKEN=your token here
DERIV_APP_ID=your app id here
```

Leave `DERIV_ACCOUNT_TYPE=demo` as it is.

Now test it (this trades nothing):

```
python -m bot --check
```

You should see your accounts listed, a line saying you're connected to a demo
account, the latest price, and "Connection check passed". If you get an error
about the token or App ID, double-check the `.env` values.

## 6. Run the bot on demo

Each time you open a new terminal, activate the environment first
(`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Mac).

1. **Watch mode (default).** Run `python -m bot`. It connects, prints prices,
   and places no trades. Leave it a few minutes to confirm it stays connected.
2. **Plumbing test.** In `.env`, change `STRATEGY=watch` to
   `STRATEGY=demo_test` and run `python -m bot` again. It places a $1 demo
   trade every 5 to 10 minutes, alternating up and down, one at a time, and
   closes each within 10 minutes. This is not a real strategy and will slowly lose virtual money;
   it's only there to prove buying, tracking and logging all work.
3. **Check the results** in the `logs` folder: `trades.csv` opens in Excel or
   Google Sheets and has a row for every trade opened, closed or skipped.
   `bot.log` has the full activity.

**Stopping the bot**

- To stop *and close its open trades*: create an empty file named `STOP` in
  the Trading-Bot folder. The bot closes its trades and exits within a few
  seconds. Delete the `STOP` file before starting it again.
- `Ctrl+C` stops it immediately, but leaves any open trade running on Deriv.

Your computer has to stay on and awake while the bot runs. A small cloud server
(VPS) can come later once the bot has earned it.

## 7. When to consider real money

Not yet. The bot currently has no real strategy, only the watch and test modes.
The next step is choosing and building a strategy, then running it on demo.

It is reasonable to consider your ~$100 only when **all** of these are true:

- A real strategy is built and has run on demo for **at least 300 trades**
  with the exact settings you'd use for real.
- The trade log shows a profit over those trades **after** using the real
  average win and loss (remember: at a 70% win rate, an average win under about
  43% of the average loss still loses money).
- It has run for at least two weeks without crashes, stuck trades, or errors
  you can't explain.
- Your ID and address are verified, and you've tested a small deposit and
  withdrawal so you know you can get money out.
- The limits are sized for $100: `STAKE=1.00`, `MAX_DAILY_LOSS=5.00` or lower.
- It's money you can afford to lose completely.

Switching is deliberate on purpose: you'd set `DERIV_ACCOUNT_TYPE=real` and
`ALLOW_REAL_MONEY=YES_I_UNDERSTAND` in `.env`. The test strategy refuses to run
on a real account.

This guide is general information, not financial advice.
