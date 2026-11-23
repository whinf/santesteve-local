import gzip
import json
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from unittest import mock
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import i18n  # noqa: E402
import render  # noqa: E402
import sources as S  # noqa: E402
import tz  # noqa: E402
from render import Site  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = tz.to_madrid(tz.parse_iso("2026-11-19T18:16:00+01:00"))  # día en que se capturaron los fixtures
# Casi todos los fixtures son datos reales de Sant Cugat (de la web hermana): sirven para probar los lectores, que no
# dependen del municipio. Lo propio de Sant Esteve (líneas, pueblos, término municipal...) se prueba aparte.
SANT_CUGAT = {"LAT": 41.4708, "LON": 2.0849, "NEAR_TOWNS": ("SANT CUGAT DEL VALLÈS", "VALLDOREIX", "RUBÍ",
              "CERDANYOLA DEL VALLÈS", "BELLATERRA", "SANT QUIRZE DEL VALLÈS"),
              "_HERE": re.compile(r"^(SANT CUGAT DEL VALL[EÈ]S|VALLDOREIX)\b"), "LINES": ("R8",)}
OCT = tz.to_madrid(tz.parse_iso("2026-10-20T18:16:00+02:00"))  # fixtures heredados (Renfe, BDNS, YouTube)
CFG = {"site_name": "Sant Esteve Avui", "site_url": "https://example.github.io/santesteve-local"}


def fixture(name):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def _varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def pb(*fields):
    """Mensaje protobuf mínimo: (número, bytes | str | int | mensaje ya codificado)."""
    out = b""
    for num, value in fields:
        if isinstance(value, int):
            out += _varint(num << 3) + _varint(value)
        else:
            raw = value.encode("utf-8") if isinstance(value, str) else value
            out += _varint(num << 3 | 2) + _varint(len(raw)) + raw
    return out


def fgc_alert(ident, text, route=None, start=0, end=2 ** 40):
    selector = pb((2, route)) if route else pb((5, "SC"))
    alert = pb((1, pb((1, start), (2, end))), (5, selector), (10, pb((1, pb((1, text), (2, "ca"))))))
    return pb((2, pb((1, ident), (5, alert))))


class TestSources(unittest.TestCase):
    def test_weather(self):
        w = S.parse_weather(fixture("metno.json"), NOW)
        self.assertIsInstance(w["now"]["temp"], float)
        self.assertEqual(w["days"][0]["date"], "2026-11-19")
        self.assertEqual(len(w["days"]), 6)
        self.assertTrue(all(d["tmin"] <= d["tmax"] and d["symbol"] for d in w["days"]))
        self.assertEqual(i18n.sky("ca", "cloudy"), ("cloudy", "Ennuvolat"))

    def test_agenda(self):
        gencat, permanent = S.parse_agenda_gencat(fixture("agenda.json"), NOW.date())
        self.assertIn("El fil de la modernitat", [e["title"] for e in gencat])
        self.assertEqual((len(gencat), len(permanent)), (10, 2))
        merged = S.merge_events(gencat, gencat)
        self.assertEqual(len(merged), len({(e["title"], e["start"]) for e in gencat}))
        self.assertEqual([S.month_ca(m) for m in ("gen.", "juny", "jul.", "ag.", "octubre")], [1, 6, 7, 8, 10])

    def test_agenda_survives_source_down(self):
        gencat, _ = S.parse_agenda_gencat(fixture("agenda.json"), NOW.date())
        with mock.patch.object(S, "socrata", side_effect=S.FetchError("analisi: 503")), mock.patch("builtins.print"):
            ag = S.fetch_agenda(NOW, {"events": gencat, "permanent": [{"title": "x"}]})
        self.assertIn("El fil de la modernitat", [e["title"] for e in ag["events"]])  # guardado
        self.assertEqual(ag["permanent"], [{"title": "x"}])
        self.assertEqual(S.AGENDA_MUNICIPI.rsplit("/", 1)[1], "sant-esteve-sesrovires")

    def test_leisure(self):
        places = S.parse_leisure(fixture("osm.json"))
        names = {p["name"] for p in places}
        self.assertIn("Monestir de Sant Cugat", names)
        self.assertIn("Pi d'en Xandri", names)
        self.assertGreater(len(places), 150)
        for p in places:
            self.assertIn(p["group"], i18n.LEISURE_GROUPS)
            self.assertIn(p["type"], i18n.PLACE_TYPES)
            self.assertTrue(p["web"] == "" or p["web"].startswith(("http://", "https://")))
        self.assertEqual(i18n.hours("ca", "Mo-Fr 08:00-20:00; Su off"), "dl-dv 08:00-20:00; dg tancat")
        self.assertEqual(i18n.cuisine("es", "regional;pizza"), "cocina tradicional, pizza")

    def test_nightlife(self):
        self.assertEqual([S._tile_id(0, 0, 0)] + [S._tile_id(1, x, y) for x, y in ((0, 0), (0, 1), (1, 1), (1, 0))],
                         [0, 1, 2, 3, 4])  # ejemplos de la especificación de PMTiles
        tile = gzip.decompress((FIX / "overture_14_8288_6113.mvt.gz").read_bytes())
        features = S.parse_mvt(tile, 14, 8288, 6113)
        self.assertEqual(len(features), 265)
        segments = S.boundary()
        places = S.parse_overture_night(features, segments)
        self.assertEqual(places, [])  # la tesela de prueba es de Cerdanyola: nada cae dentro de Sant Esteve
        self.assertTrue(S._inside(S.LON, S.LAT, segments))  # el centro
        self.assertFalse(S._inside(1.9311, 41.4886, segments))  # Martorell
        self.assertFalse(S._inside(2.1408, 41.4913, segments))  # Cerdanyola

        def venue(name, address, status="", confidence=0.9, primary="dance_club", lon=S.LON, lat=S.LAT):
            return {"@name": name, "operating_status": status, "confidence": confidence, "lon": lon, "lat": lat,
                    "taxonomy": json.dumps({"primary": primary, "hierarchy": ["arts_and_entertainment", primary]}),
                    "addresses": json.dumps([{"freeform": address}]),
                    "sources": json.dumps([{"dataset": "meta", "license": "CDLA-Permissive-2.0"}])}
        fake = [venue("Nou Nom", "Carrer De Can Mitjans 4", "open"), venue("Nom Antic", "Carrer de Can Mitjans, 4"),
                venue("Night Swinger", "Carrer Del Bosc Tancat 5", "open"), venue("Tancat", "Carrer Major 1", "permanently_closed"),
                venue("Coral Polifònica", "Carrer Major 2", primary="music_venue"), venue("Fora", "Carrer Major 3", lon=2.3),
                venue("Sala Rock", "Carrer Major 4", primary="music_venue")]
        self.assertEqual([p["name"] for p in S.parse_overture_night(fake, segments)], ["Nou Nom", "Sala Rock"])
        night = S.parse_overture_night(fake, segments)
        osm = [p for p in S.parse_leisure(fixture("osm.json")) if p["name"] == "Monestir de Sant Cugat"]
        twin = dict(osm[0], name="Monestir de Sant Cugat del Vallès", lat=osm[0]["lat"] + 0.0005, osm="", src=["meta"])
        self.assertEqual(S.merge_places(osm, [twin, night[0]]), [osm[0], night[0]])  # el repetido no entra

    def test_air(self):
        air = S.parse_air(fixture("air.json"))
        self.assertIn("Sant Cugat del Vallès", air)
        self.assertEqual(set(air["Sant Cugat del Vallès"]["NO2"]), {"value", "date", "hour"})
        self.assertEqual(i18n.aq_level("NO2", 41), 1)
        self.assertEqual(i18n.aq_level("PM10", 500), 5)

    def test_alerts_only_our_lines(self):
        with mock.patch.object(S, "LINES", SANT_CUGAT["LINES"]):
            self._renfe()
        self.assertEqual(S.LINES, ("R4", "R8"))  # Martorell
        self.assertTrue(all(set(a["lines"]) <= {"R4", "R8"} for a in S.parse_alerts(fixture("renfe_alerts.json"), OCT)))

    def _renfe(self):
        alerts = S.parse_alerts(fixture("renfe_alerts.json"), OCT)
        self.assertEqual(len(alerts), 3)
        self.assertTrue(all(a["lines"] == ["R8"] for a in alerts))  # de Rodalies, solo la R8
        strike = next(a for a in alerts if a["text"]["ca"].startswith("Per causes alienes"))
        self.assertTrue(strike["text"]["es"].startswith("Por causas ajenas"))  # las dos llegan marcadas «es»
        later = S.parse_alerts(fixture("renfe_alerts.json"), tz.to_madrid(tz.parse_iso("2026-11-18T19:00:00+01:00")))
        texts = " ".join(a["text"]["ca"] for a in later)
        self.assertNotIn("Castelldefels", texts)  # obras de la R2 etiquetadas con toda la red
        self.assertNotIn("Montcada Bifurcació", texts)  # R7: no es de las nuestras

    def test_fgc_alerts(self):
        real = (FIX / "fgc_alerts.pb").read_bytes()  # 12 notas de trenes concretos («OBSERVATIONS_…»)
        self.assertEqual(len([1 for f, _ in S._pbf(real) if f == 2]), 12)
        self.assertEqual(S.parse_fgc_alerts(real, NOW), [])
        ts = int(NOW.timestamp())
        feed = real + b"".join((
            fgc_alert("INC_1", "Retards a la línia R6 per una avaria a Sant Esteve Sesrovires", route="R6"),
            fgc_alert("INC_1b", "Retards a la línia R6 per una avaria a Sant Esteve Sesrovires", route="R60"),  # repetido
            fgc_alert("INC_2", "Servei interromput entre Barcelona i Sant Cugat (S1 i S2)", route="S1"),
            fgc_alert("INC_3", "Freqüències alterades a tota la línia Llobregat-Anoia"),
            fgc_alert("INC_4", "Obres a l'estació de Gràcia"),  # sin línea: no se sabe si nos afecta
            fgc_alert("INC_5", "Tall previst a la R60", route="R60", start=ts + 86400),  # aún no ha empezado
            fgc_alert("OBSERVATIONS_X", "Últim cotxe reservat escolar R6", route="R6")))
        got = S.parse_fgc_alerts(feed, NOW)
        self.assertEqual([a["lines"] for a in got], [["R6"], ["FGC"]])
        self.assertEqual(got[0]["text"], {"ca": "Retards a la línia R6 per una avaria a Sant Esteve Sesrovires"})
        with self.assertRaises(S.FetchError):
            S.parse_fgc_alerts(b"\x12\xff\xff", NOW)  # protobuf truncado
        renfe = fixture("renfe_alerts.json")
        with mock.patch.object(S, "fetch_fgc_alerts", side_effect=S.FetchError("fgc: 503")), \
                mock.patch.object(S, "get_json", return_value=renfe), mock.patch("builtins.print"):
            self.assertTrue(S.fetch_alerts(OCT))  # sin FGC quedan los de Rodalies
        with mock.patch.object(S, "fetch_fgc_alerts", side_effect=S.FetchError("fgc: 503")), \
                mock.patch.object(S, "get_json", side_effect=S.FetchError("renfe: 503")), self.assertRaises(S.FetchError):
            S.fetch_alerts(OCT)

    def test_pharmacies(self):
        doc = fixture("osm_pharmacies.json")
        directory = S.parse_pharmacy_directory(doc)
        self.assertEqual(len(directory), 21)
        self.assertEqual(directory[0], {"id": "beringues-sorribas-lluis", "name": "Farmàcia Beringues Sorribas, Lluís",
                                        "address": "Avinguda de Lluís Companys, 46", "town": "Sant Esteve",
                                        "phone": "935 893 968", "lat": 41.4717953, "lon": 2.0793777})
        self.assertEqual(len({p["id"] for p in directory}), 21)
        with self.assertRaises(S.FetchError):
            S.parse_pharmacy_directory({"elements": []})
        with mock.patch.object(S, "get_text", return_value=json.dumps(doc)):
            got = S.fetch_pharmacy(NOW)
        self.assertEqual((got["rosters"], len(got["directory"]), got["source"]), ({}, 21, "https://www.farmaguia.net/"))
        with mock.patch.object(S, "get_text", side_effect=S.FetchError("overpass: 504")), self.assertRaises(S.FetchError):
            S.fetch_pharmacy(NOW)
        site = Site(CFG, {"pharmacy": {"fetched": NOW.isoformat(), "data": got}}, "unused", NOW)
        self.assertEqual(site.pharmacy_shifts(), [])  # sin calendario de guardias: nunca se inventa un turno
        block = site.pharmacy_block("es")
        self.assertNotIn('class="events shifts"', block)
        self.assertIn('href="https://www.farmaguia.net/"', block)
        self.assertIn("Col·legi de Farmacèutics", block)
        page = site.pharmacy_page("ca")
        self.assertIn("Farmàcia Beringues Sorribas, Lluís", page)
        self.assertIn('href="tel:935893968"', page)
        self.assertIn("mlat=41.4717953", page)
        self.assertNotIn("<tr data-from=", page)
        self.assertIn("Farmàcies de guàrdia", site.today_strip("ca"))

    def test_aids(self):
        today = OCT.date()
        calls = {a["id"]: a for a in (S.parse_bdns_call(d, today) for d in fixture("bdns_calls.json"))}
        states = {n: (S.aid_state(a, today) or (None,))[0] for n, a in calls.items() if S.bdns_relevant(a)}
        self.assertEqual(states, {"933847": "open", "936187": "soon", "933143": "check", "922783": "open",
                                  "936968": "approx", "910930": "open"})
        self.assertFalse(S.bdns_relevant(calls["906102"]))  # grupo municipal
        self.assertFalse(S.bdns_relevant(calls["921706"]))  # Consorci de l'Habitatge de la ciudad de Barcelona
        self.assertIsNone(S.aid_state(calls["934569"], today))  # «Sense termini de presentació»
        self.assertEqual(S.aid_state(calls["922783"], today), ("open", date(2026, 12, 31)))  # fecha en el texto
        self.assertEqual(calls["933847"]["who"], ["people"])
        start = date(2026, 10, 1)
        self.assertEqual(S.end_from_text("15 Dies Hàbils", start), (date(2026, 10, 22), True))
        self.assertEqual(S.end_from_text("Diez (10) días hábiles", start), (date(2026, 10, 15), True))
        self.assertEqual(S.end_from_text("fins al 15 d’octubre", start), (date(2026, 10, 15), False))
        self.assertEqual(S.end_from_text("Indicado en la convocatoria", start), (None, False))
        self.assertEqual(S.end_from_text("del 01/10/2026 al 31/10/2026", start), (date(2026, 10, 31), False))
        self.assertEqual(S.end_from_text("fins al 15 de gener", date(2026, 12, 1)), (date(2027, 1, 15), False))
        self.assertEqual(i18n.organ_name("AGÈNCIA DE GESTIÓ D'AJUTS (AGAUR)"), "Agència de Gestió d'Ajuts (AGAUR)")
        self.assertEqual(i18n.fmt_money("ca", 2750000), "2.750.000 €")

    def test_fetch_aids_reuses_cache(self):
        details = {d["codigoBDNS"]: d for d in fixture("bdns_calls.json")}
        asked = []

        def fake(url):
            if "busqueda" in url:
                return {"content": [{"numeroConvocatoria": n} for n in details], "last": True}
            asked.append(url.rsplit("=", 1)[1])
            return details[asked[-1]]

        with mock.patch.object(S, "get_json", fake), mock.patch.object(S.time, "sleep"):
            first = S.fetch_aids(OCT)
            self.assertEqual(set(first["calls"]), {"933847", "936187", "933143", "922783", "936968", "910930"})
            self.assertEqual(first["closed"], ["906102", "921706", "934569"])
            asked.clear()
            again = S.fetch_aids(OCT, first)
        self.assertEqual(asked, [])  # nada nuevo ni caducado: ninguna consulta de detalle
        self.assertEqual(again, first)

    def test_jobs_and_fuel(self):
        with mock.patch.multiple(S, LAT=SANT_CUGAT["LAT"], LON=SANT_CUGAT["LON"], NEAR_TOWNS=SANT_CUGAT["NEAR_TOWNS"]):
            self._jobs()
        self.assertEqual(S.NEAR_TOWNS[0], "SANT ESTEVE SESROVIRES")
        fuel = S.parse_fuel(fixture("fuel_1067.json"), "Sant Cugat del Vallès")
        self.assertEqual(len(fuel), 9)
        self.assertTrue(all(1 < s["prices"]["g95"] < 3 for s in fuel if "g95" in s["prices"]))
        self.assertEqual((S.FUEL_HOME, S.FUEL_TOWNS[S.FUEL_HOME]), (1070, "Sant Esteve Sesrovires"))

    def _jobs(self):
        public = S.parse_cido(fixture("cido.json"), NOW.date())
        town = [j for j in public if j["org"].startswith("Ajuntament de Sant Cugat del Vallès")]
        self.assertEqual(len(town), 1)
        self.assertTrue(all(j["km"] < 1 and j["url"].startswith("https://cido.diba.cat/") for j in town))
        offers = S.parse_offers(fixture("feina_activa.xml"), NOW.date())
        self.assertEqual([j["town"] for j in offers], ["SANT CUGAT DEL VALLÈS"] * 4 + ["RUBÍ"] * 2)  # sin la lejana ni la vieja
        self.assertEqual(offers[0]["date"], max(j["date"] for j in offers[:4]))
    def test_contracts(self):
        cs = S.parse_contracts(fixture("contracts.json"))
        by = lambda words: next(c for c in cs if c["title"].startswith(words))  # noqa: E731
        green = by("Contracte de manteniment de les zones verdes")
        self.assertEqual((green["lots"], green["phase"], round(green["amount"])), (4, "Formalització", 14822827))
        self.assertEqual((green["winners"][:2], len(green["winners"])), (["PREZERO ESPAÑA, S.A.U.", "TALHER, S.A."], 4))
        self.assertEqual(by("Servei de manteniment preventiu i correctiu de la flota")["deadline"], "2026-11-26T12:00")
        self.assertEqual(by("Obres per la instal·lació de carregadors")["phase"], "Anul·lació")
        self.assertEqual(S.companies_only("*** 3757 **||B12345678", "Pascual López||Obres Vallès SL"), ["Obres Vallès SL"])
        self.assertEqual(i18n.fmt_big("ca", 49950920.77), "50,0 M€")
        self.assertEqual(i18n.fmt_big("en", 1050000), "€1.1m")

    def test_borme(self):
        page = fixture("borme_bcn.html")
        with mock.patch.object(S, "_HERE", SANT_CUGAT["_HERE"]):
            self._borme(page)
        for town, ours in (("SANT ESTEVE SESROVIRES", True), ("SANT ESTEVE DE SESROVIRES", True),
                           ("SANT ESTEVE DE PALAUTORDERA", False), ("MARTORELL", False)):
            self.assertEqual(bool(S._HERE.match(town)), ours, town)

    def _borme(self, page):
        known = {}
        events = {e["name"]: e for e in S.parse_borme(page, "2026-11-17", "https://www.boe.es/b", known)}
        self.assertEqual({n: e["kind"] for n, e in events.items()}, {
            "FRACTALSTACK S.L.": "new", "BATAS PRAT S.L.": "moved_in", "GAMING E-GLOBAL XPERIENCE S.L.": "moved_in"})
        self.assertEqual(events["FRACTALSTACK S.L."]["capital"], 3000.0)
        self.assertEqual(set(known), {"B 666541", "B 542694", "B 602130"})
        again = S.parse_borme(page, "2026-11-18", "https://www.boe.es/b", known)
        self.assertEqual([e["kind"] for e in again], ["new"])  # las ya conocidas no «llegan» otra vez
        gone = ('<h5 class="articulo">1 - BATAS PRAT S.L.</h5><p class="parrafo">Cambio de domicilio social. CL MAJOR 5 (RUBI). '
                'Datos registrales. S 8 , H B 542694, I/A 3 (10.11.26).</p>')
        self.assertEqual([(e["kind"], e["town"]) for e in S.parse_borme(gone, "2026-11-19", "u", known)], [("moved_out", "RUBI")])
        self.assertNotIn("B 542694", known)

    def test_fetch_companies(self):
        sumario = json.dumps(fixture("borme_sumario.json")).encode()
        page = fixture("borme_bcn.html")
        asked = []

        def fake(url, **kw):
            asked.append(url[-8:])
            if url.endswith(("20261024", "20261025")):  # sábado, y el domingo de hoy
                raise S.FetchError(f"{url}: HTTP Error 404: Not Found")
            return sumario
        sunday = tz.to_madrid(tz.parse_iso("2026-10-25T09:00:00+01:00"))
        prev = {"since": "2025-10-26", "last": "2026-10-22", "known": {}, "events": []}
        with mock.patch.object(S, "get_bytes", fake), mock.patch.object(S, "get_text", return_value=page), \
                mock.patch.object(S.time, "sleep"), mock.patch.object(S, "_HERE", SANT_CUGAT["_HERE"]):
            first = S.fetch_companies(sunday, prev)
            self.assertEqual(asked, ["20261023", "20261024", "20261025"])
            self.assertEqual((first["last"], len(first["events"]), len(first["known"])), ("2026-10-24", 3, 3))
            asked.clear()
            self.assertEqual(S.fetch_companies(sunday, first), first)
            self.assertEqual(asked, ["20261025"])
        down = mock.Mock(side_effect=S.FetchError("boe.es: HTTP Error 503"))
        with mock.patch.object(S, "get_bytes", down), self.assertRaises(S.FetchError):
            S.fetch_companies(sunday, prev)

    def test_directory(self):
        eq = S.parse_equipments(fixture("equip.json"))
        self.assertEqual(len(eq), 193)
        self.assertTrue(all(len(e["cp"]) in (0, 5) for e in eq))
        self.assertEqual(len(S.parse_chargers(fixture("ev.json"))), 6)
        for cat in {e["cat"] for e in eq}:
            self.assertIn(cat, i18n.CATEGORIES)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.links += [a[k] for k in ("href", "src") if a.get(k)]


class TestSite(unittest.TestCase):
    def test_week_picks_rotate(self):
        items = [{"id": f"p{i}"} for i in range(10)]
        mon, sun, next_mon = date(2026, 10, 26), date(2026, 11, 1), date(2026, 11, 2)
        self.assertEqual(render.week_picks(items, mon, 3), render.week_picks(items, sun, 3))
        this, after = render.week_picks(items, mon, 3), render.week_picks(items, next_mon, 3)
        self.assertEqual(len(this), 3)
        self.assertFalse({p["id"] for p in this} & {p["id"] for p in after})
        self.assertEqual(render.week_picks([], mon, 3), [])
        self.assertEqual(len(render.week_picks(items[:2], mon, 3)), 2)

    def test_curated_picks(self):
        picks = render.load_picks()
        food = sum(p["kind"] == "food" for p in picks)  # aún sin restaurantes: o ninguno o los suficientes para rotar
        self.assertTrue(food == 0 or food >= 2 * render.PICKS_PER_WEEK["food"])
        self.assertGreaterEqual(sum(p["kind"] == "experience" for p in picks), 2 * render.PICKS_PER_WEEK["experience"])
        self.assertEqual(len({p["id"] for p in picks}), len(picks))
        for p in picks:
            for lang in i18n.LANGS:
                self.assertTrue(p["why"][lang] and p["tags"][lang], p["id"])
            self.assertTrue(not p.get("url") or p["url"].startswith(("https://", "http://")), p["id"])
            self.assertTrue(set(p.get("months", [1])) <= set(range(1, 13)), p["id"])
            date.fromisoformat(p["checked"])

    def test_politics_guide(self):
        g = render.load_politics()
        date.fromisoformat(g["checked"])
        ids = [lv["id"] for lv in g["levels"]]
        self.assertGreaterEqual(len(ids), 7)
        self.assertEqual(len(set(ids)), len(ids))
        ways = [w for lv in g["levels"] for w in lv["ways"]] + g["common"]
        for lv in g["levels"]:
            self.assertIn(lv["vote"], ("direct", "indirect", "none"))
            self.assertTrue(lv["ways"], lv["id"])
            for f in ("name", "body", "size", "who", "elected", "decides"):
                self.assertTrue(all(lv[f].get(lg) for lg in i18n.LANGS), (lv["id"], f))
        for w in ways:
            self.assertTrue(all(w["name"].get(lg) and w["text"].get(lg) for lg in i18n.LANGS), w["name"])
            self.assertTrue(w["url"] == "" or w["url"].startswith("https://"), w["url"])

    def test_events_split_and_weekend(self):
        cfg = CFG
        events = [{"title": "Acabat", "start": "2026-11-10", "end": "2026-11-12", "source": "ajuntament"},
                  {"title": "En marxa", "start": "2026-11-17", "end": "2026-11-25", "source": "ajuntament"},
                  {"title": "Dissabte", "start": "2026-11-21", "end": "2026-11-21", "source": "ajuntament"},
                  {"title": "<script>x</script>", "start": "2026-11-19", "url": "javascript:alert(1)", "source": "ajuntament"}]
        site = Site(cfg, {"agenda": {"fetched": NOW.isoformat(), "data": {"events": events}}}, "unused", NOW)
        upcoming, ongoing = site.events_split()
        self.assertEqual([e["title"] for e in ongoing], ["En marxa"])  # el acabado no sale
        self.assertEqual(sorted(e["title"] for e in site.weekend_events()), ["Dissabte", "En marxa"])
        item = site.event_item("ca", next(e for e in upcoming if e.get("url")))
        self.assertNotIn("javascript:", item)
        self.assertIn("&lt;script&gt;", item)
        self.assertIn("\\u003c", render.jsonld_text({"name": "</script>"}))

    def test_pick_window(self):
        fest = {"months": [10, 11], "from": "10-20", "until": "11-15"}
        self.assertEqual([render.pick_live(fest, d) for d in (date(2026, 10, 19), date(2026, 10, 20), date(2026, 11, 15),
                                                              date(2026, 11, 18))], [False, True, True, False])
        xmas = {"from": "12-15", "until": "01-06"}  # cruza el cambio de año
        self.assertEqual([render.pick_live(xmas, d) for d in (date(2026, 12, 14), date(2026, 12, 31), date(2027, 1, 6),
                                                              date(2027, 1, 7))], [False, True, True, False])
        self.assertTrue(render.pick_live({}, date(2026, 5, 1)))
        self.assertFalse(render.pick_live({"months": [10]}, date(2026, 5, 1)))

    def test_started_events_and_freshness(self):
        cfg = CFG
        today = NOW.date().isoformat()  # NOW = 18:16
        events = [{"title": "Matí", "start": today, "time": "10:00", "source": "ajuntament"},
                  {"title": "Vespre", "start": today, "time": "20 h", "source": "gencat"},
                  {"title": "Sense hora", "start": today, "time": "tot el dia", "source": "ajuntament"},
                  {"title": "Llarg", "start": today, "end": "2026-11-30", "time": "10:00", "source": "ajuntament"}]
        old = "2026-11-17T09:00:00+01:00"  # más de 26 h antes de NOW
        site = Site(cfg, {"agenda": {"fetched": NOW.isoformat(), "data": {"events": events}},
                          "alerts": {"fetched": old, "data": []}}, "unused", NOW)
        self.assertEqual([site.started(e) for e in events], [True, False, False, False])
        self.assertEqual(render.event_start(events[1]).isoformat(), f"{today}T20:00:00+01:00")
        self.assertEqual([e["title"] for e in site.upcoming()], ["Vespre", "Sense hora", "Llarg"])
        done = site.event_item("ca", events[0])
        self.assertIn('class="started"', done)
        self.assertIn('<span class="tier t3 on">Ja ha començat</span>', done)
        later = site.event_item("ca", events[1])
        self.assertIn(f'data-start="{today}T20:00+01:00"', later)
        self.assertIn('class="tier t3 on" hidden', later)
        self.assertNotIn("data-start", site.event_item("ca", events[3]))  # varios días: no «empieza» hoy
        self.assertNotIn("Matí", site.agenda_preview("ca"))
        self.assertIn('data-upcoming="6"', site.agenda_preview("ca"))
        self.assertIn('class="stale">', site.stamp("ca", "agenda", "alerts"))  # manda la fuente más antigua
        self.assertIn('class="stale" hidden>', site.stamp("ca", "agenda"))
        self.assertEqual(site.stamp("ca", "weather"), "")

    def test_light_tip(self):
        cfg = CFG

        def block(hour):
            slot = {"avg_eur_kwh": 0.07, "label": f"{hour:02d}:00–{hour + 3:02d}:00", "start": f"2026-11-19T{hour:02d}:00:00+01:00"}
            light = {"date": NOW.date().isoformat(), "cheapest": {"1": slot, "3": slot}}
            return Site(cfg, {"light": {"fetched": NOW.isoformat(), "data": light}}, "unused", NOW).light("es")
        self.assertIn("tarifa regulada (PVPC)", block(3))
        self.assertNotIn("energía solar", block(3))  # de madrugada no hay sol
        self.assertIn("energía solar", block(13))

    def test_filter_tags(self):
        self.assertEqual(render.aid_topics({"title": "Ayudas al alquiler para jóvenes", "purpose": "Otras"}), ["housing", "family"])
        self.assertEqual(render.aid_topics({"title": "Subvencions a l'agricultura", "purpose": "Cultura"}), ["culture"])  # no «cultur» suelto
        self.assertEqual(render.aid_topics({"title": "Premio X", "title_ca": None, "purpose": "Educación"}), ["study", "culture"])
        self.assertEqual(render.aid_topics({"title": "Convocatoria genérica"}), [])
        self.assertEqual(render.job_tags({"contract": "Contracte laboral indefinit", "hours": "Jornada parcial matí (15 hores)"}),
                         ["indef", "part"])
        self.assertEqual(render.job_tags({"contract": "Contracte laboral temporal (6 mesos)", "hours": "Jornada completa"}),
                         ["temp", "full"])
        cfg = {"site_name": "Sant Esteve Avui", "site_url": "https://example.github.io/x", "owner": {"email": "a@example.com"}}
        site = Site(cfg, {}, "unused", NOW)
        self.assertIn('href="mailto:a@example.com?subject=Sant%20Esteve%20Avui%3A%20sugerencia"', site.suggest("es"))
        self.assertEqual(Site({"site_name": "x", "site_url": "https://example.github.io/x"}, {}, "unused", NOW).suggest("es"), "")
        bar = site.filter_bar("ca", [("a", "A")], [("b", "B")])
        self.assertIn("data-filter hidden", bar)  # sin JS no se ve
        self.assertIn('data-filter-tag="b" data-filter-group="1"', bar)

    def test_same_keys(self):
        for lang in i18n.LANGS:
            self.assertEqual(set(i18n.STRINGS[lang]), set(i18n.STRINGS["ca"]), lang)

    def test_build_and_links(self):
        gencat, permanent = S.parse_agenda_gencat(fixture("agenda.json"), NOW.date())
        with mock.patch.object(S, "NEAR_TOWNS", SANT_CUGAT["NEAR_TOWNS"]):
            offers = S.parse_offers(fixture("feina_activa.xml"), NOW.date())
        with mock.patch.object(S, "_HERE", SANT_CUGAT["_HERE"]):
            borme = S.parse_borme(fixture("borme_bcn.html"), "2026-11-17", "https://www.boe.es/b", {})
        data = {
            "weather": S.parse_weather(fixture("metno.json"), NOW),
            "agenda": {"events": S.merge_events(gencat),
                       "permanent": permanent},
            "air": S.parse_air(fixture("air.json")),
            "alerts": S.parse_alerts(fixture("renfe_alerts.json"), OCT),
            "equipments": S.parse_equipments(fixture("equip.json")),
            "chargers": S.parse_chargers(fixture("ev.json")),
            "leisure": S.parse_leisure(fixture("osm.json")),
            "nightlife": [],
            "aids": {"calls": {d["codigoBDNS"]: S.parse_bdns_call(d, OCT.date()) for d in fixture("bdns_calls.json")}},
            "cido": S.parse_cido(fixture("cido.json"), NOW.date()),
            "offers": offers,
            "fuel": {"updated": "19/11/2026 6:00:00", "stations": S.parse_fuel(fixture("fuel_1067.json"), S.FUEL_TOWNS[S.FUEL_HOME])},
            "contracts": S.parse_contracts(fixture("contracts.json")),
            "pharmacy": {"source": S.PHARMACY_URL, "directory": S.parse_pharmacy_directory(fixture("osm_pharmacies.json")),
                         "rosters": {}},
            "companies": {"since": "2025-11-20", "last": "2026-11-17", "known": {}, "events": borme},
        }
        cfg = CFG
        with tempfile.TemporaryDirectory() as tmp:
            out = Site(cfg, {k: {"fetched": NOW.isoformat(), "data": v} for k, v in data.items()}, tmp, NOW).build()
            home = (out / "ca/index.html").read_text(encoding="utf-8")
            self.assertIn("Sant Esteve Sesrovires avui", home)
            self.assertNotIn("Cerdanyola", home.replace("Cerdanyola del Vallès", ""))  # solo como municipio vecino
            self.assertIn("Guia d'oci", home)
            leisure = (out / "es/ocio/index.html").read_text(encoding="utf-8")
            self.assertIn("Puig Madrona", leisure)  # del fixture
            self.assertIn('href="#i-party-popper"', leisure)  # icono de fiesta
            self.assertIn('aria-current="page"', leisure)  # chip de sección activo
            self.assertIn('class="sections"', leisure)
            self.assertIn("OpenStreetMap", leisure)
            self.assertIn("Trens: FGC R6 i Rodalies R4, R8 (Martorell)", home)
            self.assertIn('<span class="line">R8</span>', home)
            self.assertIn('href="https://www.fgc.cat/"', home)
            self.assertIn("Ajudes que pots demanar ara", home)
            aids = (out / "es/ayudas/index.html").read_text(encoding="utf-8")
            self.assertIn("convocatorias/", aids)
            self.assertNotIn("grupos politicos", aids.lower())
            self.assertIn('id="weekend"', leisure)
            agenda = (out / "ca/agenda/index.html").read_text(encoding="utf-8")
            self.assertIn("El fil de la modernitat", agenda)
            ics = (out / "agenda.ics").read_bytes().decode("utf-8")
            self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
            self.assertIn("Sant Esteve Sesrovires", ics)
            self.assertTrue(all(len(x.encode()) <= 75 for x in ics.split("\r\n")))
            jobs = (out / "ca/feina/index.html").read_text(encoding="utf-8")
            self.assertIn("cido.diba.cat/oposicions/", jobs)
            self.assertIn("feinaactiva.gencat.cat/search/offers/detail/", jobs)
            self.assertIn("Carburant més barat avui a Sant Esteve", home)
            self.assertIn('id="fuel"', (out / "es/equipamientos/index.html").read_text(encoding="utf-8"))
            self.assertIn("Inversions i empreses", home)
            invest = (out / "ca/inversions/index.html").read_text(encoding="utf-8")
            for text in ("zones verdes", "14,8 M€", "ofertes fins al 26 de novembre", "FRACTALSTACK S.L.", "feed.xml",
                         "no és cap recomanació", "Consell Comarcal"):
                self.assertIn(text, invest)
            self.assertNotIn("carregadors intel·ligents", invest)  # anul·lat
            atom = "{http://www.w3.org/2005/Atom}"
            feed = ET.parse(out / "es/inversiones/feed.xml").getroot()
            titles = [e.findtext(atom + "title") for e in feed.iter(atom + "entry")]
            self.assertIn("Empresas nuevas · FRACTALSTACK S.L.", titles)
            self.assertTrue(any(t.startswith("En licitación · ") for t in titles))
            self.assertIn("Estalvia a Sant Esteve", home)
            save = (out / "es/ahorra/index.html").read_text(encoding="utf-8")
            self.assertIn("La gasolinera más barata", save)
            self.assertNotIn("aiguesdebarcelona", save)  # no es de la zona de Aigües de Barcelona
            self.assertIn("Recomanat aquesta setmana", home)
            picks_en = (out / "en/recommendations/index.html").read_text(encoding="utf-8")
            politics = (out / "es/participa/index.html").read_text(encoding="utf-8")
            self.assertIn('class="ladder"', politics)
            self.assertIn('href="#town"', politics)
            self.assertIn('id="world"', politics)
            self.assertIn('href="#i-globe"', politics)
            self.assertTrue((out / "en/get-involved/index.html").exists())
            self.assertIn("Things to do</h2>", picks_en)
            self.assertNotIn("Where to eat</h2>", picks_en)  # aún no hay restaurantes elegidos
            self.assertTrue((out / "static/style.css").exists())
            self.assertFalse((out / "static/base.css").exists())
            sw = (out / "sw.js").read_text(encoding="utf-8")  # en la raíz, versionado por build
            self.assertIn('"santesteve-20', sw)
            self.assertNotIn("__BUILD__", sw)
            self.assertFalse((out / "static/sw.js").exists())
            mans = {lg: json.loads((out / f"static/manifest.{lg}.webmanifest").read_text(encoding="utf-8")) for lg in i18n.LANGS}
            self.assertEqual({m["id"] for m in mans.values()}, {"/santesteve-local/"})  # una sola app
            self.assertEqual(mans["es"]["start_url"], "/santesteve-local/es/")
            self.assertEqual(mans["en"]["display"], "standalone")
            self.assertEqual([x["url"] for x in mans["ca"]["shortcuts"]], [
                "/santesteve-local/ca/agenda/", "/santesteve-local/ca/emergencies/", "/santesteve-local/ca/farmacies/"])
            self.assertIn("/static/manifest.en.webmanifest", picks_en)
            self.assertIn('data-sw="/santesteve-local/sw.js"', picks_en)
            self.assertIn("data-install", picks_en)
            self.assertIn('href="#i-smartphone"', home)
            offline = (out / "offline.html").read_text(encoding="utf-8")
            self.assertIn("noindex", offline)
            self.assertIn("Sense connexió", offline)
            self.assertNotIn('rel="canonical"', offline)  # no es la portada: ni canonical ni chip activo
            self.assertNotIn('aria-current="page"', offline)
            self.assertIn('rel="canonical" href="https://example.github.io/santesteve-local/ca/"', home)
            self.assertIn("/static/now.js", home)
            self.assertIn('og:image', (out / "index.html").read_text(encoding="utf-8"))
            self.assertIn('class="note fresh" data-fetched=', home)
            self.assertLess(home.index('class="today"'), home.index('id="weather"'))  # franja «hoy» arriba del todo
            self.assertLess(home.index('class="quick"'), home.index('id="transport"'))
            self.assertIn("data-today-events=", home)
            for page in (aids, jobs, agenda):
                self.assertIn("/static/filter.js", page)
                self.assertIn("data-filter-scope", page)
                self.assertIn("data-filter-empty hidden", page)
            self.assertIn('data-filter-tag="indef"', jobs)
            self.assertIn("data-count>(", jobs)
            self.assertNotIn("/static/filter.js", home)
            pharmacies = (out / "ca/farmacies/index.html").read_text(encoding="utf-8")
            self.assertIn("Farmàcia Beringues Sorribas, Lluís", pharmacies)  # el directorio...
            self.assertIn("farmaguia.net", pharmacies)  # ...y el buscador oficial de guardias
            self.assertIn('href="#i-pill"', home)
            sos_ca = (out / "ca/emergencies/index.html").read_text(encoding="utf-8")
            self.assertIn('id="pharmacy"', sos_ca)
            sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
            self.assertIn("/ca/farmacies/</loc>", sitemap)
            sos = (out / "en/emergencies/index.html").read_text(encoding="utf-8")
            self.assertIn('href="tel:112"', sos)
            self.assertIn("Policia Local de Sant Cugat del Vallès", sos)  # del fixture
            # Sin worker (board_api_url) no hay tauler: ni página, ni menú, ni mapa del sitio, ni textos legales
            self.assertFalse((out / "ca/tauler").exists())
            self.assertNotIn("/tauler/", home + sitemap)
            privacy = (out / "ca/privacitat/index.html").read_text(encoding="utf-8")
            self.assertNotIn("tauler", privacy.lower())
            self.assertNotIn("<!--", privacy.split("<main", 1)[-1].split("</main>")[0])
            self.assertNotIn("data-visits", home)  # sin visits_url no hay contador
            broken = []
            for page in out.rglob("*.html"):
                p = Links()
                p.feed(page.read_text(encoding="utf-8"))
                for link in p.links:
                    link = link.split("#")[0].replace("https://example.github.io", "")
                    if link.startswith("/santesteve-local/"):
                        target = out / link[len("/santesteve-local/"):]
                        target = target / "index.html" if link.endswith("/") else target
                        if not target.exists():
                            broken.append(f"{page.relative_to(out)} -> {link}")
            self.assertEqual(broken, [])


class TestBoard(unittest.TestCase):
    POST = {"id": "abc123def456", "cat": "deal", "lang": "es", "title": "Bici <script>alert(1)</script>",
            "body": "Bici de paseo, poco uso.\nRecogida en el centro.", "name": "Marta", "contact": "Telegram @marta",
            "price": "40 €", "created": 1761000000, "expires": 1764888000, "email": "secreto@example.com"}

    def test_parse_board(self):
        parsed = S.parse_board({"posts": [self.POST, {"id": "x"}, "basura"]})
        self.assertEqual(len(parsed["posts"]), 1)
        self.assertNotIn("email", parsed["posts"][0])  # nunca llega a la web estática
        self.assertEqual(parsed["posts"][0]["price"], "40 €")
        with self.assertRaises(S.FetchError):
            S.parse_board({"error": "x"})

    def build(self, cfg, data=None):
        with tempfile.TemporaryDirectory() as tmp:
            out = Site(cfg, data or {}, tmp, NOW).build()
            return {lg: (out / lg / i18n.t(lg, "slug_board") / "index.html").read_text(encoding="utf-8") for lg in i18n.LANGS}

    def test_board_page_with_worker(self):
        cfg = dict(CFG, board_api_url="https://board.example.workers.dev/", turnstile_site_key="1x00000000000000000000AA",
                   visits_url="https://board.example.workers.dev/hit?site=sc")
        data = {"board": {"fetched": NOW.isoformat(), "data": {"posts": [self.POST]}}}
        page = self.build(cfg, data)["es"]
        self.assertIn('data-api="https://board.example.workers.dev"', page)
        self.assertIn("static/board.js", page)
        self.assertIn('data-visits="https://board.example.workers.dev/hit?site=sc" data-text="Visitas: {today} hoy', page)
        self.assertIn("challenges.cloudflare.com/turnstile", page)
        self.assertIn("Bici &lt;script&gt;alert(1)&lt;/script&gt;", page)  # título escapado
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("secreto@example.com", page)
        self.assertIn('id="p-abc123def456"', page)
        self.assertIn("Recogida en el centro.", page)
        self.assertIn('href="#i-tag"', page)
        self.assertIn("publicado el 21 de octubre", page)  # 1761000000 = 21/10/2025 00:40 en Madrid
        self.assertIn("<\\/", page)  # el JSON de textos escapa «</»
        self.assertIn('aria-current="page">Tablón</a>', page)
        self.assertIn("Tablón vecinal de Sant Esteve", page)

    def test_social(self):
        feed = (FIX / "youtube_feed.xml").read_text(encoding="utf-8")
        videos = S.parse_youtube(feed, "ajuntament", OCT.date())
        self.assertEqual([v["id"] for v in videos], ["WgyShGeo76U", "M61WQAPUto4", "veodYaaoP34", "_DGggN6rFg4"])
        self.assertEqual((videos[0]["views"], videos[0]["published"], videos[0]["short"]), (1364, "2026-07-09", True))
        self.assertFalse(videos[1]["short"])
        self.assertEqual(S.parse_youtube(feed, "x", date(2028, 1, 1)), [])  # los vídeos viejos no se guardan
        with self.assertRaises(S.FetchError):
            S.parse_youtube("<html>", "x", OCT.date())
        profiles = S.load_social()
        self.assertEqual(len({p["id"] for p in profiles}), len(profiles))
        for p in profiles:
            self.assertIn(p["group"], render.SOCIAL_GROUPS)
            self.assertEqual(set(p["desc"]), set(i18n.LANGS))
            self.assertTrue(p["links"] and all(u.startswith("https://") for u in p["links"].values()))
            date.fromisoformat(p["checked"])
        cfg = CFG
        evil = {"id": "abcdefghijk", "title": "<b>Hola</b>", "published": "2026-10-01", "channel": "ajuntament", "short": False, "views": 37826}
        old = dict(evil, id="oldoldoldol", published="2025-01-01", views=10 ** 6)
        other = dict(evil, id="otherchanne", channel="no-existe", views=10 ** 6)
        data = {"social": {"fetched": NOW.isoformat(), "data": {"videos": videos + [evil, old, other]}}}
        with tempfile.TemporaryDirectory() as tmp:
            out = Site(cfg, data, tmp, NOW).build()
            page = (out / "es/redes/index.html").read_text(encoding="utf-8")
            home = (out / "es/index.html").read_text(encoding="utf-8")
            self.assertIn("redes/", (out / "sitemap.xml").read_text(encoding="utf-8"))
        self.assertIn("&lt;b&gt;Hola&lt;/b&gt;", page)
        self.assertIn("37.826 visualizaciones", page)
        self.assertIn('href="https://www.youtube.com/watch?v=abcdefghijk"', page)
        self.assertIn('href="https://www.youtube.com/shorts/WgyShGeo76U"', page)
        self.assertEqual(page.count("i.ytimg.com/vi/"), render.VIDEOS_PER_CHANNEL)  # tope por canal
        for hidden in ("oldoldoldol", "otherchanne", "M61WQAPUto4"):
            self.assertNotIn(hidden, page)
        self.assertIn("https://www.instagram.com/ajuntamentsesrovires/", page)
        self.assertIn('aria-current="page">Redes</a>', page)
        self.assertIn("Lo más visto en las redes", home)
        with tempfile.TemporaryDirectory() as tmp:  # sin datos de YouTube queda el directorio
            out = Site(cfg, {}, tmp, NOW).build()
            page = (out / "ca/xarxes/index.html").read_text(encoding="utf-8")
            self.assertNotIn("El més vist a les xarxes", (out / "ca/index.html").read_text(encoding="utf-8"))
        self.assertNotIn("ytimg", page)
        self.assertIn("A qui seguir", page)

    def test_board_home_count(self):
        cfg = dict(CFG, board_api_url="https://b.example")
        data = {"board": {"fetched": NOW.isoformat(), "data": {"posts": [self.POST]}}}
        with tempfile.TemporaryDirectory() as tmp:
            out = Site(cfg, data, tmp, NOW).build()
            self.assertIn("1 anuncis actius", (out / "ca/index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
