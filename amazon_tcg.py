"""
amazon_tcg.py — Amazon TCG Deal Finder (One Piece + Pokémon)

Réplique la méthode du scanner LEGO (Playwright headless, Amazon FR/DE/ES/IT),
adaptée au TCG scellé. Un "plan" = un produit dont le prix, dans un marché, est
nettement sous la médiane des autres marchés (arbitrage cross-Amazon) — ou en
promo (prix barré).

Pipeline : scrape Amazon FR/DE/ES/IT (One Piece + Pokémon scellé)
→ normalisation (code de set + tier + langue) → détection de deals cross-marché
→ index.html (trié par écart) + alerte Telegram optionnelle.

CLI (mirroir de run_lego) :
    python amazon_tcg.py --scraper amazon_fr        # un marché
    python amazon_tcg.py --scraper report           # rapport depuis les CSV
"""
from playwright.sync_api import sync_playwright
import re, csv, time, random, os, argparse
from statistics import median

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── CONFIG ───────────────────────────────────────────────────────────────────
MARKETS = {
    "FR": {"base_url": "https://www.amazon.fr", "locale": "fr-FR"},
    "DE": {"base_url": "https://www.amazon.de", "locale": "de-DE"},
    "ES": {"base_url": "https://www.amazon.es", "locale": "es-ES"},
    "IT": {"base_url": "https://www.amazon.it", "locale": "it-IT"},
}

# (requête de recherche, jeu). Requêtes volontairement scellé-only.
QUERIES = [
    ("one piece card game display", "One Piece"),
    ("one piece booster box",       "One Piece"),
    ("pokemon display",             "Pokemon"),
    ("pokemon booster box",         "Pokemon"),
    ("pokemon elite trainer box",   "Pokemon"),
    ("pokemon coffret dresseur",    "Pokemon"),
]

MAX_PAGES = int(os.getenv("MAX_PAGES", "4"))
HEADLESS  = os.getenv("HEADLESS", "1") != "0"
MIN_PRICE = 20.0     # sous ce prix : boosters à l'unité / accessoires

# Deal cross-marché : le moins cher doit être nettement sous la médiane des autres
ARB_MIN_PCT = 0.12
ARB_MIN_EUR = 12
ARB_TIERS   = ("display", "case", "etb", "coffret")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MARKET_CSV = {m: os.path.join(BASE_DIR, f"amazon_tcg_{m.lower()}_raw.csv") for m in MARKETS}
OUTPUT_HTML = os.path.join(BASE_DIR, "index.html")

# ─── NORMALISATION TCG (repris de PREORDER) ──────────────────────────────────
SET_CODE_RE = re.compile(r"\b(OP|EB|PRB|ST|EV|ME|SV|SVP)[\s\-]?(\d{1,3})\b", re.I)

# Ordre = spécificité décroissante. ETB et coffret AVANT display : un "Coffret
# Dresseur d'élite (9 boosters)" ne doit pas être happé par la règle display à
# cause du décompte de boosters.
TIER_RULES = [
    ("case",     [r"\bcase\b", r"\bcarton\b"]),
    ("etb",      [r"\betb\b", r"elite trainer", r"dresseur d['’ ]?[eé]lite",
                  r"top[\s-]?trainer", r"allenatore"]),
    ("coffret",  [r"\bcoffret\b", r"\bbundle\b", r"\btin\b", r"collection box",
                  r"premium collection", r"lot de \d+\s*boosters"]),
    ("display",  [r"\bdisplay\b", r"bo[iî]te de \d+\s*boosters", r"\b\d+\s*boosters\b",
                  r"booster\s*box", r"boosterbox", r"36\s*(pack|booster)",
                  r"pr[eé]sentoir"]),
    ("booster",  [r"\bbooster\b", r"\bsobre\b", r"\bbustina\b"]),
]

GAMES = {"One Piece": ("one piece",), "Pokemon": ("pokemon", "pokémon")}


def detect_game(title):
    """Déduit le jeu du TITRE (pas de la requête : Amazon pad avec d'autres
    TCG). None si ce n'est ni One Piece ni Pokémon → produit ignoré."""
    n = (title or "").lower()
    for game, keys in GAMES.items():
        if any(k in n for k in keys):
            return game
    return None


# Bruit : accessoires, singles, produits ouverts, versions JP/CN/KR
_NOISE_RE = re.compile(r"""(?ix)
    sleeve|prot[eè]ge|protection|protective|schutz|classeur|binder|portfolio|
    toploader|deck\s*box|playmat|tapis|acryl|plexi|magnetic|vitrine|
    display\s*case|card\s*case|
    \bsingle\b|carte\s*[àa]\s*l['’ ]?unit|lot\s*de\s*\d+\s*cartes|
    japonais|japanese|\(jp\)|\bjp\b|
    chinois|chinoise|chinese|version\s*ch\b|cor[ée]en|coreano|korean
""")

ALLOWED_LANGS = ["(en)", "- en", "_en"]


def is_english(name, game):
    n = (name or "").lower()
    if "english" in n or any(t in n for t in ALLOWED_LANGS):
        return True
    if re.search(r"\((?:jp|jap|kr)\)|\bjp\b|japan|japon|korean|cor[ée]en", n):
        return False
    if "(fr)" in n or "- fr" in n:
        return False
    # Token "FR" nu exclu pour One Piece seulement (OP = EN voulu)
    if game == "One Piece" and re.search(r"\bfr\b", n):
        return False
    return True


def extract_set_code(name):
    m = SET_CODE_RE.search(name or "")
    return f"{m.group(1).upper()}{int(m.group(2)):02d}" if m else None


def classify_tier(name):
    n = (name or "").lower()
    for tier, patterns in TIER_RULES:
        if any(re.search(p, n) for p in patterns):
            return tier
    return "other"


def is_noise(title, game):
    if not title:
        return True
    if _NOISE_RE.search(title):
        return True
    return not is_english(title, game)


# ─── HELPERS PLAYWRIGHT (repris de LEGO_v2) ──────────────────────────────────
def safe_get_text(item, selectors, timeout=2000):
    for sel in selectors:
        try:
            loc = item.locator(sel)
            if loc.count() > 0:
                return loc.first.inner_text(timeout=timeout).strip()
        except Exception:
            continue
    return None


def safe_get_attr(item, selectors, attr="href"):
    for sel in selectors:
        try:
            loc = item.locator(sel)
            if loc.count() > 0:
                return loc.first.get_attribute(attr)
        except Exception:
            continue
    return None


def normalize_price(price_str):
    if not price_str:
        return None
    s = price_str.replace("€", "").replace("£", "").replace("$", "")
    s = s.replace("\xa0", "").replace(" ", "").replace(" ", "").replace(",", ".")
    # garde le dernier point comme décimale si plusieurs (1.299.95 -> 1299.95)
    if s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def has_strike(item):
    try:
        return item.locator("span.a-price[data-a-strike='true']").count() > 0
    except Exception:
        return False


def looks_blocked(page):
    try:
        t = (page.title() or "").lower()
        if "sorry" in t or "robot" in t:
            return True
        body = page.locator("form[action*='validateCaptcha'], #captchacharacters")
        return body.count() > 0
    except Exception:
        return False


# ─── SCRAPER AMAZON ──────────────────────────────────────────────────────────
def scrape_amazon(market: str, max_pages=MAX_PAGES) -> list[dict]:
    mkt = market.upper()
    cfg = MARKETS[mkt]
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale=cfg["locale"], user_agent=UA,
                                  viewport={"width": 1360, "height": 900})
        page = ctx.new_page()
        for query, game in QUERIES:
            url = f"{cfg['base_url']}/s?k={query.replace(' ', '+')}"
            print(f"[Amazon {mkt}] {game} · {query}", flush=True)
            try:
                page.goto(url, timeout=60000)
            except Exception as e:
                print(f"  [WARN] {e}")
                continue
            if looks_blocked(page):
                print(f"  [BLOCKED] Amazon {mkt} sert un captcha (IP datacenter ?) — skip")
                continue
            try:
                if page.locator("#sp-cc-accept").count():
                    page.locator("#sp-cc-accept").first.click()
                    time.sleep(1)
            except Exception:
                pass

            for _ in range(1, max_pages + 1):
                try:
                    page.wait_for_selector("div.s-search-result", timeout=10000)
                except Exception:
                    pass
                items = page.locator("div.s-result-item[data-asin]")
                for i in range(items.count()):
                    try:
                        item = items.nth(i)
                        title = safe_get_text(item, ["h2 span", "h2"])
                        game = detect_game(title)          # jeu réel = celui du titre
                        if not game or is_noise(title, game):
                            continue
                        tier = classify_tier(title)
                        if tier == "other" or tier == "booster":
                            continue
                        price = normalize_price(
                            safe_get_text(item, ["span.a-price span.a-offscreen"]))
                        if price is None or price < MIN_PRICE:
                            continue
                        href = safe_get_attr(item, ["h2 a", "a.a-link-normal[href*='/dp/']"])
                        rows.append({
                            "market":   mkt,
                            "game":     game,
                            "set_code": extract_set_code(title) or "",
                            "tier":     tier,
                            "asin":     item.get_attribute("data-asin") or "",
                            "title":    title,
                            "price":    price,
                            "promo":    has_strike(item),
                            "url":      cfg["base_url"] + href if href else "",
                        })
                    except Exception:
                        continue
                nxt = page.locator("a.s-pagination-next")
                if nxt.count() > 0 and not nxt.first.is_disabled():
                    nxt.first.click()
                    time.sleep(random.uniform(2.5, 4.5))
                else:
                    break
            time.sleep(random.uniform(2.5, 5.0))
        ctx.close()
        browser.close()

    # dédup par ASIN
    uniq = {r["asin"] or r["title"]: r for r in rows}
    out = list(uniq.values())
    print(f"[Amazon {mkt}] {len(out)} produits scellés retenus", flush=True)
    return out


# ─── I/O ─────────────────────────────────────────────────────────────────────
FIELDS = ["market", "game", "set_code", "tier", "asin", "title", "price", "promo", "url"]


def save_raw(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"[csv] {len(rows)} → {os.path.basename(path)}")


def load_all():
    rows = []
    for m, path in MARKET_CSV.items():
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["price"] = float(r["price"]) if r["price"] else None
                r["promo"] = r["promo"] in ("True", "true", "1")
                rows.append(r)
    return rows


# ─── DÉTECTION DE DEALS CROSS-MARCHÉ ─────────────────────────────────────────
def find_deals(rows):
    groups = {}
    for r in rows:
        if not r["set_code"] or r["tier"] not in ARB_TIERS or not r["price"]:
            continue
        groups.setdefault((r["game"], r["set_code"], r["tier"]), []).append(r)

    deals = []
    for (game, code, tier), offers in groups.items():
        if len({o["market"] for o in offers}) < 2:
            continue
        offers.sort(key=lambda o: o["price"])
        best = offers[0]
        others = [o["price"] for o in offers if o["market"] != best["market"]]
        if not others:
            continue
        ref = median(others)
        spread = ref - best["price"]
        if spread < ARB_MIN_EUR or spread / ref < ARB_MIN_PCT:
            continue
        deals.append({**best, "ref_price": round(ref, 2),
                      "spread_eur": round(spread, 2),
                      "spread_pct": round(100 * spread / ref),
                      "compare": " · ".join(f"{o['market']} {o['price']:.2f}€"
                                            for o in offers[:5])})
    deals.sort(key=lambda d: -d["spread_pct"])
    return deals


# ─── RAPPORT ─────────────────────────────────────────────────────────────────
def generate_report(rows):
    deals = find_deals(rows)
    promos = [r for r in rows if r["promo"] and r["price"]]
    lignes = []
    for d in deals:
        lignes.append(
            f"<tr class='deal'><td>{d['game']}</td><td>{d['set_code']}</td><td>{d['tier']}</td>"
            f"<td><b>{d['market']} {d['price']:.2f}€</b></td><td>−{d['spread_pct']}% "
            f"(méd. {d['ref_price']:.2f}€)</td><td>{d['compare']}</td>"
            f"<td><a href='{d['url']}' target='_blank'>{d['title'][:70]}</a></td></tr>")
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Amazon TCG — Deals cross-marché</title><style>
body{{font-family:system-ui,sans-serif;margin:1.2rem;background:#0f1115;color:#e6e6e6}}
h1{{font-size:1.3rem}} .sub{{color:#9aa}} table{{border-collapse:collapse;width:100%;margin-top:1rem}}
td,th{{border-bottom:1px solid #2a2f3a;padding:.45rem .6rem;font-size:.9rem;text-align:left}}
tr.deal td:nth-child(4){{color:#7CFC98}} a{{color:#7db3ff;text-decoration:none}}
input{{padding:.4rem;margin-top:.6rem;width:100%;background:#171a21;border:1px solid #2a2f3a;color:#e6e6e6}}
</style></head><body>
<h1>Amazon TCG — arbitrage cross-marché (One Piece + Pokémon)</h1>
<div class="sub">{len(rows)} produits scellés · {len(deals)} deals cross-marché · {len(promos)} en promo · maj {time.strftime('%Y-%m-%d %H:%M')}</div>
<input id="q" placeholder="🔍 filtrer (set, jeu, marché…)" oninput="flt()">
<table id="t"><thead><tr><th>Jeu</th><th>Set</th><th>Tier</th><th>Moins cher</th>
<th>Écart</th><th>Comparatif marchés</th><th>Produit</th></tr></thead>
<tbody>{''.join(lignes) or '<tr><td colspan=7>Aucun deal cross-marché ce run.</td></tr>'}</tbody></table>
<script>function flt(){{let q=document.getElementById('q').value.toLowerCase();
for(let r of document.querySelectorAll('#t tbody tr'))r.style.display=r.innerText.toLowerCase().includes(q)?'':'none';}}</script>
</body></html>"""
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[report] {len(deals)} deals → {os.path.basename(OUTPUT_HTML)}")

    # Alerte Telegram optionnelle (réutilise les tokens du bot PREORDER)
    token, chat = os.getenv("TELEGRAM_TOKEN_2"), os.getenv("TELEGRAM_CHAT_ID_2")
    if deals and token and chat:
        import requests
        msg = ["🛒 <b>AMAZON TCG — deals cross-marché</b>"]
        for d in deals[:10]:
            msg.append(f"\n<b>{d['game']} {d['set_code']} ({d['tier']}) −{d['spread_pct']}%</b>\n"
                       f"💰 {d['market']} <b>{d['price']:.2f}€</b> (méd. {d['ref_price']:.2f}€)\n"
                       f"📊 {d['compare']}\n🔗 {d['url']}")
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": "\n".join(msg),
                                "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=10).raise_for_status()
            print(f"[telegram] {min(len(deals),10)} deal(s) envoyé(s)")
        except Exception as e:
            print(f"[telegram] échec: {e}")


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Amazon TCG Deal Finder")
    ap.add_argument("--scraper", required=True,
                    choices=["amazon_fr", "amazon_de", "amazon_es", "amazon_it", "report"])
    args = ap.parse_args()

    if args.scraper == "report":
        generate_report(load_all())
        return
    mkt = args.scraper.split("_")[1].upper()
    rows = scrape_amazon(mkt)
    save_raw(rows, MARKET_CSV[mkt])


if __name__ == "__main__":
    main()
