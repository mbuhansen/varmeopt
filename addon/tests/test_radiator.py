"""Radiatorernes ventiler og rum: rummene, nu-billedet og logbogens halvtimer."""

import unittest

from varmeopt.__main__ import Varmeopt
from varmeopt.logbook import Logbook, Reading
from varmeopt.options import _DEFAULTS
from varmeopt.radiator import parse_rooms, room_state, snapshot

SLOT = 1_764_000_000.0 - 1_764_000_000.0 % 1800


class RoomsTest(unittest.TestCase):
    def test_standardrummene(self):
        rooms = parse_rooms(_DEFAULTS["radiator_rooms"])
        self.assertEqual(len(rooms), 7)
        stue = rooms[0]
        self.assertEqual(stue.name, "stue")
        self.assertEqual(len(stue.valves), 2)
        # Badeværelset har gulvvarme og er ikke med.
        self.assertFalse(any("bad" in r.climate for r in rooms))
        # Alle otte ventiler, ingen to gange.
        valves = [v for r in rooms for v in r.valves]
        self.assertEqual(len(valves), 8)
        self.assertEqual(len(set(valves)), 8)

    def test_tastefejl_springes_over(self):
        rooms = parse_rooms("climate.a=sensor.x; uden lighedstegn; climate.b= ;;")
        self.assertEqual([r.climate for r in rooms], ["climate.a"])

    def test_vindue_overstyring_og_slukket_udelades(self):
        rooms = [
            room_state("stue", "heat", {"current_temperature": 22.0, "temperature": 22.5},
                       [45.0, 60.0]),
            room_state("køkken", "heat", {"window_open": True, "current_temperature": 18.0,
                                          "temperature": 22.5}, [100.0]),
            room_state("kontor", "heat", {"override_until": "2026-11-24T22:00",
                                          "current_temperature": 20.0,
                                          "temperature": 24.0}, [90.0]),
            # Slukket beholder sit mål - som gangen i RoomMind.
            room_state("sove", "off", {"current_temperature": 17.0, "temperature": 21.5},
                       [1.0]),
            room_state("gang", "heat", {"current_temperature": 21.9, "temperature": 22.0},
                       [None, 35.0]),
        ]
        snap = snapshot(rooms)
        self.assertEqual(snap.top, 60.0)
        self.assertEqual(snap.top_room, "stue")
        self.assertEqual(snap.second, 45.0)
        # Køkkenets, kontorets og soveværelsets underskud tæller ikke.
        self.assertAlmostEqual(snap.shortfall, 0.5)
        self.assertEqual(set(snap.shortfalls), {"stue", "gang"})
        self.assertEqual(snap.rooms, 2)
        self.assertEqual(len(snap.excluded), 3)
        self.assertIn("sove: slukket", snap.excluded)

    def test_ingen_termostat_er_ukendt(self):
        snap = snapshot([room_state("stue", None, None, [None])])
        self.assertIsNone(snap.top)
        self.assertIsNone(snap.shortfall)
        self.assertEqual(snap.shortfalls, {})


class LogbookTest(unittest.TestCase):
    def walk(self, make):
        book = Logbook(None)
        rows = []
        for m in range(0, 31):
            out = book.observe(SLOT + m * 60, make(m), lambda: {})
            if out is not None:
                rows.append(out)
        self.assertEqual(len(rows), 1)
        return rows[0]["skete"]

    def test_middel_rum_og_underskud(self):
        def make(m):
            # Første ti minutter er køkkenet mest åbent og mangler 0,8.
            early = m < 10
            return Reading(
                vp_el=2.0, vp_el_rum=2.0 if m < 15 else 0.0, ude=5.0,
                ventil_max=80.0 if early else 50.0, ventil_anden=30.0,
                ventil_rum="køkken" if early else "stue",
                frem_hus=45.0, rum_mangler=0.8 if early else 0.1,
                rum_mangler_rum={"køkken": 0.8 if early else -0.2, "stue": 0.1},
            )

        skete = self.walk(make)
        self.assertAlmostEqual(skete["ventil_max"], (10 * 80 + 20 * 50) / 30, places=1)
        self.assertEqual(skete["ventil_rum"], "stue")
        self.assertEqual(skete["frem_hus"], 45.0)
        # Det største, ikke middel - både samlet og pr. rum.
        self.assertEqual(skete["rum_mangler"], 0.8)
        self.assertEqual(skete["rum_mangler_rum"], {"køkken": 0.8, "stue": 0.1})
        # 15 minutter à 2 kW.
        self.assertAlmostEqual(skete["vp_el_rum_kwh"], 0.5, places=3)

    def test_ventiler_der_tier_giver_intet_middel(self):
        skete = self.walk(
            lambda m: Reading(ventil_max=50.0 if m < 5 else None, frem_hus=45.0)
        )
        self.assertIsNone(skete["ventil_max"])
        self.assertIsNone(skete["rum_mangler"])
        self.assertEqual(skete["rum_mangler_rum"], {})
        self.assertEqual(skete["frem_hus"], 45.0)

    def test_ny_halvtime_starter_forfra(self):
        book = Logbook(None)
        rows = []
        for m in range(0, 61):
            reading = Reading(rum_mangler=2.0 if m < 30 else 0.1,
                              rum_mangler_rum={"stue": 2.0 if m < 30 else 0.1})
            out = book.observe(SLOT + m * 60, reading, lambda: {})
            if out is not None:
                rows.append(out)
        self.assertEqual(rows[1]["skete"]["rum_mangler"], 0.1)
        self.assertEqual(rows[1]["skete"]["rum_mangler_rum"], {"stue": 0.1})


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
