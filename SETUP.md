# Setup — GitHub Actions + GitHub Pages (100% free, no credit card)

Yeh kaise kaam karta hai (1 line mein): **GitHub Actions** har 15 minute (9:15 AM – 3:30 PM IST, trading days)
tumhare positions check karta hai, Yahoo se price fetch karta hai, exit rules chalata hai,
aur Telegram pe alert bhejta hai — aur result `docs/state.json` mein save karke repo mein commit
kar deta hai. **GitHub Pages** us `docs/` folder ko ek website ki tarah serve karta hai, jo tum
kisi bhi phone se khol sakte ho, home screen pe add kar sakte ho, aur wahi se naya position add /
close bhi kar sakte ho.

Koi card, koi paid service nahi lagti. Bas GitHub account chahiye (agar nahi hai, github.com pe
free mein bana lo — sirf email chahiye).

---

## Step 1 — Repo banao aur files upload karo

1. github.com pe login karo → **New repository**
2. Naam do, jaise `etf-tracker`. **Private** rakho (recommended — tumhara trade data hai).
3. Is zip ki saari files (jo maine banayi hain) us repo mein upload kar do — GitHub ki web UI
   mein "Add file → Upload files" se, ya `git push` se agar command line comfortable hai.
4. Confirm karo ki structure aisa dikhe:
   ```
   .github/workflows/scan.yml
   scan.py
   requirements.txt
   portfolio.json
   docs/index.html
   docs/state.json
   docs/manifest.json
   docs/sw.js
   docs/icon.svg
   ```

## Step 2 — Telegram secrets add karo

1. Repo mein **Settings → Secrets and variables → Actions → New repository secret**
2. Do secrets banao:
   - `TELEGRAM_BOT_TOKEN` — @BotFather se mila token
   - `TELEGRAM_CHAT_ID` — @userinfobot se mila numeric chat ID
3. Yeh secrets encrypted rehte hain, koi bhi (tum bhi baad mein) inhe read nahi kar sakta, sirf
   overwrite.

## Step 3 — GitHub Pages on karo

1. **Settings → Pages**
2. "Build and deployment" mein Source = **Deploy from a branch**
3. Branch = `main`, Folder = **/docs** → Save
4. Ek minute mein URL milega jaisa: `https://yourusername.github.io/etf-tracker/`

## Step 4 — Ek personal access token banao (sirf iss repo ke liye)

Yeh token dashboard ko position add/close/delete karne deta hai, seedha GitHub se baat karke.

1. **github.com → apni profile photo → Settings → Developer settings → Personal access tokens
   → Fine-grained tokens → Generate new token**
2. Naam do, jaise `etf-tracker-dashboard`
3. **Repository access** → "Only select repositories" → apna `etf-tracker` chuno (koi aur repo
   nahi milega isse — safe hai)
4. **Permissions** mein:
   - Contents → **Read and write**
   - Actions → **Read and write**
5. Generate karo, token copy kar lo (yeh sirf ek baar dikhega)

## Step 5 — Dashboard open karo aur setup karo

1. Step 3 wala URL phone ke Chrome mein kholo
2. **Setup** button tap karo (jahan pehle "Alerts" tha)
3. GitHub username, repo name (`etf-tracker`), aur Step 4 wala token paste karo → Save
4. Yeh token sirf tumhare phone ke browser mein save hota hai (localStorage), kahi bhejta nahi
   hai sivaye seedha `api.github.com` ke

## Step 6 — Pehla scan chalao

Scheduled scan market hours (9:15 AM – 3:30 PM IST, har 15 min, Mon-Fri; NSE holidays skip) mein khud chalta hai. Abhi turant
test karne ke liye:

1. Repo mein **Actions tab → "ETF scan" workflow → Run workflow → Run workflow**
2. ~30-60 second baad dashboard refresh karo (ya "Check now" button, jo yehi kaam karta hai)

## Step 7 — Home screen pe add karo

Chrome mein dashboard URL khula ho, ⋮ menu → **Add to Home screen**. App jaisa icon milega, bina
browser bar ke.

---

## Roz ka use

- **Add position**: dashboard se hi, jaise pehle tha
- **Record sell**: dashboard se, lekin sell price khud daalna padega (browser Yahoo se live price
  nahi maang sakta — CORS block karta hai)
- **Alerts**: automatic, har 15 min market hours mein, Telegram pe

## Jaan lo yeh baatein

- **Cron time thoda idhar-udhar ho sakta hai** — GitHub ka free scheduler "best effort" hai,
  kabhi 5-15 min late chal sakta hai. Yeh normal hai.
- **Dashboard link technically public hai** — repo private hai, lekin GitHub Pages se publish hua
  page khud ek public URL par hai (link kisi ko na do). Isme sirf tumhara portfolio data dikhta
  hai, koi login credential nahi, isliye risk low hai — par URL guess-proof nahi hai, isse zyada
  security chahiye toh bata dena, ek simple password-gate bhi laga sakte hain.
- **Free tier limits** — GitHub Actions free minutes (2000/month private repo, unlimited public)
  itni hai ki yeh scan kabhi khatam nahi hogi is scale par.

## Schedule & strategy notes (split-capital / slot version)

Strategy = `backtest_nse_split.py`. Saari values `scan.py` ke upar CONFIG me hain aur backtest se same rakhni hain:
DMA 10, target +3%, stop-loss -8%, max hold 5 din, trail 0.5%, breakeven (+1.5% touch -> stop entry+1%), EMA 5/40 death cross, late-hold EMA 3/13 (din 5 ke baad).

- **Slots**: capital `N_SLOTS` (2) hisson me. `MAX_CONCURRENT_POSITIONS = 1` => ek time par ek hi position; jis slot ne aaj sell kiya wo aaj dobara use nahi hota (T+1), doosra free slot usi din buy leta hai. `ALLOW_SAME_ETF = False` => jo ETF already held hai wo skip, agla rank liya jata hai.
- **BUY alert**: roz **3:00 PM - 3:29 PM IST** ke scans me Telegram + dashboard par "BUY - ETF (Slot n)" aata hai (backtest close price par kharidta hai, isliye order 3:30 se pehle lagao). Dashboard ka "Record this buy" button form pehle se bhar deta hai. Agar 3:00-3:15 ke beech rank badal jaye to naya BUY alert aa sakta hai.
- **Suggested quantity**: slot ka paisa = us slot ki last sell ka proceeds (compounding), ya pehli baar `INITIAL_CAPITAL / N_SLOTS`. Apni asli capital ke hisaab se `INITIAL_CAPITAL` `scan.py` me badlo.
- **Position ko slot milta hai**: dashboard me Add position par slot chuno (ya Auto). Purani positions ko scan khud slot assign kar deta hai.
- **Cron UTC me hota hai**: 9:15 IST = 03:45 UTC. `scan.yml` me teen cron lines 9:15, 9:30 ... 15:15, 15:30 IST cover karti hain.
- **Holidays**: `nse_holidays.json` me 2026 bhara hai. Har saal December me agle saal ki dates add karo.
- **Target / trailing / breakeven / stop-loss** har scan me day ke High/Low se check hote hain.
- **DMA, EMA death cross, Rank #1, max-holding, late-hold EMA** closing price par chalte hain, isliye 2:30 PM ke baad wale scans me check hote hain (`CLOSE_RULES_FROM` in scan.py).
- **Data safety**: ranking me wo ETF skip hote hain jinka last bar 5 din se purana ho ya jisme ek din me 20% se zyada move dikhe (bad print), backtest ke cleaning jaisa.
- **Test alert**: dashboard Setup -> "Test an alert" me ab "BUY signal" bhi hai.
- Repo me roz ~26 commits banenge (har scan ka ek). Normal hai.
