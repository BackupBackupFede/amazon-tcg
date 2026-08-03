# Amazon TCG Deal Finder (One Piece + Pokémon)

Scanner Amazon **FR / DE / ES / IT** pour le TCG scellé, sur le modèle de ton
scanner LEGO (Playwright headless). Trouve les **écarts de prix cross-marché** :
un display/ETB/coffret nettement moins cher dans un pays qu'ailleurs = plan
d'achat (c'est comme ça que tu as eu ton One Piece).

## Usage

```bash
pip install -r requirements.txt
python -m playwright install chromium

python run_amazon_tcg.py                 # FR/DE/ES/IT en parallèle + rapport
python run_amazon_tcg.py --report-only   # rapport depuis les CSV existants
python amazon_tcg.py --scraper amazon_de # un seul marché
```

Sorties : `amazon_tcg_<marché>_raw.csv` (par pays) + `index.html` (deals triés par
écart). Si `TELEGRAM_TOKEN_2` / `TELEGRAM_CHAT_ID_2` sont définis, les 10
meilleurs deals partent aussi sur Telegram (mêmes tokens que le bot PREORDER).

## Ce qu'il fait, précisément

- Cherche 6 requêtes scellé (One Piece + Pokémon display / booster box / ETB / coffret).
- **Déduit le jeu du titre** (Amazon pad avec Yu-Gi-Oh, Magic, Star Wars… → ignorés).
- Filtre : accessoires (sleeves, protections, vitrines), singles, versions **JP/CN/KR**.
- Normalise **code de set** (OP-15, SV07…) + **tier** (display/case/etb/coffret).
- Deal cross-marché si le moins cher est **≥12 % ET ≥12 €** sous la médiane des autres pays.

## ⚠️ GitHub Actions — à savoir avant de crier au loup

Le workflow `.github/workflows/amazon_tcg.yml` est prêt (bouton manuel +
2×/jour). **Mais** :

- **Amazon bloque agressivement les IP datacenter** (celles de GitHub Actions). Ton
  scanner LEGO marche car tu le lances de chez toi (IP résidentielle). Sur GHA, tu
  peux te manger un **captcha** → le run remonte `[BLOCKED] … skip` et ne trouve
  rien. Le code le détecte et n'échoue pas salement, mais tu n'auras pas de données.
- Dans mes tests depuis une IP datacenter, un scrape **léger est passé** (34 produits
  FR récupérés) — donc ça *peut* marcher, mais ce n'est **pas garanti** et ça peut
  devenir flaky sous charge. D'où la cadence volontairement basse (2×/jour).
- **Recommandation** : lance-le d'abord en **manuel** (Actions → Run workflow) pour
  voir s'il passe. S'il se fait bloquer régulièrement, reste sur du **local** (comme
  LEGO) ou ajoute un proxy résidentiel. Rien à changer au code dans un cas comme
  dans l'autre.
- Pour tourner sur GHA, ce dossier doit être **son propre dépôt git privé**
  (`git init` + remote), ou être ajouté à un dépôt existant. Réutilise les secrets
  Telegram du bot PREORDER.

## Limite honnête : Pokémon vs One Piece

Le matching cross-marché repose sur le **code de set**. **One Piece** l'affiche
(OP-15, OP-07…) → arbitrage fiable. **Pokémon** utilise souvent le **nom** localisé
(« Écarlate & Violet », « Méga-Évolution ») sans code dans le titre Amazon → moins
de matchs cross-marché. Les produits Pokémon distribués par **Bandai** (codes SV06,
SV07…) matchent bien ; les autres apparaissent au catalogue mais génèrent moins de
deals automatiques. Pour Pokémon, le rapport reste utile en lecture directe (prix
par marché) même sans match auto.
