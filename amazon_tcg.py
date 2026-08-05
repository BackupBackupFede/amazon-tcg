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
# On ne retient QUE les displays (boîtes de boosters) et les cases (cartons de
# displays). Ni cartes à l'unité, ni ETB, ni coffrets/petits packs. Pour
# réélargir un jour : ajoute "etb" et/ou "coffret" ici.
KEEP_TIERS = ("display", "case")
ARB_TIERS  = KEEP_TIERS

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
    # Un display = 18/24/36 boosters. On exige un compte à 2 chiffres (10-36)
    # pour ne PAS confondre avec les mini-boîtes "4 boosters + 1 carte".
    ("display",  [r"\bdisplay\b", r"pr[eé]sentoir", r"booster\s*box", r"boosterbox",
                  r"bo[iî]te de (1[0-9]|2[0-9]|3[0-6])\s*boosters",
                  r"\b(1[0-9]|2[0-9]|3[0-6])\s*boosters\b", r"36\s*(pack|booster)"]),
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
    sleeve|prot[eè]ge|protection|protective|protector|schutz|classeur|binder|portfolio|
    toploader|deck\s*box|playmat|tapis|acryl|plexi|magnetic|vitrine|
    display\s*case|card\s*case|
    funko|puzzle|figurine|figuren|peluche|plush|porte[\s-]?cl[eé]|
    sticker|autocollant|checklane|ravensburger|play\s*['’]?\s*n['’]?\s|
    \(\s*[1-6]\s*boosters|
    \bsingle\b|carte\s*[àa]\s*l['’ ]?unit|lot\s*de\s*\d+\s*cartes|
    coffret\s*de\s*\d+\s*cartes|ultraboost|secr[eè]te|special\s*art|
    \b(?:op|eb|prb|st|ev|sv)\d{1,2}-\d{2,3}\b|          # n° de carte (OP06-119)
    japonais|japanese|giappones\w*|japanisch|japon[ée]s\w*|\(jp\)|\bjp\b|
    chinois|chinoise|chinese|cinese|chinesisch|version\s*ch\b|cor[ée]en|coreano|korean
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
                        if tier not in KEEP_TIERS:
                            continue
                        # prix courant = a-price NON barré ; prix barré = a-text-price
                        price = normalize_price(safe_get_text(item, [
                            "span.a-price:not(.a-text-price) span.a-offscreen",
                            "span.a-price span.a-offscreen"]))
                        if price is None or price < MIN_PRICE:
                            continue
                        old_price = normalize_price(safe_get_text(item, [
                            "span.a-price.a-text-price span.a-offscreen",
                            "span.a-price[data-a-strike='true'] span.a-offscreen"]))
                        promo = bool(old_price and old_price > price)
                        href = safe_get_attr(item, ["h2 a", "a.a-link-normal[href*='/dp/']"])
                        rows.append({
                            "market":   mkt,
                            "game":     game,
                            "set_code": extract_set_code(title) or "",
                            "tier":     tier,
                            "asin":     item.get_attribute("data-asin") or "",
                            "title":    title,
                            "price":    price,
                            "old_price": old_price if promo else "",
                            "promo":    promo,
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
FIELDS = ["market", "game", "set_code", "tier", "asin", "title", "price", "old_price", "promo", "url"]


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
                r["old_price"] = float(r["old_price"]) if r.get("old_price") else None
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
    """Rapport local façon LEGO : grand tableau de TOUTES les offres, triable,
    deals cross-marché surlignés (pas filtrés). Case 'deals seulement' optionnelle."""
    import html as _html
    deals = find_deals(rows)                       # pour l'alerte Telegram
    promos = sum(1 for r in rows if r["promo"] and r["price"])

    # Écart cross-marché par offre = médiane des AUTRES marchés du même set+tier
    groups = {}
    for r in rows:
        if r["set_code"] and r["tier"] in ARB_TIERS and r["price"]:
            groups.setdefault((r["game"], r["set_code"], r["tier"]), []).append(r)
    info = {}
    for offers in groups.values():
        if len({o["market"] for o in offers}) < 2:
            continue
        for o in offers:
            others = [x["price"] for x in offers if x["market"] != o["market"]]
            if not others:
                continue
            ref = median(others)
            spread = ref - o["price"]
            info[id(o)] = (ref, (100 * spread / ref) if ref else 0,
                           bool(spread >= ARB_MIN_EUR and ref and spread / ref >= ARB_MIN_PCT))

    rows_sorted = sorted(rows, key=lambda r: (
        r["game"], r["set_code"] or "zzz", r["tier"], r["price"] or 1e9))

    tbody = ""
    for r in rows_sorted:
        ref, gap, is_deal = info.get(id(r), (None, None, False))
        price = r["price"] or 0
        price_txt = f"{price:.2f}€" if r["price"] else "—"
        ref_txt = f"{ref:.0f}€" if ref else "—"
        if gap is None:
            gap_cell, gap_v = "—", 0
        elif gap >= 0:
            gap_cell, gap_v = f'<b style="color:#2ecc71">−{gap:.0f}%</b>', gap
        else:
            gap_cell, gap_v = f'<span class="dim">+{abs(gap):.0f}%</span>', gap
        if r["promo"] and r.get("old_price") and r["price"]:
            disc = round(100 * (r["old_price"] - r["price"]) / r["old_price"])
            promo_cell = f'<b style="color:#f5a623">🏷️ −{disc}%</b> <span class="dim">{r["old_price"]:.0f}€</span>'
            promo_v = disc
        else:
            promo_cell, promo_v = "", 0
        title = _html.escape(r["title"] or "")[:72]
        url = _html.escape(r["url"] or "")
        tbody += (
            f'<tr data-deal="{1 if is_deal else 0}"{" class=deal" if is_deal else ""}>'
            f'<td><b>{r["set_code"] or "—"}</b></td>'
            f'<td>{title}</td>'
            f'<td class="dim">{r["game"]}</td>'
            f'<td class="dim">Amazon {r["market"]}</td>'
            f'<td class="price" data-v="{price}">{price_txt}</td>'
            f'<td class="dim" data-v="{ref or 0}">{ref_txt}</td>'
            f'<td data-v="{gap_v}">{gap_cell}</td>'
            f'<td data-v="{promo_v}">{promo_cell}</td>'
            f'<td><a href="{url}" target="_blank">Shop →</a></td></tr>')
    if not tbody:
        tbody = '<tr><td colspan=9 class="dim">Aucune offre — lance un scrape.</td></tr>'

    heads = [("Set", 0), ("Produit", 0), ("Jeu", 0), ("Marché", 0), ("Prix", 1),
             ("Réf", 1), ("vs marché", 1), ("Promo", 0), ("Lien", 0)]
    ths = "".join(f'<th onclick="sortBy({i},{num})">{name} ⇅</th>'
                  for i, (name, num) in enumerate(heads))
    now = time.strftime("%d/%m/%Y %H:%M")

    css = """<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;background:#1a1a2e;color:#e0e0e0;padding:20px;margin:0}
.wrap{max-width:1250px;margin:auto}
h2{color:#f5a623;margin:0 0 4px 0}
.meta{color:#888;font-size:12px;margin-bottom:14px}
.controls{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
input#search{background:#0f3460;color:#eee;border:1px solid #444;padding:6px 10px;border-radius:6px;font-size:13px;width:280px}
label{font-size:12px;color:#ccc;cursor:pointer;user-select:none}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;border-bottom:1px solid #1e2a45;text-align:left;font-size:13px}
th{background:#0f3460;color:#f5a623;position:sticky;top:0;cursor:pointer;white-space:nowrap}
tbody tr:hover{background:#152040}
tr.deal{background:#14231b}tr.deal:hover{background:#183524}
.price{font-weight:bold;color:#2ecc71}
.dim{color:#aaa;font-size:12px}
a{color:#f5a623;text-decoration:none}a:hover{text-decoration:underline}
</style>"""
    js = """<script>
function sortBy(c,num){var tb=document.querySelector('#t tbody'),rs=[].slice.call(tb.rows);
var asc=tb.getAttribute('data-c')==c&&tb.getAttribute('data-d')=='1';
tb.setAttribute('data-c',c);tb.setAttribute('data-d',asc?'0':'1');var dir=asc?-1:1;
rs.sort(function(a,b){var x=a.cells[c].getAttribute('data-v'),y=b.cells[c].getAttribute('data-v');
if(x==null)x=a.cells[c].innerText;if(y==null)y=b.cells[c].innerText;
return num?((parseFloat(x)||0)-(parseFloat(y)||0))*dir:(''+x).localeCompare(y)*dir;});
rs.forEach(function(r){tb.appendChild(r);});}
function flt(){var q=document.getElementById('search').value.toLowerCase(),only=document.getElementById('dealsOnly').checked,
rr=document.querySelectorAll('#t tbody tr');for(var i=0;i<rr.length;i++){var r=rr[i];
r.style.display=((r.innerText.toLowerCase().indexOf(q)>-1)&&(!only||r.getAttribute('data-deal')=='1'))?'':'none';}}
</script>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Amazon TCG Plans — {now}</title>{css}</head><body><div class="wrap">
<h2>🎴 Amazon TCG Plans</h2>
<div class="meta">{now} — {len(rows)} offres (One Piece + Pokémon, displays) · {len(deals)} deals cross-marché · {promos} en promo · triées par jeu / set / prix</div>
<div class="controls">
<input type="text" id="search" placeholder="🔍 Set, titre, jeu, marché..." oninput="flt()">
<label><input type="checkbox" id="dealsOnly" onchange="flt()"> 🔥 deals seulement</label>
</div>
<table id="t"><thead><tr>{ths}</tr></thead><tbody>{tbody}</tbody></table>
</div>{js}</body></html>"""
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[report] {len(rows)} offres ({len(deals)} deals) → {os.path.basename(OUTPUT_HTML)}")

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
