"""
run_amazon_tcg.py — Lanceur parallèle Amazon TCG (mirroir de run_lego.py).
Lance les 4 marchés Amazon en parallèle puis génère le rapport.

    python run_amazon_tcg.py                       # FR/DE/ES/IT + rapport
    python run_amazon_tcg.py --report-only         # rapport seul (CSV existants)
    python run_amazon_tcg.py --scrapers amazon_fr amazon_de
"""
import subprocess, sys, argparse, time
from pathlib import Path

SCRIPT = Path(__file__).parent / "amazon_tcg.py"
ALL = ["amazon_fr", "amazon_de", "amazon_es", "amazon_it"]


def run_parallel(scrapers):
    procs = [(s, subprocess.Popen([sys.executable, str(SCRIPT), "--scraper", s]))
             for s in scrapers]
    ok = True
    for s, p in procs:
        p.wait()
        print(f"[Launcher] {'✅' if p.returncode == 0 else '❌'} {s} (code {p.returncode})")
        ok &= p.returncode == 0
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--scrapers", nargs="+", choices=ALL, default=ALL)
    args = ap.parse_args()

    if not args.report_only:
        t0 = time.time()
        if not run_parallel(args.scrapers):
            print("[Launcher] ⚠️ Certains marchés ont échoué — rapport quand même.")
        print(f"[Launcher] Scraping terminé en {round(time.time()-t0,1)}s")

    print("[Launcher] ▶ Génération du rapport...")
    r = subprocess.run([sys.executable, str(SCRIPT), "--scraper", "report"])
    print("[Launcher] ✅ Rapport → index.html" if r.returncode == 0 else "[Launcher] ❌ Erreur rapport")


if __name__ == "__main__":
    main()
