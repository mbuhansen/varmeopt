"""Radiatorernes skyggemåling: rummene, logbogens middel og nattens regning."""

import random
import unittest

from varmeopt.__main__ import Varmeopt
from varmeopt.logbook import Logbook, Reading
from varmeopt.options import _DEFAULTS
from varmeopt.radiator import (
    MAX_LUFT,
    MIN_SLOTS,
    RadiatorReport,
    fit,
    parse_rooms,
    room_state,
    snapshot,
)

SLOT = 1_764_000_000.0 - 1_764_000_000.0 % 1800


def row(t, opening, supply, outdoor, shortfall=0.0, cover=1.0, el=None, price=2.0, room="stue"):
    return {
        "t": t,
        "dækning": cover,
        "så": {"el_kr": price},
        "skete": {
            "ventil_max": opening, "frem_hus": supply, "ude_middel": outdoor,
            "rum_mangler": shortfall, "vp_el_rum_kwh": el, "ventil_rum": room,
        },
    }


def house(n=400, seed=1, a=200.0, b=-3.0, c=-1.5, noise=2.0):
    """Ventilerne efter en kendt lov: fremløbet svinger 44-66, ude 0-10."""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        supply = rnd.uniform(44, 66)
        outdoor = rnd.uniform(0, 10)
        opening = a + b * supply + c * outdoor + rnd.gauss(0, noise)
        rows.append(row(SLOT + i * 1800, opening, supply, outdoor, el=0.5))
    return rows


class RoomsTest(unittest.TestCase):
    def test_standardrummene(self):
        rooms = parse_rooms(_DEFAULTS["radiator_rooms"])
        self.assertEqual(len(rooms), 7)
        stue = rooms[0]
        self.assertEqual(stue.name, "stue")
        self.assertEqual(len(stue.valves), 2)
        # Badeværelset har gulvvarme og er ikke med.
        self.assertFalse(any("bad" in r.climate for r in rooms))

    def test_tastefejl_springes_over(self):
        rooms = parse_rooms("climate.a=sensor.x; uden lighedstegn; climate.b= ;;")
        self.assertEqual([r.climate for r in rooms], ["climate.a"])

    def test_vindue_og_overstyring_udelades(self):
        rooms = [
            room_state("stue", {"current_temperature": 22.0, "temperature": 22.5}, [45.0, 60.0]),
            room_state("køkken", {"window_open": True, "current_temperature": 18.0,
                                  "temperature": 22.5}, [100.0]),
            room_state("kontor", {"override_until": "2026-11-24T22:00",
                                  "current_temperature": 20.0, "temperature": 24.0}, [90.0]),
            room_state("gang", {"current_temperature": 21.9, "temperature": 22.0}, [None, 35.0]),
        ]
        snap = snapshot(rooms)
        self.assertEqual(snap.top, 60.0)
        self.assertEqual(snap.top_room, "stue")
        self.assertEqual(snap.second, 45.0)
        # Køkkenets og kontorets underskud tæller ikke: de er udeladt.
        self.assertAlmostEqual(snap.shortfall, 0.5)
        self.assertEqual(snap.rooms, 2)
        self.assertEqual(len(snap.excluded), 2)

    def test_ingen_termostat_er_ukendt(self):
        snap = snapshot([room_state("stue", None, [None])])
        self.assertIsNone(snap.top)
        self.assertIsNone(snap.shortfall)


class LogbookTest(unittest.TestCase):
    def test_middel_rum_og_underskud(self):
        book = Logbook(None)
        rows = []
        for m in range(0, 31):
            # Første ti minutter er køkkenet mest åbent og et rum mangler 0,8.
            early = m < 10
            reading = Reading(
                vp_el=2.0, vp_el_rum=2.0 if m < 15 else 0.0, ude=5.0,
                ventil_max=80.0 if early else 50.0, ventil_anden=30.0,
                ventil_rum="køkken" if early else "stue",
                frem_hus=45.0, rum_mangler=0.8 if early else 0.1,
            )
            out = book.observe(SLOT + m * 60, reading, lambda: {})
            if out is not None:
                rows.append(out)
        self.assertEqual(len(rows), 1)
        skete = rows[0]["skete"]
        self.assertAlmostEqual(skete["ventil_max"], (10 * 80 + 20 * 50) / 30, places=1)
        self.assertEqual(skete["ventil_rum"], "stue")
        self.assertEqual(skete["frem_hus"], 45.0)
        # Det største, ikke middel.
        self.assertEqual(skete["rum_mangler"], 0.8)
        # 15 minutter à 2 kW.
        self.assertAlmostEqual(skete["vp_el_rum_kwh"], 0.5, places=3)

    def test_ventiler_der_tier_giver_intet_middel(self):
        book = Logbook(None)
        rows = []
        for m in range(0, 31):
            reading = Reading(ventil_max=50.0 if m < 5 else None, frem_hus=45.0)
            out = book.observe(SLOT + m * 60, reading, lambda: {})
            if out is not None:
                rows.append(out)
        self.assertIsNone(rows[0]["skete"]["ventil_max"])
        self.assertIsNone(rows[0]["skete"]["rum_mangler"])


class FitTest(unittest.TestCase):
    def test_finder_loven(self):
        found, note = fit([
            (r["skete"]["ventil_max"], r["skete"]["frem_hus"], r["skete"]["ude_middel"])
            for r in house()
        ])
        self.assertEqual(note, "ok")
        self.assertAlmostEqual(found.b, -3.0, delta=0.1)
        self.assertAlmostEqual(found.c, -1.5, delta=0.2)
        # 75 % ved 5 grader: (75 - 200 + 7,5) / -3 = 39,2.
        self.assertAlmostEqual(found.needed_supply(5.0, 75.0), 39.2, delta=0.5)

    def test_for_faa_halvtimer(self):
        found, note = fit([(50.0, 45.0 + i % 10, 5.0) for i in range(MIN_SLOTS - 1)])
        self.assertIsNone(found)
        self.assertIn("samler", note)

    def test_for_lidt_spredning(self):
        found, note = fit([(50.0 + i % 3, 45.0 + (i % 2) * 0.5, 5.0) for i in range(200)])
        self.assertIsNone(found)
        self.assertIn("svinget", note)

    def test_ingen_sammenhaeng(self):
        rnd = random.Random(3)
        points = [(rnd.uniform(30, 60), rnd.uniform(44, 66), rnd.uniform(0, 10)) for _ in range(300)]
        found, note = fit(points)
        self.assertIn("ingen", note)


class ReportTest(unittest.TestCase):
    def test_maettede_og_kolde_rum_holdes_ude(self):
        """En ventil i loftet eller et rum der fryser, ville trække hældningen flad."""
        rows = house()
        # Halvtimer hvor fremløbet var lavt og ventilen stod i loftet, eller
        # hvor et rum manglede varme og ventilen var åben uanset fremløb.
        for i in range(150):
            rows.append(row(SLOT + (400 + i) * 1800, 100.0, 66.0 - i % 20, 5.0))
            rows.append(row(SLOT + (550 + i) * 1800, 90.0, 60.0 + i % 6, 5.0, shortfall=1.2))
        report = RadiatorReport()
        report.nightly(rows, "2026-11-25")
        self.assertTrue(report.usable)
        self.assertAlmostEqual(report.fit.b, -3.0, delta=0.15)

    def test_kroner_og_luft(self):
        report = RadiatorReport()
        report.nightly(house(), "2026-11-25")
        # Kurven siger 45, pumpen leverer 46,5; der var nok med ~39.
        report.price(
            lambda ude: 45.0,
            lambda setpoint, ude: 4.0 + (45.0 - setpoint) * 0.05,
            1.5, 75.0,
        )
        self.assertIsNotNone(report.luft_k)
        self.assertGreater(report.luft_k, 5.0)
        self.assertLessEqual(report.luft_k, MAX_LUFT)
        self.assertGreater(report.saving_kr, 0)
        self.assertIn("lavere", report.note)
        self.assertEqual(len(report.at_outdoor), 4)

    def test_luften_klemmes_aldrig_negativ(self):
        report = RadiatorReport()
        report.nightly(house(), "2026-11-25")
        # Kurven ligger allerede under det nødvendige.
        report.price(lambda ude: 30.0, lambda sp, ude: 4.0, 1.5, 75.0)
        self.assertEqual(report.luft_k, 0.0)
        self.assertEqual(report.saving_kwh, 0.0)

    def test_samme_nat_to_gange(self):
        report = RadiatorReport()
        self.assertTrue(report.nightly(house(), "2026-11-25"))
        self.assertFalse(report.nightly(house(), "2026-11-25"))

    def test_gemmes_og_hentes(self):
        report = RadiatorReport()
        report.nightly(house(), "2026-11-25")
        report.price(lambda ude: 45.0, lambda sp, ude: 4.0 + (45 - sp) * 0.05, 1.5, 75.0)
        again = RadiatorReport.from_raw(report.to_raw())
        self.assertAlmostEqual(again.fit.b, report.fit.b, places=2)
        self.assertEqual(again.luft_k, report.luft_k)
        # Halvtimerne overlever, så kronerne kan regnes igen efter en genstart.
        again.price(lambda ude: 45.0, lambda sp, ude: 4.0 + (45 - sp) * 0.05, 1.5, 75.0)
        self.assertAlmostEqual(again.saving_kr, report.saving_kr, delta=0.2)

    def test_ulaeselig_fil(self):
        report = RadiatorReport.from_raw({"fit": {"a": "x"}, "recent": [[1]]})
        self.assertIsNone(report.fit)
        self.assertIn("samler", report.note)


class RoomHeatingPowerTest(unittest.TestCase):
    def test_kun_rumvarme(self):
        f = Varmeopt._room_heating_power
        self.assertEqual(f(3.0, False, False, False), 3.0)
        self.assertEqual(f(3.0, True, False, False), 0.0)
        self.assertEqual(f(3.0, False, True, False), 0.0)
        self.assertEqual(f(3.0, False, False, True), 0.0)
        # Ukendt varmtvandsflag er ukendt, ikke rumvarme.
        self.assertIsNone(f(3.0, None, False, False))
        self.assertIsNone(f(None, False, False, False))


if __name__ == "__main__":
    unittest.main()
