# ragfarm — A/B test MoE vs. dense, slots a nová správa modelů

**Datum:** 7. 8. 2026 · **Hardware:** NVIDIA DGX Spark (GB10, 128 GB unified memory)

---

## Proč jsme to dělali

Chtěl jsem odpovědět na jednu konkrétní otázku: **vyplatí se nám architektura MoE
(Mixture-of-Experts — „směs expertů", model, kde se pro každý token aktivuje jen
malá část vah), nebo klasický dense model (hustý — počítá se přes všechny váhy)?**

Do té doby jsme na Sparku jeli MoE, protože je rychlý. Jenže rychlost není to, co
nás zajímá. Zajímá nás přesnost, dodržování system promptu (systémové instrukce,
která modelu určuje pravidla chování) a spolehlivost tool callingu (volání
nástrojů — schopnost modelu si sám vyžádat data z našeho korpusu). Rychlost je jen
podmínka použitelnosti, ne cíl.

Abych to mohl férově změřit, musel jsem nejdřív postavit infrastrukturu, která
umožní **mít dva modely v paměti současně a přepínat mezi nimi uprostřed
konverzace se stejným kontextem**. To je ta část, které říkám **slots** (sloty).
Vedlejší efekt: stejná mechanika nám v budoucnu umožní **delegaci mezi modely** —
levný rychlý model na rutinu, silný na složité úlohy.

---

## Thinking modely — co to je a proč nás zajímají

Používáme tzv. **thinking modely** („přemýšlející"). Fungují jednoduše: než model
napíše odpověď, vygeneruje si nejdřív **reasoning trace** (stopu uvažování) — text,
ve kterém si problém rozebere, zkouší varianty, opravuje se. Teprve pak napíše
finální odpověď. Ta stopa se uživateli standardně nezobrazuje; v našem rozhraní se
dá rozkliknout.

Technicky je to oddělené: vLLM (náš inference server — software, který model
provozuje a obsluhuje dotazy) nám tu stopu vrací ve zvláštním poli, takže se
nemíchá do odpovědi.

**Proč je to zásadní pro vývoj:** vidím přesně, kde model odbočil. Reálný příklad
z tohoto týdne — v našem system promptu bylo pravidlo, že každý vygenerovaný kód
se má otestovat v interpreteru. Interpreter ale umí jen Python. Když uživatel
chtěl JavaScript, model se v reasoning trace zacyklil na větě *„uživatel chce CSS
a JavaScript, což není Python. Počkat, to je problém."* Bez té stopy bych viděl
jen pomalou odpověď a hádal proč. S ní jsem to opravil za deset minut.

**Proč je to užitečné i pro běžného uživatele:** může si ověřit, *jak* model
k odpovědi došel. U infrastrukturních dotazů to znamená rozdíl mezi „model to
tvrdí" a „model to tvrdí a je vidět, ze kterého řádku tabulky to vzal". Pro
posouzení důvěryhodnosti odpovědi je to nejsilnější nástroj, který máme.

**Daň:** ta stopa jsou vygenerované tokeny (token — nejmenší jednotka textu, se
kterou model pracuje, zhruba slabika až slovo). Platí se za ně časem. U složitého
puzzle si model vygeneroval přes 16 000 znaků uvažování, než napsal tři věty
odpovědi. To je vidět ve všech časech níže.

---

## Co ty dva modely umí

Oba aktivní modely jsou **Qwen3-VL** — VL znamená vision-language, tedy rozumí
i obrazu, ne jen textu. Konkrétně:

- **Čtení obrázků a OCR** (rozpoznání textu v obraze) — účtenky, screenshoty,
  fotky tabulí. Text přepíše doslovně, včetně čísel a formátování.
- **Generování diagramů přímo v chatu**, ve dvou formátech:
  - **Mermaid** — textový zápis diagramu, který se v rozhraní rovnou vykreslí
    jako obrázek.
  - **draw.io** — plnohodnotný interaktivní diagram, ve kterém jde zoomovat,
    posouvat a přepínat vrstvy, přímo v okně chatu. Běží proti naší lokální kopii
    draw.io, takže funguje i bez internetu.
- **Převod diagramu na diagram** — pošlete screenshot ručně kresleného schématu
  a model ho překreslí do editovatelného formátu, případně rovnou upraví
  („přidej mezi retrieval a generování box pro reranker").
- **Generování kódu v mnoha jazycích** — Python, JavaScript, HTML/CSS, SQL, bash.
- **Spuštění a otestování Pythonu přímo v chatu.** U Pythonu model nejen kód
  napíše, ale rovnou ho pustí v izolovaném sandboxu na připravených testovacích
  případech, změří čas běhu a vypíše, co prošlo a co ne. U ostatních jazyků kód
  jen napíše — sandbox je pouze pythonovský, a nutit ho do JavaScriptu byla přesně
  ta chyba, kterou jsem tento týden opravil.

---

## Dopad na hardware: jeden model vs. dva

| | 1 model (dříve) | 2 modely (nyní) |
|---|---|---|
| vLLM instance | 1 | 2 (samostatné procesy) |
| GPU paměť LLM | ~59,5 GB | **39,1 + 47,3 = 86,4 GB** |
| + embedder | 1,6 GB | 1,6 GB |
| + reranker | 0,9 GB | 0,9 GB |
| **celkem obsazeno** | ~62 GB ze 121 | **~89 GB ze 121** |
| volné | ~59 GB | ~32 GB |
| místo na disku | 18 GB | 30,1 + 33,1 = 63,2 GB |

**Klíčové zjištění:** parametr, kterým se vLLM přiděluje paměť, se počítá jako
podíl z **celé** GPU a každá instance si ho počítá **nezávisle na ostatních**. Dvě
instance s původním nastavením by se navzájem přerazily. Musel jsem napsat výpočet,
který rozpočet rozdělí a **aktivaci odmítne**, pokud by součet překročil strop.

Druhé zjištění, na které jsem narazil až ostrým během: **sloty se musí startovat
postupně, ne najednou.** vLLM si při startu měří volnou paměť; když startují dvě
instance zároveň, každá vidí, jak se jí ta druhá mění pod rukama, a spadne. Nyní
je to ošetřené v aktivačním skriptu.

---

## Benchmark 1: výkon

Oba modely jsou **oficiální Qwen, stejná kvantizace FP8** (quantization — komprese
vah modelu na menší přesnost; FP8 = 8bitová čísla). Jediná proměnná je
architektura. To je záměr — porovnávat jablka s jablky.

| Metrika | **MoE** Qwen3-VL-30B-A3B Thinking FP8 | **dense** Qwen3-VL-32B Thinking FP8 |
|---|---|---|
| Architektura | MoE, 128 expertů/vrstvu, 8 aktivních na token | dense, všech 32B aktivních |
| Celkem parametrů | 30 mld. (~3 mld. aktivních na token) | 32 mld. (32 mld. aktivních) |
| Velikost na disku | 30,1 GiB | 33,1 GiB |
| GPU paměť za běhu | 39,1 GB | 47,3 GB |
| **Decode** (generování odpovědi) | **53,1 tok/s** | **5,7 tok/s** |
| Prefill @ 1,7 tis. tokenů | 6 107 tok/s | 1 813 tok/s |
| Prefill @ 6,9 tis. tokenů | 6 957 tok/s | 1 630 tok/s |
| Prefill @ 20,7 tis. tokenů | 4 803 tok/s | 1 096 tok/s |
| **Studený start** (první spuštění, JIT kompilace jader) | ~7 min | ~9 min |
| **Teplý start** (běžný restart služby) | **117 s** | **248 s** |
| — z toho načtení vah z disku | 44,6 s | 141,2 s |
| — z toho inicializace enginu | 37,0 s | 68,4 s |

*Prefill = zpracování celého vstupu najednou (dotaz + kontext). Decode = generování
odpovědi token po tokenu. Prefill běží paralelně, decode sériově — proto ten řádový
rozdíl mezi nimi.*

*Studený start nastane jen jednou po instalaci nebo po aktualizaci — systém si při
něm kompiluje výpočetní jádra na míru našemu GPU a výsledek si uloží. Každý další
restart je už teplý; to je číslo, které platí v provozu.*

**Výsledek: MoE je 9,3× rychlejší na decode a 3,4–4,4× na prefill.** Je to
očekávané a je to přesně důvod, proč MoE architektura vznikla — u dense modelu se
pro každý jednotlivý token protáhne pamětí všech 32 miliard parametrů, u MoE jen
zhruba tři.

---

## Benchmark 2: inteligence

Tady mě zajímalo něco jiného než rychlost. Použil jsem dva známé logické puzzly,
každý **5× na každém modelu** — modely nejsou deterministické, jeden běh nic
nedokazuje (což mě v průběhu testování jednou spolehlivě zmátlo, viz poznámka na
konci).

**Zadání 1 — „tři krabice":** *Máš tři krabice. Jedna obsahuje jen jablka, druhá
jen pomeranče, třetí směs obojího. Všechny tři krabice jsou popsané, ale všechny
tři popisky jsou špatně. Kolik nejméně kusů ovoce musíš celkem vytáhnout, abys
spolehlivě určil obsah všech tří krabic?* — Správně: **1**

**Zadání 2 — „Monty Hall":** *Tři dveře, za jedněmi auto, za dvěma kozy. Vybereš
dveře č. 1. Moderátor, který ví, co je za dveřmi, otevře dveře č. 3 — je za nimi
koza. Nabídne ti změnit volbu na dveře č. 2. Máš přehodit, nebo zůstat?* —
Správně: **přehodit, pravděpodobnost 2/3**

| Test | **MoE** 30B-A3B FP8 | **dense** 32B FP8 |
|---|---|---|
| Tři krabice — úspěšnost | **5/5** | **5/5** |
| Tři krabice — průměrný čas | 100 s | 253 s |
| Tři krabice — délka uvažování | 16 805 znaků | 4 498 znaků |
| Monty Hall — úspěšnost | **5/5** | **5/5** |
| Monty Hall — průměrný čas | 35 s | 412 s |
| Monty Hall — délka uvažování | 4 206 znaků | 5 404 znaků |

**Na čisté logice je to remíza 10:10.** Oba modely obě úlohy zvládly pokaždé.
Všech dvacet odpovědí jsem přečetl ručně a ověřil, že automatický vyhodnocovač
skóroval správně — nespoléhal jsem na to, že hledání klíčových slov v textu je
totéž co správná odpověď.

Zajímavost: MoE potřeboval na „krabice" **3,7× delší uvažování** než dense
(16 805 vs. 4 498 znaků), a přesto došel ke stejnému výsledku. Dense přemýšlí
úsporněji.

---

## Benchmark 3: práce s naším korpusem — a tady se to láme

Tohle je test, který nejlépe odpovídá reálnému nasazení: dotaz, na který model
**musí** sáhnout do našich firemních dat. Znovu 5× na každém modelu.

**Zadání:** *Kdo je projektový manažer (PM) za firmu EPC? Uveď jen jeho, ne celý
projektový tým.* — Správně: **Marek Česal** (v tabulce má roli „PM – řízení
cut-over procesu"; ostatní jsou členové týmu s rolemi „Sítě", „Aplikace" apod.)

| Metrika | **MoE** 30B-A3B FP8 | **dense** 32B FP8 |
|---|---|---|
| **Správných odpovědí** | **2/5** | **4/5** |
| Věcně **chybných** odpovědí | **3** | **0** |
| Odmítnutí odpovědět | 0 | 1 |
| Nástroj vůbec zavolán | 5/5 | 5/5 |
| Průměrný čas celého kola | 32 s | 196 s |

### Co přesně MoE dělal špatně — je to mechanická chyba, ne „hloupost"

Ve všech třech chybných bězích poslal do vyhledávacího nástroje parametr `k=1`,
tedy *„vrať mi jediný záznam"*. Přepsal tím výchozí hodnotu 8. S jedním jediným
řádkem pak odpověděl, co v tom řádku bylo — a byl to špatný člověk („Petr Pyzsko",
který má v tabulce roli „Sítě"). V obou úspěšných bězích poslal `k=8` a odpověděl
správně.

- 3 chyby = 3× `k=1`
- 2 úspěchy = 2× `k=8`
- dense poslal `k=8` **ve všech pěti bězích**

Jinými slovy: **MoE si sám podřízl retrieval** (vyhledávání) tím, že špatně
nastavil parametr nástroje. Přesně tomu se říká spolehlivost tool callingu.

### Rozdíl ve *způsobu* selhání je důležitější než poměr úspěšnosti

MoE v chybných bězích **sebejistě jmenoval nesprávnou osobu**. Dense v jediném
neúspěšném běhu **odmítl odpovědět** se zdůvodněním, že mezi nalezenými záznamy
není nikdo označený jako PM. To je bezpečné selhání — uživatel dostane „nevím"
místo věrohodně vypadající nepravdy. V provozu je tenhle rozdíl podstatnější než
samotná statistika.

### Rozpad RAG pipeline (26 měřených dotazů)

RAG = Retrieval-Augmented Generation (generování obohacené o vyhledávání — model
nejdřív najde relevantní pasáže v našich datech a teprve pak odpovídá).

| Fáze | Čas | Co se děje |
|---|---|---|
| **embed** (převod dotazu na vektor) | 112 ms | dotaz se převede na číselný vektor, aby se dal porovnávat významově |
| **fuse** (sloučení výsledků) | 74 ms | sloučí se dvě paralelní větve hledání — významová a přesná na klíčová slova |
| **rerank** (přeřazení) | 300 ms | druhý, přesnější model přeskládá nalezené pasáže podle skutečné relevance |
| **expand** (rozšíření kontextu) | 0 ms | doplní sousední odstavce, pokud dávají smysl (u tabulkových řádků se neuplatní) |
| **celkem** | **486 ms** | |

Celý vyhledávací řetězec trvá **necelou půl sekundu** a **je pro oba modely
totožný** — není úzkým hrdlem. Rozdíl v celkovém čase odpovědi (32 s vs. 196 s)
jde téměř výhradně na vrub rychlosti generování.

---

## Závěr A/B testu — poctivě

Zadání znělo potvrdit, že dense vyhrává řádově. **Naměřená data to takto
nepotvrzují a bylo by nekorektní to tak napsat.** Co data ukazují:

| Kritérium | Vítěz | Rozdíl |
|---|---|---|
| Rychlost generování | **MoE** | 9,3× |
| Rychlost zpracování vstupu | **MoE** | 3,4–4,4× |
| Čistá logika (puzzly) | **remíza** | 10:10 obojí |
| Přesnost nad firemními daty | **dense** | 4/5 vs. 2/5 |
| **Věcně chybné odpovědi** | **dense** | **0 vs. 3** |
| Bezpečné selhání („nevím") | **dense** | odmítl místo výmyslu |
| Spolehlivost volání nástrojů | **dense** | 5/5 správný parametr vs. 2/5 |
| Spotřeba GPU paměti | **MoE** | 39 GB vs. 47 GB |

**Moje doporučení:** rozdíl není v inteligenci — v čisté logice jsou oba stejně
dobré. Rozdíl je v **disciplíně**: dense se drží zadaného postupu, a když si není
jistý, řekne to. MoE improvizuje s parametry nástroje a pak sebejistě odpoví
nesprávně. Pro nasazení, kde odpovědi vedou k rozhodnutím o infrastruktuře, je
**nula nesprávných odpovědí z pěti** silnější argument než devítinásobná rychlost.

**Otevřená otázka, kterou chci doměřit:** ta chyba MoE s `k=1` může být opravitelná
úpravou system promptu (explicitní zákaz snižovat `k`). Pokud ano, poměr sil se
může výrazně změnit ve prospěch MoE, protože rychlostní náskok má obrovský.
Doporučuji to zkusit dřív, než uděláme definitivní rozhodnutí.

**Poznámka k metodice:** během testování jsem jednou vyhodnotil jediný běh jako
průkazný a málem z toho udělal závěr. Opakování 5× ukázalo, že to byl šum. Všechny
výsledky v tomto dokumentu jsou z pěti běhů a všechny odpovědi jsem ručně
zkontroloval, protože automatický vyhodnocovač může nesprávně ohodnotit
parafrázovanou, ale správnou odpověď.

---

## Co jsem kvůli tomu musel přestavět

### Nová správa modelů

Původní skripty pro stahování a přepínání modelů byly psané pro starý formát
z předchozího hardwaru a na Sparku byly nepoužitelné. Napsal jsem je znovu:

| Bylo | Je | Co to řeší |
|---|---|---|
| `fetch-llm.sh` | **`fetch_llm.py`** | stahování řízené registrem, ověření po bajtech |
| `activate-llm.sh` | **`activate_llm.py`** | přiřazení modelu do slotu + výpočet paměti |
| — | **`models/llm/active.json`** | jediný zdroj pravdy, verzovaný v gitu |

**`active.json`** je krátký soubor se seznamem všech modelů, které má nasazení mít,
a s informací, které z nich jsou právě aktivní ve kterém slotu. Repozitář se tím dá
dodat **předkonfigurovaný** — na novém stroji stačí jeden příkaz a stáhne se přesně
ta sada modelů, která tam patří.

Dvě věci, které jsem musel vyřešit a stály za to:

1. **Stahování přes standardní knihovnu nefungovalo.** Při výpadcích sítě se
   zaseklo na mrtvém spojení a nevrátilo řízení — proces žil, přenos stál na
   0 B/s a nikdy se nezotavil. Přepsal jsem přenos na `curl` s detekcí zamrznutí
   a navázáním na rozdělaný soubor. Tou cestou prošlo 67 GB přes několik výpadků.
2. **Kontrola integrity.** Po každém stažení se velikost každého souboru porovná
   proti zdroji. Aktuálně: **6 modelů, 0 chybějících, 0 nesouhlasících.**

### Sloty

vLLM umí obsloužit **jeden model na jeden proces** — víc modelů najednou neumí.
Slot je tedy naše abstrakce nad jednou instancí: vlastní port, vlastní konfigurace,
vlastní rozpočet paměti. Prakticky to znamená, že v rozhraní vidím oba modely a
**můžu mezi nimi přepnout uprostřed rozhovoru**, aniž bych přišel o kontext. Pro
srovnávací testování je to zásadní — položím stejnou otázku oběma a rozdíl vidím
okamžitě.

### Dokumentace

- **ADR-0013** (architektonické rozhodnutí o inference vrstvě) — nová sekce §2a
  popisuje registr modelů, sloty, vzorec pro rozpočet paměti a proč je to **třetí
  nezávislý paměťový rozpočet** vedle dvou, které jsme znali.
- **`docs/deployment.md`** — nová sekce o slotech s naměřenými čísly a popis nové
  správy modelů.
- **`models/llm/MODEL.md`** — už to není záznam o jednom modelu, ale o celé sadě.
- **`BUILD_STATE.md`** — sestavovací krok pro inference vrstvu nyní používá registr.
- **Oprava system promptu** — pravidlo o testování kódu platí nově jen pro Python.

---

## Kompletní topologie služeb

Systém není jeden program, ale síť spolupracujících služeb. Tabulka je hlavně o tom,
**co která služba dělá a proč existuje**.

| Služba | Adresa | Co to je a co dělá |
|---|---|---|
| **vLLM slot 0** | `127.0.0.1:8080` | Inference server s prvním jazykovým modelem (nyní MoE 30B-A3B Thinking FP8). Přijímá dotazy v OpenAI-kompatibilním formátu — díky tomu je zbytek systému na konkrétním modelu **nezávislý** a výměna modelu se nedotkne ničeho jiného. Zpracuje kontext, vygeneruje uvažování i odpověď, a když usoudí, že potřebuje data, vrátí místo odpovědi požadavek na zavolání nástroje. |
| **vLLM slot 1** | `127.0.0.1:8082` | Totéž s druhým modelem (nyní dense 32B Thinking FP8). Samostatný proces, samostatný rozpočet GPU paměti. Existuje kvůli A/B srovnání a do budoucna kvůli delegaci úloh mezi modely. |
| **Reranker** | `127.0.0.1:8081` | Druhý, menší a specializovaný model (cross-encoder). Nevytváří text — jen bere dvojice (dotaz, nalezená pasáž) a přiděluje jim skóre relevance. Vyhledávání vrátí zhruba čtyřicet kandidátů, reranker je přeskládá a odřízne balast. Je to nejdražší fáze vyhledávání (300 ms), ale právě on rozhoduje, jestli model dostane k odpovědi ty správné podklady. |
| **Embedder** | `127.0.0.1:8090` | Převádí text na číselné vektory, a to dvěma způsoby zároveň: **dense** vektor zachycuje význam (najde „jak se přihlásím", i když v textu stojí „přístup přes proxy"), **sparse** vektor zachycuje konkrétní slova (najde přesně `hsmbvxip001ts`). Ta kombinace je důvod, proč systém zvládá obojí — hledání podle smyslu i podle přesného identifikátoru. Používá se při vkládání dat i při každém dotazu. |
| **Qdrant** | `127.0.0.1:6333` | Vektorová databáze. Uchovává korpus rozsekaný na pasáže, ke každé oba typy vektorů plus původní text a informaci o zdroji. Umí hledat podle podobnosti vektorů, což je pro počítač jediný způsob, jak hledat „podle významu". Aktuálně 183 pasáží z 5 firemních dokumentů. |
| **rag-retrieval** | `127.0.0.1:8104` | Mozek vyhledávání a jediný nástroj, který model reálně používá (`search_corpus`). Řídí celý řetězec: dotaz → embedder → dvě paralelní větve hledání v Qdrantu → sloučení → reranker → odříznutí slabých výsledků → doplnění sousedního kontextu → vrácení **doslovného** textu i s uvedením zdroje. To „doslovného" je podstatné: model nedostane parafrázi, ale původní řádek, takže může citovat přesně. |
| **mcpo** | `127.0.0.1:8000` | Překladová brána mezi světem nástrojů a rozhraním. Interní služby mluví protokolem MCP, chatovací rozhraní umí OpenAPI — mcpo mezi tím překládá a všechny nástroje vystavuje na jednom místě. Přidání nového nástroje je pak otázka konfigurace, ne zásahu do kódu. |
| **mcp-placement** | `127.0.0.1:8101` | Nástroj pro dotazy do virtualizační platformy OpenNebula — „na kterém fyzickém stroji běží tenhle virtuál" a „co všechno běží na tomhle stroji". Aktuálně v mock režimu (vrací testovací data), protože zatím nemáme přístup k produkčnímu clusteru. |
| **mcp-host-control** | `127.0.0.1:8102` | Jediná služba, která smí **něco změnit** — restartovat hypervizor. Proto má tři nezávislé pojistky: výchozí režim je „jen ukaž, co bys udělal", cíl musí být na schváleném seznamu, a operace vyžaduje potvrzení člověkem v dialogu. Model tedy nemůže restartovat stroj sám od sebe; může pouze připravit návrh, který schvaluje člověk. |
| **mcp-fs** | `127.0.0.1:8103` | Experimentální čtení souborů v izolovaném adresáři. Záměrně **není** připojen k rozhraní — model se k němu nedostane. |
| **Open WebUI** | `0.0.0.0:3000` | Chatovací rozhraní. Jediná služba dostupná ze sítě, chráněná přihlášením. Drží historii konverzací, presety modelů (systémový prompt, parametry, povolené nástroje), vykresluje diagramy a spouští kód. Tady se přepíná mezi sloty uprostřed rozhovoru. |
| **drawio-viewer** | `0.0.0.0:80` | Lokální kopie editoru draw.io. Existuje proto, aby se diagramy vykreslovaly v chatu **bez připojení k internetu** — na uzavřené síti nebo při prezentaci. |
| **ingester (watcher)** | — | Na pozadí hlídá adresář s dokumenty. Když se soubor změní, sám ho zpracuje, rozseká na pasáže a aktualizuje databázi. Rozpoznává XLSX (každý řádek tabulky = samostatný záznam se zachovanými názvy sloupců — proto model umí odpovídat na dotazy typu „kdo má roli PM"), DOCX i PDF. |

---

## Slovníček pojmů

| Termín | Vysvětlení |
|---|---|
| **LLM** (Large Language Model) | Velký jazykový model. Neuronová síť natrénovaná na obrovském množství textu, která umí předpovídat pokračování textu — a tím i odpovídat, shrnovat, psát kód. [wiki](https://cs.wikipedia.org/wiki/Velk%C3%BD_jazykov%C3%BD_model) |
| **Token** | Nejmenší jednotka textu, se kterou model pracuje — zhruba slabika až krátké slovo. Rychlost modelu se měří v tokenech za sekundu. |
| **Dense model** | „Hustý" model. Pro každý token se počítá přes všechny parametry. Pomalejší, ale konzistentnější. |
| **MoE** (Mixture-of-Experts) | „Směs expertů". Model je rozdělen na mnoho malých specializovaných částí a pro každý token se aktivuje jen několik z nich. Řádově rychlejší při stejné velikosti. [wiki](https://en.wikipedia.org/wiki/Mixture_of_experts) |
| **Parametry / váhy** | Naučená čísla uvnitř modelu. „30B" znamená 30 miliard parametrů. Zhruba odpovídají tomu, kolik toho model „umí". |
| **Prefill** | Fáze, kdy model najednou zpracuje celý vstup (dotaz + kontext). Probíhá paralelně, proto je rychlá. |
| **Decode** | Fáze generování odpovědi, token po tokenu. Každý token závisí na předchozím, takže se nedá paralelizovat — proto je řádově pomalejší než prefill. |
| **Kvantizace** (quantization) | Komprese vah modelu na menší přesnost (FP8 = 8 bitů, NVFP4 = 4 bity). Model zabere méně paměti a běží rychleji za cenu malé ztráty přesnosti. [wiki](https://en.wikipedia.org/wiki/Quantization_(signal_processing)) |
| **Kontextové okno** | Kolik textu model „vidí" najednou — dotaz, historie konverzace i nalezené podklady. U nás 32 tisíc tokenů. |
| **Thinking model** | Model, který si před odpovědí vygeneruje vlastní úvahu. Zpomalí to odpověď, ale zlepší kvalitu u složitých úloh a umožní zkontrolovat postup. |
| **Reasoning trace** | Ta vygenerovaná úvaha. U nás se vrací odděleně od odpovědi, takže jde zobrazit nebo skrýt. |
| **System prompt** | Skryté instrukce, které model dostane před každou konverzací — pravidla chování, formát odpovědí, kdy použít nástroj. |
| **Tool calling** | Schopnost modelu vyžádat si zavolání externí funkce (např. vyhledání v databázi) místo toho, aby si odpověď vymyslel. [wiki](https://en.wikipedia.org/wiki/Large_language_model) |
| **RAG** (Retrieval-Augmented Generation) | Postup, kdy model nejdřív vyhledá relevantní pasáže ve firemních datech a odpovídá pouze z nich. Řeší zastaralost i vymýšlení. [wiki](https://en.wikipedia.org/wiki/Retrieval-augmented_generation) |
| **Embedding** | Převod textu na vektor čísel tak, že významově podobné texty mají blízké vektory. Základ vyhledávání podle smyslu. [wiki](https://en.wikipedia.org/wiki/Word_embedding) |
| **Dense vs. sparse vektor** | Dense zachycuje význam (najde i jinými slovy). Sparse zachycuje konkrétní slova (najde přesný identifikátor). Kombinace obojího = hybridní vyhledávání. |
| **Vektorová databáze** | Databáze, která umí rychle najít nejpodobnější vektory. U nás Qdrant. [wiki](https://en.wikipedia.org/wiki/Vector_database) |
| **Reranking** | Druhé kolo hodnocení nalezených výsledků přesnějším modelem. Vyhledání je rychlé a hrubé, reranking pomalý a přesný. |
| **Chunking** | Rozsekání dokumentů na menší pasáže před uložením. Způsob sekání přímo určuje, co půjde najít. |
| **Halucinace** | Když model sebejistě tvrdí něco, co není pravda. Hlavní riziko nasazení; RAG a citace zdrojů to omezují. [wiki](https://en.wikipedia.org/wiki/Hallucination_(artificial_intelligence)) |
| **Inference** | Samotné používání natrénovaného modelu (na rozdíl od trénování). |
| **Inference server** | Software, který model drží v paměti a obsluhuje dotazy. U nás vLLM. |
| **Vision-language model** (VL) | Model, který kromě textu rozumí i obrázkům. |
| **OCR** | Rozpoznání textu v obraze. [wiki](https://cs.wikipedia.org/wiki/Optick%C3%A9_rozpozn%C3%A1v%C3%A1n%C3%AD_znak%C5%AF) |
| **Sandbox** | Izolované prostředí pro bezpečné spuštění kódu, který napsal model. |
| **Unified memory** | Architektura, kde procesor a grafická karta sdílejí jednu fyzickou paměť. Výhoda: velké modely se vejdou. Nevýhoda: všechny služby soutěží o stejnou paměť i propustnost. |
