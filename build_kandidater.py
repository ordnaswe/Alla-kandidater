#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_kandidater.py  (v2 – stöder den utökade sajten med karta-per-parti och vallistor)
=======================================================================================
Hämtar Valmyndighetens kandidatur-CSV och bygger om <script id="DATA"> i
sveriges-kandidater.html så att sajten visar aktuell data.

Genererar hela DATA-blocket, inklusive de nya nycklarna i den utökade filen:
  P          per person [ålder, kön(0=M,1=K), partiindex, länindex,
                          valtyp-mask(1=RD,2=RF,4=KF), yrkeindex(-1)]
  srec       per person [namn, fkListindex, komNamesindex(-1), valbar-flagga]
  kfk        per person: kommunkod för personens KF-kandidatur ("" om ingen)
  lists      vallistor [valtyp, valkretsnamn, fk, valkretsbeteckning, N, mem]
                mem = [personindex, ordning, personindex, ordning, ...]
                N   = valbar gräns (mandat 2022) – se nedan
  kommun        {kod:{n,M,K,aSum,aCnt,n65}}  per kommun (KF, alla partier)
  kommunParty   {kod:{fk:{n,M,K,aSum,aCnt,n65}}} per kommun och riksdagsparti
  mapParties    de åtta riksdagspartierna (fast lista)
  + uppslagslistor och alla aggregat (för schema-paritet).

2022-BEROENDE som INTE finns i CSV:n – hanteras så här:
  * DATA.kommunValbar  : bevaras oförändrad från befintliga filen.
  * lists[..][4] = N   : "valbar gräns (mandat 2022)". Bevaras genom att matcha
                         varje ny lista mot den gamla på (valtyp, valkretsnamn, fk,
                         valkretsbeteckning). Hittas ingen match sätts N = -1
                         (då visas ingen valbar-gräns för just den listan).
  * srec[..][3]        : härleds konsekvent ur lists + N (personens placering ≤ N
                         på någon lista => valbar för det valet). Bitar enligt sidan:
                         1=kommun, 2=region, 4=riksdag. Saknas N blir flaggan 0/-1.
Eftersom 2022 års mandat är historiska och listidentiteten (område + parti) är
stabil, överlever valbar-gränserna en daglig ombyggnad via denna matchning.

GEO-blocket (kartans geografi) rörs aldrig.

ANTAGANDEN (ändras överst):
  * Endast rader med GILTIG = 'J' räknas.
  * En person = ett KANDIDATNUMMER; valtyp slås ihop per person.
  * Partier med >= PARTY_MIN_PERSONER personer listas separat, övriga som "Övriga".
  * Län per person härleds ur folkbokföringskommun via GEO.
  * Yrke = texten efter "ålder, " i VALSEDELSUPPGIFT, gemener.
"""

import sys, re, json, argparse, urllib.request, io, csv, collections, statistics
import os, unicodedata

DATA2022_FILE = os.environ.get('DATA2022_FILE', 'data2022.json')

def _norm_namn(x):
    x = unicodedata.normalize('NFKD', (x or '').lower())
    return re.sub(r'[^a-z ]', '', ''.join(c for c in x if not unicodedata.combining(c))).strip()


# ----------------------------- KONFIG ---------------------------------------
CSV_URL   = "https://data.val.se/filer/val2026/parti/kandidaturer.csv"
HTML_FILE = "sveriges-kandidater.html"
PARTY_MIN_PERSONER = 100
_RDRF = {}   # fylls i main() ur data2022.json
ONLY_GILTIG = True
MAP_PARTIES = ["S", "M", "SD", "C", "V", "KD", "MP", "L"]   # de åtta riksdagspartierna

LAN_CODE_NAME = [
    ("01", "Stockholms län"), ("03", "Uppsala län"), ("04", "Södermanlands län"),
    ("05", "Östergötlands län"), ("06", "Jönköpings län"), ("07", "Kronobergs län"),
    ("08", "Kalmar län"), ("09", "Gotlands län"), ("10", "Blekinge län"),
    ("12", "Skåne län"), ("13", "Hallands län"), ("14", "Västra Götalands län"),
    ("17", "Värmlands län"), ("18", "Örebro län"), ("19", "Västmanlands län"),
    ("20", "Dalarnas län"), ("21", "Gävleborgs län"), ("22", "Västernorrlands län"),
    ("23", "Jämtlands län"), ("24", "Västerbottens län"), ("25", "Norrbottens län"),
]
LAN_LIST = [name for _, name in LAN_CODE_NAME] + ["Okänd/övrig"]
LAN_OKAND = len(LAN_LIST) - 1
LAN_CODE_TO_IDX = {code: i for i, (code, _) in enumerate(LAN_CODE_NAME)}

VT_BIT = {"RD": 1, "RF": 2, "KF": 4}            # valtyp -> bit i P[4] och lists[..][0]
# srec[3]-flaggans bitar (sidans vbTag): 1=kommun, 2=region, 4=riksdag
VT_TO_SREC3 = {1: 4, 2: 2, 4: 1}                 # RD->riksdag(4), RF->region(2), KF->kommun(1)
AGE_LBL = ["18–29", "30–39", "40–49", "50–64", "65–74", "75+"]

def age_bucket(a):
    if a < 0:  return -1
    if a < 30: return 0
    if a < 40: return 1
    if a < 50: return 2
    if a < 65: return 3
    if a < 75: return 4
    return 5

# Extra ortnamn/geografiska ord som inte fångas av kommun-/länslistan.
# Lägg gärna till fler här om du ser orter som slinker igenom i yrkeslistan.
EXTRA_PLATSER = {
    "centrum", "city", "visby", "tätort", "kommun", "kommunen", "ort", "orten",
    "landsbygd", "landsbygden", "stad", "staden", "norr", "söder", "öster", "väster",
}

def _ar_alder(tok):
    # "71", "71 år", "71år"
    return re.fullmatch(r"\d+\s*(år)?", tok) is not None

def yrke_ur_valsedelsuppgift(s, platser=frozenset()):
    """Plocka ut yrket ur fritextfältet VALSEDELSUPPGIFT.
    Tar bort åldrar ('71 år') och geografiska namn (kommuner/län/orter)
    och returnerar första riktiga yrkesordet, i gemener. Tomt om inget finns.
    """
    s = (s or "").strip()
    if not s:
        return ""
    for del_ in s.split(","):
        p = del_.strip().strip(".").lower()
        # ta bort ev. inledande "NN år " som sitter ihop med yrket
        p = re.sub(r"^\d+\s*år\s+", "", p).strip()
        if not p:
            continue
        if _ar_alder(p):                 # ren ålder, t.ex. "71" eller "71 år"
            continue
        if p in platser:                 # kommun-/läns-/ortnamn
            continue
        return p
    return ""

def read_block(html, block_id):
    m = re.search(r'<script id="%s" type="application/json">(.*?)</script>' % block_id, html, re.S)
    if not m:
        sys.exit('Hittade inte <script id="%s"> i HTML-filen.' % block_id)
    return m.group(1), m.span(1)

def build_geo_maps(geo):
    kom_names, name_to_idx, code_to_lanidx, code_order, code_to_name = [], {}, {}, [], {}
    for f in geo["features"]:
        code, name, lan = f["c"], f["n"], f.get("lan", "")
        code_order.append(code); code_to_name[code] = name
        if name not in name_to_idx:
            name_to_idx[name] = len(kom_names); kom_names.append(name)
        code_to_lanidx[code] = LAN_CODE_TO_IDX.get(lan, LAN_OKAND)
    return kom_names, name_to_idx, code_to_lanidx, code_order, code_to_name

def fetch_csv_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "kandidater-build/2.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
    return raw.decode("utf-8-sig")

def to_int(x, default=-1):
    try:
        return int(str(x).strip())
    except (ValueError, TypeError):
        return default

# ------------------------------ huvudlogik ----------------------------------
def build_data(csv_text, geo, old_data):
    kom_names, kom_name_to_idx, code_to_lanidx, code_order, code_to_name = build_geo_maps(geo)
    geo_name_to_lanidx = {code_to_name[c]: code_to_lanidx[c] for c in code_order}
    platser = {n.lower() for n in kom_names} | {l.lower() for l in LAN_LIST} | EXTRA_PLATSER

    # N-värden (valbar gräns 2022) från gamla lists, nyckel (vt, area, fk, lbl)
    old_N = {}
    for L in old_data.get("lists", []):
        if len(L) >= 5 and isinstance(L[4], (int, float)) and L[4] and L[4] > 0:
            old_N.setdefault((L[0], L[1], L[2], L[3]), L[4])

    rdr = csv.reader(io.StringIO(csv_text), delimiter=";")
    header = next(rdr)
    col = {h.strip(): i for i, h in enumerate(header)}
    def g(row, key):
        i = col.get(key)
        return row[i].strip() if (i is not None and i < len(row)) else ""

    persons = {}
    cand = 0
    cand_vt = {"RD": 0, "RF": 0, "KF": 0}
    parties_distinct = set()
    fk_fullname = {}
    groups = {}   # (VALTYP, VALKRETSKOD, PARTIKOD, LISTNUMMER) -> {vt,area,fk,lbl,mem}

    for row in rdr:
        if not row:
            continue
        if ONLY_GILTIG and g(row, "GILTIG") != "J":
            continue
        knum = g(row, "KANDIDATNUMMER")
        if not knum:
            continue
        cand += 1
        vt = g(row, "VALTYP")
        vt_bit = VT_BIT.get(vt, 0)
        if vt in cand_vt:
            cand_vt[vt] += 1
        kon = 1 if g(row, "KÖN") == "K" else 0
        fk = g(row, "PARTIFÖRKORTNING")
        pkod = g(row, "PARTIKOD")
        if pkod:
            parties_distinct.add(pkod)
        if fk and fk not in fk_fullname:
            fk_fullname[fk] = g(row, "PARTIBETECKNING") or fk
        age = to_int(g(row, "ÅLDER_PÅ_VALDAGEN"))
        yrke = yrke_ur_valsedelsuppgift(g(row, "VALSEDELSUPPGIFT"), platser)
        kommun_namn = g(row, "FOLKBOKFÖRINGSKOMMUN")
        ordning = to_int(g(row, "ORDNING"))
        omr_kod = g(row, "VALOMRÅDESKOD").zfill(4) if g(row, "VALOMRÅDESKOD") else ""

        p = persons.get(knum)
        if p is None:
            p = {"namn": g(row, "NAMN"), "age": age, "kon": kon, "fk": fk,
                 "kommun": kommun_namn, "yrke": yrke, "mask": 0, "kf": "", "ort": "",
                 "_lists": []}
            persons[knum] = p
        p["mask"] |= vt_bit
        if p["age"] < 0 and age >= 0: p["age"] = age
        if not p["fk"] and fk: p["fk"] = fk
        if not p["yrke"] and yrke: p["yrke"] = yrke
        if not p["kommun"] and kommun_namn: p["kommun"] = kommun_namn
        if vt == "KF" and not p["kf"] and omr_kod:
            p["kf"] = omr_kod
        if vt == "KF" and not p.get("ort") and kommun_namn:
            p["ort"] = kommun_namn
        p["_lists"].append((vt_bit, g(row, "VALKRETSNAMN"), fk,
                            g(row, "VALKRETSBETECKNING PÅ VALSEDELN"), ordning))

        gkey = (vt, g(row, "VALKRETSKOD"), pkod, g(row, "LISTNUMMER"))
        grp = groups.get(gkey)
        if grp is None:
            grp = {"vt": vt_bit, "area": g(row, "VALKRETSNAMN"), "fk": fk,
                   "lbl": g(row, "VALKRETSBETECKNING PÅ VALSEDELN"), "mem": []}
            groups[gkey] = grp
        grp["mem"].append((knum, ordning))

    # ---- personordning + index ----
    order = sorted(persons)
    idx = {knum: i for i, knum in enumerate(order)}
    persons_l = [persons[k] for k in order]
    n = len(persons_l)

    # ---- partyList ----
    pcount = collections.Counter(p["fk"] for p in persons_l)
    top = [fk for fk, c in pcount.most_common() if fk and c >= PARTY_MIN_PERSONER]
    party_list = [{"fk": fk, "name": fk_fullname.get(fk, fk)} for fk in top] + [{"fk": "Övriga", "name": "Övriga"}]
    party_idx = {fk: i for i, fk in enumerate(top)}
    OVR = len(top)

    # ---- fkList / yrkeList ----
    fk_all = [""] + sorted({p["fk"] for p in persons_l if p["fk"]})
    fk_idx = {fk: i for i, fk in enumerate(fk_all)}
    yrke_list, yrke_idx = [], {}
    for p in persons_l:
        y = p["yrke"]
        if y and y not in yrke_idx:
            yrke_idx[y] = len(yrke_list); yrke_list.append(y)

    # ---- lists (med bevarat N) ----
    lists = []
    for grp in groups.values():
        mem = sorted(grp["mem"], key=lambda t: (t[1] if t[1] > 0 else 10**9, idx[t[0]]))
        flat = []
        for knum, ordn in mem:
            flat.append(idx[knum]); flat.append(ordn)
        N = old_N.get((grp["vt"], grp["area"], grp["fk"], grp["lbl"]), -1)
        lists.append([grp["vt"], grp["area"], grp["fk"], grp["lbl"], N, flat])

    # ---- srec[3] (valbar-flagga) ----
    # Kommun-valbarhet härleds ur BEVARAD kommunValbar (2022 års mandat per kommun+parti):
    #   person på KF-lista med ordning <= N(kommun,parti) => valbar i kommunval (bit 1).
    # Region/riksdag OR:as in ur den stabila 2022-bundlen (bit 2/4). Robust mot ombyggnad.
    kv = old_data.get("kommunValbar", {})
    NbyKF = {}
    for kod, parts in kv.items():
        for fk, e in parts.items():
            NN = e.get("N", 0)
            if NN and NN > 0:
                NbyKF[(kod, fk)] = NN
    valbar = [0] * n
    hasrank = [False] * n
    for i, p in enumerate(persons_l):
        for (vt_bit, area, fk, lbl, ordn) in p.get("_lists", []):
            if ordn and ordn > 0:
                hasrank[i] = True
                if vt_bit == 4:  # KF
                    NN = NbyKF.get((p.get("kf", ""), fk))
                    if NN and ordn <= NN:
                        valbar[i] |= 1  # kommun-bit
    def srec3(i):
        b = valbar[i]
        p = persons_l[i]
        key = _RDRF.get("%s|%s" % (_norm_namn(p["namn"]), p["age"]))
        if key:
            b |= (int(key) & 6)   # bit 2=region, 4=riksdag
        if b > 0: return b
        return 0 if hasrank[i] else -1

    # ---- P, srec, kfk ----
    P, srec, kfk = [], [], []
    for i, p in enumerate(persons_l):
        pi = party_idx.get(p["fk"], OVR)
        li = geo_name_to_lanidx.get(p["kommun"], LAN_OKAND)
        yi = yrke_idx.get(p["yrke"], -1) if p["yrke"] else -1
        P.append([p["age"], p["kon"], pi, li, p["mask"], yi])
        srec.append([p["namn"], fk_idx.get(p["fk"], 0), kom_name_to_idx.get(p["kommun"], -1), srec3(i)])
        kfk.append(p["kf"] or "")

    # ---- kommun + kommunParty (personbaserat via kfk) ----
    kommun, kommunParty = {}, {}
    for p in persons_l:
        code = p["kf"]
        if not code:
            continue
        kk = "K" if p["kon"] else "M"
        e = kommun.setdefault(code, {"n": 0, "M": 0, "K": 0, "aSum": 0, "aCnt": 0, "n65": 0, "nU30": 0})
        e["n"] += 1; e[kk] += 1
        if p["age"] >= 0:
            e["aSum"] += p["age"]; e["aCnt"] += 1
            if p["age"] >= 65: e["n65"] += 1
            if p["age"] < 30: e["nU30"] += 1
        if p["fk"] in MAP_PARTIES:
            pe = kommunParty.setdefault(code, {}).setdefault(
                p["fk"], {"n": 0, "M": 0, "K": 0, "aSum": 0, "aCnt": 0, "n65": 0, "nU30": 0})
            pe["n"] += 1; pe[kk] += 1
            if p["age"] >= 0:
                pe["aSum"] += p["age"]; pe["aCnt"] += 1
                if p["age"] >= 65: pe["n65"] += 1
                if p["age"] < 30: pe["nU30"] += 1

    # ---- aggregat (schema-paritet; sidan räknar om dem live) ----
    ages = [p["age"] for p in persons_l if p["age"] >= 0]
    kon = {"M": sum(1 for p in persons_l if p["kon"] == 0),
           "K": sum(1 for p in persons_l if p["kon"] == 1)}
    bitkey = {1: "rd", 2: "rf", 4: "kf"}
    konVt = {"rd": {"M": 0, "K": 0}, "rf": {"M": 0, "K": 0}, "kf": {"M": 0, "K": 0}}
    konLan = {name: {"M": 0, "K": 0} for name in LAN_LIST}
    for p in persons_l:
        kk = "K" if p["kon"] else "M"
        konLan[LAN_LIST[geo_name_to_lanidx.get(p["kommun"], LAN_OKAND)]][kk] += 1
        for bit, key in bitkey.items():
            if p["mask"] & bit: konVt[key][kk] += 1
    ageBuckets = [[lbl, 0] for lbl in AGE_LBL]
    for a in ages: ageBuckets[age_bucket(a)][1] += 1
    ageMean = round(sum(ages) / len(ages), 1) if ages else 0
    pg = collections.defaultdict(lambda: {"M": 0, "K": 0, "aSum": 0, "aCnt": 0})
    for p in persons_l:
        e = pg[p["fk"]]; e["K" if p["kon"] else "M"] += 1
        if p["age"] >= 0: e["aSum"] += p["age"]; e["aCnt"] += 1
    parties = [{"fk": fk, "name": fk_fullname.get(fk, fk), "n": pg[fk]["M"] + pg[fk]["K"],
                "M": pg[fk]["M"], "K": pg[fk]["K"],
                "age": round(pg[fk]["aSum"] / pg[fk]["aCnt"], 1) if pg[fk]["aCnt"] else 0}
               for fk in top]
    partiesRest = {"count": max(len(parties_distinct) - len(top), 0),
                   "n": sum(1 for p in persons_l if p["fk"] not in party_idx)}
    yhave = sum(1 for p in persons_l if p["yrke"])
    ycnt = collections.Counter(p["yrke"] for p in persons_l if p["yrke"])
    ykon = collections.defaultdict(lambda: [0, 0])
    for p in persons_l:
        if p["yrke"]: ykon[p["yrke"]][p["kon"]] += 1
    yrkenTot = [[y, c] for y, c in ycnt.most_common(30)]
    yrkenKon = [[y, ykon[y][0], ykon[y][1]] for y, _ in ycnt.most_common(18)]
    MINV = max(12, n // 600)
    skew = [(y, ykon[y][0], ykon[y][1], ykon[y][0] + ykon[y][1]) for y in ycnt
            if (ykon[y][0] + ykon[y][1]) >= MINV]
    maleSkew = [[y, m, k] for (y, m, k, t) in sorted(skew, key=lambda x: x[1] / x[3], reverse=True)[:10]]
    femaleSkew = [[y, m, k] for (y, m, k, t) in sorted(skew, key=lambda x: x[2] / x[3], reverse=True)[:10]]
    yvt = {"rd": collections.Counter(), "rf": collections.Counter(), "kf": collections.Counter()}
    for p in persons_l:
        if not p["yrke"]: continue
        for bit, key in bitkey.items():
            if p["mask"] & bit: yvt[key][p["yrke"]] += 1
    yrkenVt = {k: [[y, c] for y, c in v.most_common(10)] for k, v in yvt.items()}

    _RESULT = {
        "n": n, "cand": cand, "candVt": cand_vt, "nParties": len(parties_distinct),
        "kon": kon, "konVt": konVt, "konLan": konLan,
        "ageBuckets": ageBuckets, "ageMean": ageMean,
        "ageMedian": int(statistics.median(ages)) if ages else 0,
        "ageMin": min(ages) if ages else 0, "ageMax": max(ages) if ages else 0,
        "share65": round(100 * sum(1 for a in ages if a >= 65) / len(ages), 1) if ages else 0,
        "shareU30": round(100 * sum(1 for a in ages if a < 30) / len(ages), 1) if ages else 0,
        "parties": parties, "partiesRest": partiesRest,
        "yrkeHave": yhave, "yrkenTot": yrkenTot, "yrkenKon": yrkenKon,
        "maleSkew": maleSkew, "femaleSkew": femaleSkew, "yrkenVt": yrkenVt,
        "kommun": kommun, "lanList": LAN_LIST, "partyList": party_list,
        "yrkeList": yrke_list, "P": P, "komNames": kom_names, "fkList": fk_all,
        "srec": srec, "kommunValbar": old_data.get("kommunValbar", {}),
        "mapParties": MAP_PARTIES, "kommunParty": kommunParty, "kfk": kfk, "lists": lists,
    }
    _RESULT["_persons_l"] = persons_l
    return _RESULT


# ============================ EXTRA-BLOCK ===================================
MAP_BIG = ["S", "M", "SD", "C", "V", "KD", "MP", "L"]
_PNAMN = {"S":"Socialdemokraterna","M":"Moderaterna","SD":"Sverigedemokraterna",
          "C":"Centerpartiet","V":"Vänsterpartiet","KD":"Kristdemokraterna",
          "MP":"Miljöpartiet","L":"Liberalerna"}

def build_extra(persons_l, code_to_name, bundle):
    """Bygger EXTRA-blocket (kartlägen, topplistor, 2022-jämförelser) ur 2026-personer
    + den statiska 2022-bundlen. Personmatchning sker på normaliserat namn + åldersdiff 3-5 år."""
    midx = bundle.get("match", {})          # normnamn -> [åldrar 2022]
    since = bundle.get("since22", {})        # {riket:{...}, parti:{fk:{...}}}

    def is_ny(namn, age):
        if age < 0: return True
        cand = midx.get(_norm_namn(namn))
        if not cand: return True
        return not any(3 <= age - a <= 5 for a in cand)

    per_fk = {fk: [0, 0] for fk in MAP_BIG}      # [nya, tot]
    komm_new = {}                                 # kod -> [nya, tot]
    kom = {}                                      # kod -> {aSum,aCnt,K,n}
    # varvning: (vt,area,fk,lbl) -> [(ordning,kön)]
    lists_kon = {}
    yng_pool = []

    for p in persons_l:
        fk = p["fk"]; age = p["age"]; kon = p["kon"]
        ny = is_ny(p["namn"], age)
        if fk in per_fk:
            per_fk[fk][1] += 1
            if ny: per_fk[fk][0] += 1
        code = p.get("kf") or ""
        if code:
            kn = komm_new.setdefault(code, [0, 0]); kn[1] += 1
            if ny: kn[0] += 1
            e = kom.setdefault(code, {"aSum":0,"aCnt":0,"K":0,"n":0})
            e["n"] += 1
            if kon: e["K"] += 1
            if age >= 0: e["aSum"] += age; e["aCnt"] += 1
        if age >= 18:
            yng_pool.append(p)

    # komstat {kod:{ma,ak,ny}}
    komstat = {}
    for code, e in kom.items():
        ny, tot = komm_new.get(code, [0, 0])
        komstat[code] = {
            "ma": round(e["aSum"]/e["aCnt"], 1) if e["aCnt"] else -1,
            "ak": round(100*e["K"]/e["n"], 1) if e["n"] else -1,
            "ny": round(100*ny/tot, 1) if tot else -1,
        }

    ny_parti = [[fk, per_fk[fk][1], round(100*per_fk[fk][0]/per_fk[fk][1], 1) if per_fk[fk][1] else 0]
                for fk in MAP_BIG]
    tot_ny = sum(v[0] for v in per_fk.values()); tot_al = sum(v[1] for v in per_fk.values())
    ny_riket = round(100*tot_ny/tot_al, 1) if tot_al else 0

    # varvning ur listmedlemskap
    for p in persons_l:
        for (vt, area, fk, lbl, ordn) in p.get("_lists", []):
            if fk in per_fk and p["kon"] in (0, 1):
                lists_kon.setdefault((vt, area, fk, lbl), []).append((ordn, p["kon"]))
    varv = {fk: [0, 0] for fk in MAP_BIG}
    for (vt, area, fk, lbl), cs in lists_kon.items():
        if fk not in varv or len(cs) < 4: continue
        cs = sorted(cs)
        for (o1, k1), (o2, k2) in zip(cs, cs[1:]):
            if o1 < 0 or o2 < 0: continue
            varv[fk][1] += 1
            if k1 != k2: varv[fk][0] += 1
    varvtab = sorted(([fk, round(100*a/t, 1)] for fk, (a, t) in varv.items() if t),
                     key=lambda x: -x[1])

    # yngst/äldst topp-10
    def fmt(lst):
        return [[p["namn"], p["age"], p["fk"], p.get("ort", "")] for p in lst]
    yng = fmt(sorted(yng_pool, key=lambda p: p["age"])[:10])
    ald = fmt(sorted(yng_pool, key=lambda p: -p["age"])[:10])

    # sedan 2022 (2026-sidan räknas nu; 2022-sidan ur bundlen)
    def agg26(sel):
        ages = [p["age"] for p in sel if p["age"] >= 0]
        K = sum(1 for p in sel if p["kon"] == 1); M = sum(1 for p in sel if p["kon"] == 0)
        return {"n": len(sel), "ma": round(sum(ages)/len(ages), 1) if ages else 0,
                "ak": round(100*K/(K+M), 1) if (K+M) else 0}
    by_fk = {}
    for p in persons_l:
        by_fk.setdefault(p["fk"], []).append(p)
    since_parti = []
    for fk in MAP_BIG:
        a = since.get("parti", {}).get(fk, {}); b = agg26(by_fk.get(fk, []))
        since_parti.append({"fk": fk, "namn": _PNAMN[fk],
            "n22": a.get("n"), "n26": b["n"], "ma22": a.get("ma"), "ma26": b["ma"],
            "ak22": a.get("ak"), "ak26": b["ak"]})
    since2022 = {"riket": {"y22": since.get("riket", {}), "y26": agg26(persons_l)},
                 "parti": since_parti}

    return {
        "komstat": komstat, "ny_parti": ny_parti, "ny_riket": ny_riket,
        "varv": varvtab, "yngst": yng, "aldst": ald,
        "since2022": since2022,
        "match_note": "Jämförelsen 2022 bygger på namn + åldersskillnad (3–5 år). Uppskattning; namnbyten och flyttar kan ge fel i enskilda fall.",
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=HTML_FILE)
    ap.add_argument("--csv-file")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--data2022", default=DATA2022_FILE, help="Statisk 2022-databundle (JSON)")
    args = ap.parse_args()

    html = open(args.html, encoding="utf-8").read()
    data_text, data_span = read_block(html, "DATA")
    geo_text, _ = read_block(html, "GEO")
    old = json.loads(data_text)
    geo = json.loads(geo_text)

    if args.csv_file:
        csv_text = open(args.csv_file, encoding="utf-8-sig").read()
    else:
        print("Laddar ner", CSV_URL, "...")
        csv_text = fetch_csv_text(CSV_URL)

    # Ladda stabil 2022-bundle
    global _RDRF
    try:
        _BUNDLE = json.load(open(args.data2022, encoding="utf-8"))
        _RDRF = _BUNDLE.get("rdrf", {})
    except FileNotFoundError:
        print("VARNING: hittade inte", args.data2022, "- region/riksdag-valbarhet och EXTRA hoppas över.")
        _BUNDLE = None

    data = build_data(csv_text, geo, old)
    persons_l = data.pop("_persons_l", [])

    # Bygg om EXTRA-blocket om bundlen finns
    if _BUNDLE is not None:
        _, code_to_name, _, _, _2 = None, {}, None, None, None
        extra = build_extra(persons_l, {}, _BUNDLE)
        m_extra = re.search(r'(<script id="EXTRA" type="application/json">).*?(</script>)', html, re.S)
        if m_extra:
            html = html[:m_extra.start()] + m_extra.group(1) + \
                   json.dumps(extra, ensure_ascii=False, separators=(",", ":")) + \
                   m_extra.group(2) + html[m_extra.end():]
            # data_span kan ha flyttats om EXTRA ligger före DATA -> läs om DATA-span
            _, data_span = read_block(html, "DATA")
        else:
            print("VARNING: <script id=\"EXTRA\"> saknas i HTML - EXTRA hoppas över.")

    # Ta bort tunga fält som sajtens JS inte läser (håller filen liten)
    for _k in ("lists", "kfk", "kommunParty"):
        data.pop(_k, None)

    if args.self_check:
        for k in ("n", "cand", "nParties", "ageMean"):
            o, nv = old.get(k), data.get(k)
            flag = "  <-- STOR AVVIKELSE" if (isinstance(o, (int, float)) and o and abs(nv - o) > 0.25 * abs(o)) else ""
            print(f"  {k}: {o} -> {nv}{flag}")
        vb_old = sum(1 for r in old.get("srec", []) if len(r) > 3 and r[3] and r[3] > 0)
        vb_new = sum(1 for r in data.get("srec", []) if len(r) > 3 and r[3] and r[3] > 0)
        print(f"  valbar (srec[3]>0): {vb_old} -> {vb_new}")
        print(f"  EXTRA-block: {'uppdaterat' if _BUNDLE is not None else 'HOPPAT (saknar data2022.json)'}")

    new_html = html[:data_span[0]] + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + html[data_span[1]:]
    open(args.html, "w", encoding="utf-8").write(new_html)
    print(f"Klar. Skrev {args.html}: {data['n']} personer, {data['cand']} kandidaturer.")

if __name__ == "__main__":
    main()
