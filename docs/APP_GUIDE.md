# Phone app guide: running the bot from your phone

The app is the bot plus a small web server (`python -m server`). It runs on
one always-on machine, and Cloudflare Tunnel puts it on a subdomain of your
domain, for example `bot.yourdomain.com`. On the phone you open that address
in Chrome and add it to the home screen, where it works like an installed app.

```
 Phone (Chrome, home-screen app)
        │ https://bot.yourdomain.com
        ▼
 Cloudflare (your domain, the tunnel, optional Access login)
        │ tunnel, dialled out from the machine: no open ports
        ▼
 Always-on machine:  Docker ─ app (bot + server)  ─ cloudflared
```

A Cloudflare Worker can't run the bot itself: the bot has to stay connected
to Deriv all day, and Workers only run for a moment per request.

## What the app does

| Screen | What it's for |
|---|---|
| Home | Balance, running state, today's result, daily loss limit, open trades, recent trades. **Start / Pause** and **Stop all trading** (the kill switch: closes every open trade and stops). |
| Strategy | Pick the strategy and market, set stake, multiplier, exits and the safety limits. The same checks as `.env` apply, so the app can't set anything the bot would refuse. Switch between demo and real. |
| Lab | Every strategy idea and where it is in testing. **+ New** opens the builder; **AI ideas** asks OpenRouter for new ones. **Keep the AI coming up with ideas** keeps up to 5 ideas waiting to be tested and pauses when 5 are waiting or the day's AI limit is used. Failed ideas are deleted after the days you pick (or with **Delete** / **Delete all failed**); deleted ideas still count towards the pass bar. |
| Lab › strategy | The four readiness checks, the results behind them, **Use this strategy**. |
| Trades | Every trade decision from `logs/trades.csv`, with CSV export. |
| Switch to real | Shows the current strategy's readiness. Needs the box ticked and REAL typed. |

The theme follows the phone's clock: soft cyan in the morning, bright blue in
the day, deep blue in the evening and midnight blue at night.

### How the lab tests an idea

1. **Backtest** on the older 75% of Deriv's price history, with trading costs
   (`commission`, default 0.0002 of stake × multiplier). It needs 30+ trades,
   a profit, and a score (t-statistic of the average trade) above a bar that
   rises as more ideas are tried: 1.5 for the first, 2.0 after 10, 2.5 after
   100. Otherwise enough tries would always produce a lucky winner.
2. **Unseen data**: the newest 25% of prices, held back while designing. It
   must make money there too (10+ trades).
3. **Live prices**: from the day it passes, it trades live prices in
   simulation for 14 days (refreshed every 4 hours) and must stay profitable.
4. **Worst day** must stay inside your daily loss limit.

Pass all four and the app shows a **ready to go live** alert. Nothing switches
to real money by itself: you pick the strategy and flip the account.

### AI ideas

The AI (any OpenRouter model) only proposes strategies in the builder's rule
format. The server rejects anything that isn't a valid rule, and each idea goes
through the same four steps. The AI is shown backtest numbers only, never the
unseen-data or live-price results, so it can't tune ideas to the held-back
prices. It never trades or changes settings. `AI_MAX_IDEAS_PER_DAY` caps
requests; give the OpenRouter key its own credit limit too.

## 1. Pick the machine

Anything that stays on and runs Docker:

* **Oracle Cloud Always Free** VM (Ampere/ARM, Ubuntu): free, needs a card at
  sign-up; free capacity is sometimes busy, so creating the VM can take a
  few tries.
* **A machine at home** (old laptop, mini PC, or your PC with Docker Desktop
  and WSL): free, but it must stay on and online.
* **A small paid VPS** (Hetzner, DigitalOcean and similar): a few dollars a
  month, the least hassle.

## 2. Install Docker and get the code

On Ubuntu:

```
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # then log out and back in
git clone https://github.com/JamarJ-Jam/Trading-Bot.git
cd Trading-Bot
cp .env.example .env
```

Fill in `.env` the same way as in [SETUP_GUIDE.md](SETUP_GUIDE.md) (token, App
ID, limits). Then set up the app login:

```
docker compose build app
docker compose run --rm app python -m server setup
```

Pick a password (12+ characters), add the key it prints to an authenticator
app (Google Authenticator, Samsung Pass, Authy...), and paste the three
`APP_...` lines into `.env`. For AI ideas, also set `OPENROUTER_API_KEY`.

## 3. Create the Cloudflare Tunnel

In the Cloudflare dashboard (names move around now and then):

1. **Zero Trust → Networks → Tunnels → Create a tunnel**, type *Cloudflared*.
   Name it `trading-bot`.
2. On the install step, copy the token from the command it shows (the long
   string after `--token`) into `.env` as `CLOUDFLARE_TUNNEL_TOKEN=`. You don't
   need to run their install command; Docker runs cloudflared for you.
3. **Public hostname**: subdomain `bot`, your domain, service type `HTTP`,
   URL `app:8000`.

Recommended extra lock: **Zero Trust → Access → Applications → Add →
Self-hosted** for `bot.yourdomain.com`, with a policy allowing only your email.
Cloudflare then asks for a one-time code sent to your email before the app's
own login even appears.

## 4. Start it

```
docker compose up -d --build
docker compose logs -f app        # Ctrl+C to stop watching
```

Open `https://bot.yourdomain.com` on the S24+ in Chrome, log in, then
**⋮ → Add to home screen → Install**.

The bot starts stopped and on demo. Press **Start bot** on Home.

## Everyday use

* **Update** after new code is merged: `git pull && docker compose up -d --build`.
* **Stop all trading** works from the app; from the machine, `touch state/STOP`
  does the same.
* After a restart (power cut, update), the bot comes back running only if it
  was running before.
* Data lives in `state/` (settings, lab, risk counters), `logs/` and `data/`.
  Back up `state/` if you care about the lab history.

## Trying it without a server

```
pip install -r requirements.txt
python -m server setup             # paste the lines into .env
APP_SECURE_COOKIE=false python -m server
```

Then open http://localhost:8000 on the same computer.
