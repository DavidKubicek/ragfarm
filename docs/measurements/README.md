# Naměřená data — A/B MoE vs. dense (srpen 2026)

Syrové výstupy měření, ze kterých vychází `docs/vyzkumna-zprava-2026-08-07.md`.
Každý soubor obsahuje **úplné znění všech odpovědí modelu**, ne jen skóre — právě
proto, že automatický vyhodnocovač se prokazatelně mýlí v obou směrech a každý
výsledek se ověřoval ručně.

| soubor | co obsahuje | platnost |
|---|---|---|
| `2026-08-07-bench-final-10iter.json` | **FINÁLNÍ měření.** 10 iterací, oba modely, produkční system prompt po všech opravách. Výkon + puzzly + práce s korpusem. | **Čísla ve zprávě pocházejí odtud.** |
| `2026-08-07-probe-k-moe-20runs.json` | 20 běhů MoE po opravě parametru `k` — kontrola rozptylu | doplňkové |
| `2026-08-07-probe-k-both-5iter.json` | první ověření po opravě `k`, oba modely, 5 iterací | historické |
| `2026-08-07-bench-fp8-5iter-prompt-stub.json` | měření **před** opravami promptu, s útržkovým system promptem | **NEPOUŽÍVAT jako výsledek** — je to stav „před", doloží rozsah zlepšení |

**Pozor na poslední řádek.** Ten soubor měří jinou konfiguraci, než jakou
provozujeme (chyběl produkční system prompt). Právě porovnání s ním ukazuje, že
většina rozdílu mezi architekturami byla způsobena našimi instrukcemi, ne
architekturou — viz kapitola 2 zprávy.

Skripty, které to vygenerovaly: `scripts/bench_ab.py`, `scripts/probe_k.py`.
